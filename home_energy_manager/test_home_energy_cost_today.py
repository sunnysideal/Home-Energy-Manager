import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "home_forecaster" / "app"
sys.path.insert(0, str(APP))


def _load_runtime(payload=None, reasons=None):
    base = types.SimpleNamespace()
    base.simulate_battery = lambda slot, batt, soc_kwh, charge_eff, discharge_eff: (
        soc_kwh,
        0.0,
        0.0,
        0.0,
    )
    base.make_forecast = lambda client, store, cfg, now: (
        payload or {"attributes": {}},
        list(reasons or []),
    )
    base.main = lambda: None
    base.LOG = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

    core = types.ModuleType("battery_model_core")
    core.base = base
    core._ensure_schema = lambda store: None
    core._observe = lambda client, store, cfg, now: None
    core._model_attributes = lambda store, cfg, now: {"bands": [], "diagnostics": {}}
    sys.modules["battery_model_core"] = core

    spec = importlib.util.spec_from_file_location(
        "battery_model_forecast_runtime_cost_test", APP / "battery_model_forecast_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, base


def _payload(import_cost_p, export_income_p, cost_p, generated_at="2026-09-14T10:00:00+01:00"):
    return {
        "attributes": {
            "forecast_generated_at": generated_at,
            "today": {
                "actual": {
                    "import_cost_p": import_cost_p,
                    "export_income_p": export_income_p,
                    "cost_p": cost_p,
                }
            },
        }
    }


def test_cost_sensor_uses_authoritative_today_actual_and_converts_to_gbp():
    runtime, _base = _load_runtime()
    state, attrs = runtime.cost_today_state(_payload(25.0, 12.0, 13.0), [])
    assert state == 0.13
    assert attrs["import_cost_today"] == 0.25
    assert attrs["export_income_today"] == 0.12
    assert attrs["net_cost_today"] == 0.13
    assert attrs["unit_of_measurement"] == "GBP"
    assert attrs["device_class"] == "monetary"
    assert "state_class" not in attrs


def test_export_income_can_make_net_cost_negative_including_axle_adjustment():
    runtime, _base = _load_runtime()
    state, attrs = runtime.cost_today_state(_payload(25.0, 188.0, -163.0), [])
    assert state == -1.63
    assert attrs["export_income_today"] == 1.88
    assert attrs["net_cost_today"] == -1.63


def test_incomplete_actual_pricing_publishes_unknown_not_zero():
    runtime, _base = _load_runtime()
    state, attrs = runtime.cost_today_state(
        _payload(0.0, 0.0, 0.0),
        ["Import tariff unavailable: import cost outputs incomplete"],
    )
    assert state == "unknown"
    assert attrs["available"] is False
    assert attrs["reason"] == "actual_pricing_incomplete"


def test_day_rollover_uses_new_payload_without_carrying_previous_day_value():
    runtime, _base = _load_runtime()
    first_state, first_attrs = runtime.cost_today_state(
        _payload(140.0, 10.0, 130.0, "2026-09-14T23:55:00+01:00"), []
    )
    next_state, next_attrs = runtime.cost_today_state(
        _payload(7.0, 0.0, 7.0, "2026-09-15T00:05:00+01:00"), []
    )
    assert first_state == 1.3
    assert next_state == 0.07
    assert first_attrs["forecast_generated_at"].startswith("2026-09-14")
    assert next_attrs["forecast_generated_at"].startswith("2026-09-15")


def test_forecast_wrapper_publishes_cost_sensor_from_final_payload():
    payload = _payload(40.0, 15.0, 25.0)
    runtime, base = _load_runtime(payload=payload)

    class Client:
        def __init__(self):
            self.calls = []

        def publish(self, entity, state, attrs):
            self.calls.append((entity, state, attrs))

    client = Client()
    returned, reasons = base.make_forecast(client, object(), object(), object())
    assert returned is payload
    assert reasons == []
    assert len(client.calls) == 1
    entity, state, attrs = client.calls[0]
    assert entity == "sensor.home_energy_cost_today"
    assert state == 0.25
    assert attrs["source"] == "today.actual"
