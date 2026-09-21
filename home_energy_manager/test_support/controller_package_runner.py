"""Deterministic, out-of-process harness for the *packaged* controller entry point.

The pytest suite copies exactly the controller files selected by the production
Dockerfile. This runner imports that copy, not source-tree controller modules.
Only Home Assistant, MQTT transport, the wall clock and the inverter are faked.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
BASE = datetime(2026, 9, 21, 14, 0, tzinfo=TZ)
CHEAP_START = datetime(2026, 9, 21, 23, 0, tzinfo=TZ)
CHEAP_END = datetime(2026, 9, 22, 6, 0, tzinfo=TZ)
AXLE_START = datetime(2026, 9, 21, 18, 30, tzinfo=TZ)
AXLE_END = datetime(2026, 9, 21, 19, 30, tzinfo=TZ)


class FakeHA:
    def __init__(self):
        self.t = "test-supervisor-token"
        self.states = {}
        self.writes = []
        self.published = []

    def set(self, entity, value, attrs=None):
        self.states[entity] = {"state": str(value), "attributes": attrs or {}}

    async def state(self, entity):
        return self.states.get(entity)

    async def write(self, entity, value):
        self.writes.append({"entity": entity, "value": value})
        self.set(entity, value)

    async def publish(self, entity, value, attrs):
        self.published.append({"entity": entity, "value": value, "attributes": attrs})


def instant(at):
    return at.isoformat()


def battery_model(now):
    ranges = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
              (50, 60), (60, 70), (70, 80), (80, 90), (90, 95),
              (95, 98), (98, 99), (99, 100)]
    return {"schema_version": 1, "generated_at": instant(now),
            "bands": [{"soc_lo": a, "soc_hi": b, "effective_factor": .95}
                      for a, b in ranges],
            "top_completion_allowance_minutes": 15,
            "generic_top_completion_minutes": 15}


def no_slots_soc(at, target):
    if at < CHEAP_START:
        fraction = max(0, min(1, (at - BASE).total_seconds() /
                               (CHEAP_START - BASE).total_seconds()))
        return round(82 - 52 * fraction, 2)
    low_point = datetime(2026, 9, 22, 3, 0, tzinfo=TZ)
    fraction = max(0, min(1, (at - CHEAP_START).total_seconds() /
                           (low_point - CHEAP_START).total_seconds()))
    return round(30 + (target - 30) * fraction, 2)


def forecast(now, entities, natural_soc=4, complete=True):
    # Contract-shaped snapshots; all 30-minute rows are no-slots forecasts.
    # The low scenario reaches 4% naturally by 03:00, the shortfall scenario
    # arrives near 18%, and both use the same EDF 23:00–06:00 cheap period.
    limit = datetime(2026, 9, 23, 15, tzinfo=TZ) if complete else CHEAP_END
    start = BASE
    rows = []
    while start < limit:
        rows.append({"start": instant(start), "soc": no_slots_soc(start, natural_soc),
                     "load_kwh": .125, "pv_kwh": 0.0, "battery_kwh": .125,
                     "import_rate_p": 6.99 if CHEAP_START <= start < CHEAP_END else 31.12})
        start += timedelta(minutes=30)
    return {"state": "ready", "attributes": {
        "forecast_sequence": 123, "forecast_generated_at": instant(now),
        "forecast_trigger": "scheduled",
        "overnight_start_soc_no_slots": 30,
        "forecast_no_slots": rows,
        "offpeak": {"start": instant(CHEAP_START), "end": instant(CHEAP_END), "rate_p": 6.99},
        "controller_inputs": {"version": 3, "timezone": "Europe/London",
                              "refresh_request_entity": "sensor.mock_refresh_request",
                              "entities": entities, "values": {
                                  "grid_import_meter_source": "true_meter",
                                  "grid_export_meter_source": "true_meter"}},
        "metering": {"import_source": "true_meter", "export_source": "true_meter",
                     "import_entity": entities["grid_import_energy_total"],
                     "export_entity": entities["grid_export_energy_total"]},
        "today": {"total": {"pv_kwh": 4.0}, "actual": {"export_kwh": 0.0}},
        "no_slots_totals": {"today": {"export_kwh": 0.0}},
    }}


def prepare_entities(ha, mapping, *, soc=82):
    entities = {}
    for src in mapping:
        if src.endswith("_start") or src.endswith("_end"):
            domain, value = "time", "00:00:00"
        elif src in ("eco_mode", "charge_schedule_enable", "discharge_schedule_enable"):
            domain, value = "switch", "on"
        elif src == "pause_mode":
            domain, value = "select", "Disabled"
        elif src in ("battery_soc", "battery_capacity", "battery_reserve",
                     "pv_energy_total", "grid_import_energy_total",
                     "grid_export_energy_total", "inverter_max_charge_rate",
                     "inverter_max_discharge_rate"):
            domain = "sensor"
            value = {"battery_soc": soc, "battery_capacity": 13.79,
                     "battery_reserve": 4, "inverter_max_charge_rate": 6000,
                     "inverter_max_discharge_rate": 6000}.get(src, 0)
        else:
            domain, value = "number", 100 if src == "charge_slot_1_target" else (
                4 if src == "discharge_slot_1_target" else 3500)
        entity = f"{domain}.integration_{src}"
        ha.set(entity, value)
        entities[src] = entity
    return entities


async def run(data):
    package = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(package / "runtime" / "controller"))
    sys.path.insert(1, str(package))
    os.environ["HOME_ENERGY_MQTT_CONFIG"] = '{"enabled":false}'
    os.environ["SUPERVISOR_TOKEN"] = "test-supervisor-token"

    if data["case"] == "forecast_only":
        import forecast_only
        ha = FakeHA()
        passive = forecast_only.PassiveController({
            "operation_mode": "forecast_only", "home_energy_forecast_entity": "sensor.home_energy_forecast",
            "status_entity_id": "sensor.home_energy_controller",
        })
        async def fake_state(entity):
            return {"state": "ready", "attributes": {"forecast_sequence": 123,
                    "forecast_generated_at": instant(BASE), "controller_inputs": {"version": 3}}}
        passive.state = fake_state
        passive.publish_rest = ha.publish
        await passive.publish_status()
        return {"active_controller_loaded": "app" in sys.modules,
                "writes": ha.writes, "published": ha.published}

    import app   # The actual final Docker /app/runtime/controller/app.py.
    import active_runtime
    import controller_discovery
    import minimise_export_core
    import legacy_minimise_export_runtime

    from controller_db import DB
    now = BASE + timedelta(minutes=int(data.get("clock_minutes", 0)))
    ha = FakeHA()
    entities = prepare_entities(ha, controller_discovery.ENTITY_MAPPING,
                                soc=data.get("soc", 82))
    ha.set("sensor.home_energy_manager_battery_model", "ready", battery_model(now)
           if not data.get("invalid_model") else {"schema_version": 0})
    ev_entity = "switch.integration_ev_active"
    ha.set(ev_entity, "on" if data.get("ev_active") else "off")
    ha.set("sensor.home_energy_manager_axle", "ready", {
        "event_available": data.get("axle", "none") != "none",
        "event_type": "export",
        "start": instant(AXLE_START), "end": instant(AXLE_END),
    })
    pd_entity = "sensor.integration_power_down"
    pd_event = {"start": instant(BASE + timedelta(minutes=30)),
                "end": instant(BASE + timedelta(minutes=90)),
                "id": "test-event", "octopoints_per_kwh": 100}
    ha.set(pd_entity, "ready", {"joined_events": [pd_event] if data.get("power_down") else []})

    db = DB(Path(sys.argv[2]) / "controller.db")
    db.open()
    cfg = {"timezone": "Europe/London",
           "operation_mode": data.get("mode", "minimise_export"),
           "calibration_enabled": bool(data.get("calibration_enabled", False)),
           "deep_cycle_floor_soc": 4, "reserve_dwell_minutes": 15,
           "charge_safety_margin_minutes": 10, "safety_buffer_soc": 20,
           "minimise_export_min_soc": 25, "preferred_charge_c_rate": .25,
           "max_charge_c_rate": .4, "power_down_enabled": True,
           "power_down_events_entity": pd_entity if data.get("power_down") else "",
           "axle_entity": "sensor.home_energy_manager_axle",
           "ev_smart_charging_active_entity": ev_entity,
           "ev_smart_charging_enabled": True, "write_retry_attempts": 1}
    controller = app.core.Controller(cfg, ha, db)
    controller.now = lambda: now
    source = forecast(now, entities, data.get("natural_soc", 4),
                      complete=not data.get("incomplete_forecast"))
    assert controller.discover_from_forecast(source)
    # Exercise real Controller.apply and the production charge-target suppression
    # layer. Stub only its slow HA retry/readback boundary (tested separately).
    async def fake_original_ensure(self, field, entity, value, _window_end=None):
        await self.ha.write(entity, value)
        self.confirmed_writes_this_apply += 1
        return True
    active_runtime._original_ensure = fake_original_ensure

    async def compute():
        controller.confirmed = None
        plan = await controller.plan(source, data.get("soc", 82),
                                     {"start": CHEAP_START, "end": CHEAP_END, "rate_p": 6.99})
        if plan is not None:
            assert await controller.apply(plan)
        return plan

    if data.get("request"):
        # Exercise the installed manual-button event + real persisted sampler.
        controller._manual_low_calibration_press.set()
        await controller.sample()
    if data.get("restart"):
        # Do not issue a second request. A fresh controller instance must read
        # exactly the request that the first instance persisted.
        controller = app.core.Controller(dict(cfg), ha, db)
        controller.now = lambda: now
        assert controller.discover_from_forecast(source)

    if data.get("incorrect_patch"):
        # PR #92's regression: setting the forwarding module creates an
        # attribute there, but MUST NOT change the actual executing planner.
        legacy_minimise_export_runtime._minimise_calibration_discharge = lambda *_args: None
        assert legacy_minimise_export_runtime.runtime is minimise_export_core
        assert legacy_minimise_export_runtime._minimise_calibration_discharge is not (
            minimise_export_core._minimise_calibration_discharge)
    if data.get("incorrect_patch_only"):
        # Reproduce the PR #92 failure: register a valid replacement on the
        # forwarding module, leaving the effective planner unmodified.
        correct = minimise_export_core._minimise_calibration_discharge
        minimise_export_core._minimise_calibration_discharge = lambda *_args: None
        legacy_minimise_export_runtime._minimise_calibration_discharge = correct
        assert legacy_minimise_export_runtime.runtime is minimise_export_core

    plan = await compute()
    initial = {"plan": plan, "writes": list(ha.writes),
               "calibration_state": controller.calibration_state(),
               "request_pending": db.get("manual_low_calibration_pending", False),
               "low_reached_at": db.get("calibration_low_reached_at"),
               "auto_calibration_enabled": controller.c["calibration_enabled"],
               "model_source": getattr(controller, "_battery_model_source", None),
               "errors": controller.errors,
               "entities": entities}
    if data.get("case") == "lifecycle":
        now = datetime(2026, 9, 22, 3, 25, tzinfo=TZ)
        ha.set(entities["battery_soc"], 4)
        ha.set("sensor.home_energy_manager_battery_model", "ready", battery_model(now))
        await controller.sample()
        initial["observed_low"] = {"state": controller.calibration_state(),
                                   "pending": db.get("manual_low_calibration_pending"),
                                   "low_reached_at": db.get("calibration_low_reached_at")}
        ha.writes.clear()
        initial["recharge_plan"] = await compute()
        initial["recharge_writes"] = list(ha.writes)
        now = datetime(2026, 9, 22, 5, 55, tzinfo=TZ)
        ha.set(entities["battery_soc"], 100)
        ha.set("sensor.home_energy_manager_battery_model", "ready", battery_model(now))
        await controller.sample()
        initial["observed_full"] = {"state": controller.calibration_state(),
                                    "low_reached_at": db.get("calibration_low_reached_at"),
                                    "last_deep_calibration_at": db.get("last_deep_calibration_at"),
                                    "auto_calibration_enabled": controller.c["calibration_enabled"]}
    db.close()
    return initial


if __name__ == "__main__":
    try:
        result = asyncio.run(run(json.loads(sys.stdin.read())))
        print("@@CONTROLLER_TEST_RESULT@@" + json.dumps(result))
    except Exception:
        import traceback
        traceback.print_exc()
        raise
