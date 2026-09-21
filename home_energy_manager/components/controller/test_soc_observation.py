"""Exercise packaged SOC observation independently of automatic calibration."""
import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/London")
START = datetime(2026, 9, 21, 18, 40, tzinfo=TZ)


class Database:
    ok = True

    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class HomeAssistant:
    def __init__(self, states):
        self.states = states

    async def state(self, entity):
        value = self.states.get(entity)
        return None if value is None else {"state": str(value)}


class Controller:
    def __init__(self, enabled, energy):
        self.c = {
            "calibration_enabled": enabled,
            "battery_soc_entity": "sensor.soc",
            "battery_capacity_entity": "sensor.capacity",
            "deep_cycle_floor_soc": 4,
            "battery_charge_energy_total_entity": "sensor.charge" if energy else "",
            "battery_discharge_energy_total_entity": "sensor.discharge" if energy else "",
        }
        self.db = Database()
        self.ha = HomeAssistant({"sensor.soc": 60, "sensor.capacity": 13.79})
        self.discovery_ready = True
        self.clock = START
        self.writes = []
        if energy:
            self.ha.states.update({"sensor.charge": 10.0, "sensor.discharge": 20.0})

    def now(self):
        return self.clock

    def calibration_state(self):
        return "normal"

    async def write(self, *args):
        self.writes.append(args)


@pytest.fixture
def observer(monkeypatch):
    """Import the real active runtime with an inert legacy controller."""
    core = types.ModuleType("test_soc_observation_core")
    core.Controller = type("StubLegacyController", (), {
        "__init__": lambda self, *args, **kwargs: None,
        "calibration_state": lambda self: "normal",
        "ensure": lambda self, *args: None,
        "publish": lambda self, *args: None,
        "sample": lambda self: None,
        "calibration_attrs": lambda self: {},
    })
    core.LOG = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
    core.as_float = lambda value: float(value) if value is not None else None
    core.iso = lambda value: value.isoformat()
    core.parse_dt = lambda value: datetime.fromisoformat(value) if value else None
    runtime = types.ModuleType("minimise_export_runtime")
    runtime.core = core
    coordinator = types.ModuleType("calibration_coordinator")
    coordinator.CalibrationCoordinator = lambda *args: None
    monkeypatch.setitem(sys.modules, "minimise_export_runtime", runtime)
    monkeypatch.setitem(sys.modules, "calibration_coordinator", coordinator)
    spec = importlib.util.spec_from_file_location("test_soc_observation_active_runtime", ROOT / "active_runtime.py")
    active = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(active)

    async def original_sample(controller):
        # Reproduce the relevant legacy full/low-calibration updates when enabled.
        if not controller.c["calibration_enabled"]:
            return
        soc = controller.ha.states["sensor.soc"]
        if soc >= 100:
            controller.db.set("last_full_soc_at", controller.now().isoformat())
            if controller.db.get("calibration_low_reached_at"):
                controller.db.set("last_deep_calibration_at", controller.now().isoformat())
                controller.db.set("calibration_low_reached_at", None)
        elif soc <= 4 and controller.calibration_state() == "awaiting_deep_low":
            controller.db.set("calibration_low_reached_at", controller.now().isoformat())

    active._original_sample = original_sample
    active._HOME_FORECASTER_OPTIONS = ROOT / "nonexistent-test-meter-options.json"
    return active


async def reading(observer, controller, minutes, soc, discharged_kwh=None):
    controller.clock = START + timedelta(minutes=minutes)
    controller.ha.states["sensor.soc"] = soc
    if discharged_kwh is not None:
        controller.ha.states["sensor.discharge"] = 20.0 + discharged_kwh
    await observer._sample_soc_calibration_observation_core(controller)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_crossings_recorded_and_energy_window_completed_with_or_without_automatic_calibration(observer, enabled):
    c = Controller(enabled=enabled, energy=True)
    await reading(observer, c, 0, 60, 0)
    await reading(observer, c, 5, 46, 0.4)
    await reading(observer, c, 6, 39, 0.5)
    await reading(observer, c, 7, 35, 0.6)
    await reading(observer, c, 10, 30, 0.9)
    await reading(observer, c, 11, 18, 1.0)
    await reading(observer, c, 16, 12, 1.3)
    assert c.db.get("last_below_40_soc_at") == (START + timedelta(minutes=6)).isoformat()
    assert c.db.get("last_below_20_soc_at") == (START + timedelta(minutes=11)).isoformat()
    for threshold in (40, 20):
        key = f"soc_crossing_{threshold}"
        assert c.db.get(f"{key}_window_started_at")
        assert c.db.get(f"{key}_window_discharge_kwh") is not None
        assert c.db.get(f"{key}_window_correction_pp") is not None
        assert c.db.get(f"{key}_energy_window_pending") is None
    assert c.db.get("calibration_previous_soc") == 12
    assert not c.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_missing_energy_meters_do_not_prevent_crossing_or_soc_window(observer, enabled):
    c = Controller(enabled=enabled, energy=False)
    await reading(observer, c, 0, 60)
    await reading(observer, c, 5, 45)
    await reading(observer, c, 6, 15)  # Both thresholds in one sample.
    await reading(observer, c, 11, 10)
    for threshold in (40, 20):
        key = f"soc_crossing_{threshold}"
        assert c.db.get(f"last_below_{threshold}_soc_at") == (START + timedelta(minutes=6)).isoformat()
        assert c.db.get(f"{key}_soc_before") == 45
        assert c.db.get(f"{key}_soc_after") == 15
        assert c.db.get(f"{key}_window_actual_soc_delta_pp") is not None
        assert c.db.get(f"{key}_window_discharge_kwh") is None
        assert c.db.get(f"{key}_window_correction_pp") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_full_and_low_endpoints_observed_without_forcing_automatic_recharge(observer, enabled):
    c = Controller(enabled=enabled, energy=False)
    await reading(observer, c, 0, 25)
    await reading(observer, c, 1, 4)
    low = c.db.get("last_low_soc_at")
    assert low == (START + timedelta(minutes=1)).isoformat()
    assert c.db.get("last_deep_calibration_at") == low
    assert c.db.get("calibration_low_reached_at") is None
    await reading(observer, c, 2, 4)
    assert c.db.get("last_low_soc_at") == low
    await reading(observer, c, 3, 5)
    await reading(observer, c, 4, 4)
    assert c.db.get("last_low_soc_at") == (START + timedelta(minutes=4)).isoformat()
    await reading(observer, c, 5, 99)
    assert c.db.get("last_full_soc_at") is None
    await reading(observer, c, 6, 100)
    full = c.db.get("last_full_soc_at")
    assert full == (START + timedelta(minutes=6)).isoformat()
    await reading(observer, c, 7, 100)
    if not enabled:
        assert c.db.get("last_full_soc_at") == full
    assert not c.writes


@pytest.mark.asyncio
async def test_disabled_automatic_calibration_does_not_complete_deep_recharge_by_observation_alone(observer):
    c = Controller(enabled=False, energy=False)
    c.db.set("calibration_low_reached_at", START.isoformat())
    await reading(observer, c, 1, 100)
    assert c.db.get("last_full_soc_at") == (START + timedelta(minutes=1)).isoformat()
    assert c.db.get("calibration_low_reached_at") == START.isoformat()
    assert c.db.get("last_deep_calibration_at") is None
    # The separately owned manual coordinator completes an outstanding
    # manually requested recharge; the read-only observation path does not.
