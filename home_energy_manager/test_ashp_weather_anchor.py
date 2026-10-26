"""Regression coverage for live outdoor-temperature anchoring of ASHP CH."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

APP = Path(__file__).resolve().parent / "components" / "ashp_forecaster" / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
import main as ashp


NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


class Client:
    ch_schedule_bits = None
    ch_schedule_models = None

    def __init__(self, actual=13.1, age_minutes=2, provider=14.7):
        self.actual = actual
        self.age_minutes = age_minutes
        self.provider = provider

    def get_state(self, entity):
        if entity == "sensor.outdoor":
            return {
                "state": str(self.actual),
                "last_updated": (NOW - timedelta(minutes=self.age_minutes)).isoformat(),
            }
        return {"state": "0"}

    def get_hourly_weather(self, entity):
        return [
            {"datetime": (NOW + timedelta(hours=i)).isoformat(), "temperature": self.provider}
            for i in range(72)
        ]


def cfg(hours=4.0):
    return ashp.Config(
        "sensor.ch", "sensor.outdoor", "weather.test",
        winter_mode_below_c=13.0,
        summer_mode_above_c=14.0,
        forecast_hours=4,
        weather_anchor_hours=hours,
    )


def points():
    return [(NOW + timedelta(hours=i), 14.7) for i in range(10)]


def test_anchor_correction_and_decay():
    offset = ashp.weather_anchor_offset(Client(), cfg(), points(), NOW)
    assert offset == pytest.approx(-1.6)
    assert ashp.anchored_temperature(14.7, offset, NOW, NOW, 4) == pytest.approx(13.1)
    assert ashp.anchored_temperature(14.7, offset, NOW + timedelta(hours=2), NOW, 4) == pytest.approx(13.9)
    assert ashp.anchored_temperature(14.7, offset, NOW + timedelta(hours=4), NOW, 4) == pytest.approx(14.7)


@pytest.mark.parametrize("actual,age", [("unknown", 1), ("nan", 1), (13.1, 91), (13.1, -2), (8.0, 1)])
def test_anchor_rejects_bad_readings(actual, age):
    assert ashp.weather_anchor_offset(Client(actual=actual, age_minutes=age), cfg(), points(), NOW) is None


def test_disabled_anchor():
    assert ashp.weather_anchor_offset(Client(), cfg(0), points(), NOW) is None


def test_anchor_uses_utc_elapsed_time_across_autumn_dst():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 25, 0, 30, tzinfo=tz)
    # 01:30 GMT is two elapsed hours after 00:30 BST.
    cursor = datetime(2026, 10, 25, 1, 30, tzinfo=tz, fold=1)
    assert ashp.anchored_temperature(14.7, -1.6, cursor, now, 4) == pytest.approx(13.9)


def test_anchored_forecast_retains_heating_until_threshold(monkeypatch):
    client = Client()
    config = cfg()
    monkeypatch.setattr(ashp, "build_dhw_forecast", lambda client, store, cfg, starts: [0.0] * len(starts))
    monkeypatch.setattr(ashp, "infer_current_heating_mode", lambda *args: "winter")
    # Test deterministic elapsed time by patching the module clock.
    actual_datetime = ashp.datetime
    class Clock(actual_datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(ashp, "datetime", Clock)
    forecast = ashp.build_forecast(client, None, config, 1.15, timezone.utc)
    assert len(forecast) == 8
    assert forecast[0]["heating_mode"] == "winter"
    assert forecast[0]["ch_kwh"] > 0
    assert forecast[0]["temperature_c"] == pytest.approx(13.1)
    assert any(x["heating_mode"] == "summer" for x in forecast[5:])
    assert all(x["ch_kwh"] == 0 for x in forecast if x["heating_mode"] == "summer")


def test_anchoring_disabled_preserves_provider_forecast(monkeypatch):
    client = Client()
    config = cfg(0)
    monkeypatch.setattr(ashp, "build_dhw_forecast", lambda client, store, cfg, starts: [0.0] * len(starts))
    monkeypatch.setattr(ashp, "infer_current_heating_mode", lambda *args: "winter")
    actual_datetime = ashp.datetime
    class Clock(actual_datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(ashp, "datetime", Clock)
    forecast = ashp.build_forecast(client, None, config, 1.15, timezone.utc)
    assert forecast[0]["heating_mode"] == "summer"
    assert forecast[0]["ch_kwh"] == 0
    assert forecast[0]["temperature_c"] == pytest.approx(14.7)
