import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"

# Load the legacy module under the name expected by forecast_runner.
spec = importlib.util.spec_from_file_location("main", APP / "main.py")
legacy = importlib.util.module_from_spec(spec)
sys.modules["main"] = legacy
assert spec.loader is not None
spec.loader.exec_module(legacy)

spec = importlib.util.spec_from_file_location("forecast_runner", APP / "forecast_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules["forecast_runner"] = runner
assert spec.loader is not None
spec.loader.exec_module(runner)


def test_weather_extension_reaches_beyond_configured_48_hour_horizon(monkeypatch):
    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    raw = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 11.0 + i * 0.1}
        for i in range(45)
    ]

    client = object.__new__(runner.HorizonHAClient)
    client.forecast_hours = 48
    client.tz = tz
    monkeypatch.setattr(legacy.HAClient, "get_hourly_weather", lambda self, entity_id: list(raw))

    extended = client.get_hourly_weather("weather.test")
    parsed = [legacy.parse_dt(row["datetime"]).astimezone(tz) for row in extended]

    assert len(extended) > len(raw)
    assert parsed[-1] >= datetime.now(tz) + timedelta(hours=50)
    assert float(extended[-1]["temperature"]) == float(raw[-1]["temperature"])


def test_weather_is_not_extended_when_provider_already_covers_horizon(monkeypatch):
    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    raw = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 10.0}
        for i in range(60)
    ]

    client = object.__new__(runner.HorizonHAClient)
    client.forecast_hours = 48
    client.tz = tz
    monkeypatch.setattr(legacy.HAClient, "get_hourly_weather", lambda self, entity_id: list(raw))

    assert client.get_hourly_weather("weather.test") == raw


def test_timezone_lookup_falls_back_to_addon_timezone_when_ha_config_fails(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")

    class FailingClient:
        def get_config(self):
            raise RuntimeError("Home Assistant API GET /config failed: 401 Unauthorized")

    assert runner._timezone_name(FailingClient()) == "Europe/London"


def test_invalid_addon_timezone_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv("TZ", "Not/A-Timezone")
    assert runner._fallback_timezone_name() == "UTC"


def test_dhw_selector_waits_for_fresh_thermal_horizon(monkeypatch):
    calls = []
    legacy_values = [0.0] * 96
    thermal_values = [0.0] * 96
    thermal_values[10] = 1.25

    def fake_select(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return runner.SelectionResult(legacy_values, "legacy", "thermal_forecast_incomplete")
        return runner.SelectionResult(thermal_values, "thermal", "thermal_trial_active")

    monotonic_values = iter([0.0, 0.0, 0.1])
    monkeypatch.setattr(runner, "select_dhw_forecast", fake_select)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(monotonic_values, 0.1))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    cfg = SimpleNamespace(update_minutes=15)
    client = SimpleNamespace(get_state=lambda entity_id: {})
    store = SimpleNamespace(db=object())
    starts = [datetime.now(ZoneInfo("Europe/London")) + timedelta(minutes=30 * i) for i in range(96)]

    result = runner._select_dhw_with_refresh_wait(client, store, cfg, starts, legacy_values)

    assert len(calls) == 2
    assert result.source == "thermal"
    assert result.values == thermal_values


def test_dhw_selector_does_not_wait_for_non_transient_fallback(monkeypatch):
    calls = []
    legacy_values = [0.0] * 96

    def fake_select(*args, **kwargs):
        calls.append(1)
        return runner.SelectionResult(legacy_values, "legacy", "trial_not_ready")

    monkeypatch.setattr(runner, "select_dhw_forecast", fake_select)
    monkeypatch.setattr(runner.time, "sleep", lambda _: (_ for _ in ()).throw(AssertionError("unexpected sleep")))

    cfg = SimpleNamespace(update_minutes=15)
    client = SimpleNamespace(get_state=lambda entity_id: {})
    store = SimpleNamespace(db=object())
    starts = [datetime.now(ZoneInfo("Europe/London")) + timedelta(minutes=30 * i) for i in range(96)]

    result = runner._select_dhw_with_refresh_wait(client, store, cfg, starts, legacy_values)

    assert len(calls) == 1
    assert result.source == "legacy"
    assert result.reason == "trial_not_ready"
