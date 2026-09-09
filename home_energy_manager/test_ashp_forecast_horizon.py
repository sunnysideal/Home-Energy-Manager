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


class FakeBase:
    pass


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
