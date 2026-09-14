import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent
ASHP_APP = ROOT / "components" / "ashp_forecaster" / "app"
HOME_APP = ROOT / "components" / "home_forecaster" / "app"

spec = importlib.util.spec_from_file_location("main", ASHP_APP / "main.py")
legacy = importlib.util.module_from_spec(spec)
sys.modules["main"] = legacy
assert spec.loader is not None
spec.loader.exec_module(legacy)

spec = importlib.util.spec_from_file_location("forecast_runner_issue74", ASHP_APP / "forecast_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
assert spec.loader is not None
spec.loader.exec_module(runner)

spec = importlib.util.spec_from_file_location("home_forecaster_issue74", HOME_APP / "main.py")
home = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = home
assert spec.loader is not None
spec.loader.exec_module(home)


class RecordingClient:
    def __init__(self, tz):
        self.tz = tz
        self.states = {}

    def set_sensor(self, entity_id, state, attributes):
        self.states[entity_id] = {"state": state, "attributes": attributes}

    def get_state(self, entity_id):
        return {"state": "0"}


def _cfg():
    return SimpleNamespace(
        forecast_hours=48,
        forecast_interval_minutes=30,
        base_temperature_c=15.5,
        winter_mode_below_c=11.0,
        summer_mode_above_c=12.0,
        weather_entity="weather.test",
        update_minutes=15,
    )


def test_dhw_failure_publishes_fresh_ch_and_unknown_combined_energy(monkeypatch):
    tz = ZoneInfo("Europe/London")
    client = RecordingClient(tz)
    store = SimpleNamespace(ensure_training_signature=lambda cfg: None)
    start = datetime.now(tz).replace(minute=(datetime.now(tz).minute // 30) * 30, second=0, microsecond=0)
    rows = [
        {
            "start": (start + timedelta(minutes=30 * i)).isoformat(),
            "temperature_c": 8.0,
            "heating_mode": "winter",
            "heating_enabled": True,
            "dhw_active": None,
            "degree_days": 0.15625,
            "ch_kwh": 0.25,
            "dhw_kwh": None,
            "energy_kwh": None,
            "dhw_status": "unavailable",
            "ch_priority_applied": False,
        }
        for i in range(96)
    ]
    monkeypatch.setattr(runner.legacy, "resolve_dynamic_thresholds", lambda client, cfg: cfg)
    monkeypatch.setattr(runner.legacy, "build_training", lambda client, store, cfg, tz: (1.6, 10, 120))
    monkeypatch.setattr(runner, "_build_ch_only_forecast", lambda client, store, cfg, coefficient, tz: rows)

    runner._publish_degraded_ch_forecast(client, store, _cfg(), tz, "thermal_horizon_incomplete")

    combined = client.states["sensor.ashp_forecast_next_48h"]
    assert combined["state"] == "unknown"
    assert combined["attributes"]["aggregate_status"] == "degraded"
    assert combined["attributes"]["ch_status"] == "fresh"
    assert combined["attributes"]["dhw_status"] == "unavailable"
    assert combined["attributes"]["forecast"][0]["dhw_kwh"] is None
    assert combined["attributes"]["forecast"][0]["energy_kwh"] is None

    ch = client.states["sensor.ashp_forecast_ch_next_48h"]
    assert float(ch["state"]) > 0.0
    assert ch["attributes"]["forecast"] == rows

    dhw = client.states["sensor.ashp_forecast_dhw_next_48h"]
    assert dhw["state"] == "unknown"
    health = client.states[runner.HEALTH_ENTITY]
    assert health["state"] == "degraded"
    assert health["attributes"]["ch_status"] == "fresh"
    assert health["attributes"]["dhw_status"] == "unavailable"


def test_home_forecaster_does_not_turn_unavailable_dhw_into_zero():
    tz = ZoneInfo("Europe/London")
    start = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    state = {
        "attributes": {
            "aggregate_status": "degraded",
            "ch_status": "fresh",
            "dhw_status": "unavailable",
            "forecast": [
                {
                    "start": start.isoformat(),
                    "ch_kwh": 0.42,
                    "dhw_kwh": None,
                    "energy_kwh": None,
                }
            ],
        }
    }

    ch_map = home.parse_ashp_slots(state, "ch_kwh")
    dhw_map = home.parse_ashp_slots(state, "dhw_kwh")

    key = start.astimezone(home.timezone.utc)
    assert ch_map[key] == pytest.approx(0.42)
    assert dhw_map == {}


def test_selector_failure_marks_dhw_unavailable_and_re_raises(monkeypatch):
    tz = ZoneInfo("Europe/London")
    client = RecordingClient(tz)
    store = SimpleNamespace(db=object())
    cfg = _cfg()
    starts = [datetime.now(tz) + timedelta(minutes=30 * i) for i in range(96)]

    def fail(*args, **kwargs):
        raise runner.DHWProductionUnavailable("thermal unavailable")

    monkeypatch.setattr(runner, "_select_dhw_with_refresh_wait", fail)
    runner._install_dhw_selector(client, store)

    with pytest.raises(runner.DHWProductionUnavailable, match="thermal unavailable"):
        runner.legacy.build_dhw_forecast(client, store, cfg, starts)

    source = client.states[runner.SOURCE_ENTITY]
    assert source["state"] == "unavailable"
    assert source["attributes"]["thermal_selected"] is False
    assert source["attributes"]["fallback_available"] is False
    assert "thermal unavailable" in source["attributes"]["reason"]


def test_selector_recovery_restores_thermal_source(monkeypatch):
    tz = ZoneInfo("Europe/London")
    client = RecordingClient(tz)
    store = SimpleNamespace(db=object())
    cfg = _cfg()
    starts = [datetime.now(tz) + timedelta(minutes=30 * i) for i in range(96)]
    values = [0.0] * 96
    values[3] = 1.2

    monkeypatch.setattr(
        runner,
        "_select_dhw_with_refresh_wait",
        lambda *args, **kwargs: runner.SelectionResult(values, "thermal", "thermal_authoritative"),
    )
    runner._install_dhw_selector(client, store)

    result = runner.legacy.build_dhw_forecast(client, store, cfg, starts)

    assert result == values
    source = client.states[runner.SOURCE_ENTITY]
    assert source["state"] == "thermal"
    assert source["attributes"]["thermal_selected"] is True
    assert source["attributes"]["reason"] == "thermal_authoritative"
