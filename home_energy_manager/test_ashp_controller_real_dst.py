"""Regression tests against actual ASHP horizon generator and controller DST charge solver."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from components.ashp_forecaster.app.main import forecast_slot_starts, interpolate_temperature
from components.ashp_forecaster.app.dhw_production_selector import _thermal_values
from common.forecast_slots import production_slot_start

TZ=ZoneInfo("Europe/London")
UTC=timezone.utc

@pytest.mark.parametrize("instant",[
    "2026-10-23T10:13:00+00:00","2026-10-24T12:13:00+00:00",
    "2026-10-25T00:13:00+00:00","2026-10-25T01:13:00+00:00",
    "2026-10-26T12:13:00+00:00","2027-03-26T12:13:00+00:00",
    "2027-03-27T12:13:00+00:00","2027-03-28T00:13:00+00:00",
    "2027-03-28T01:13:00+00:00","2027-03-29T12:13:00+00:00",
])
def test_ashp_real_slot_builder_accepts_thermal_horizon(instant):
    now=datetime.fromisoformat(instant).astimezone(TZ)
    first=production_slot_start(now)
    starts=forecast_slot_starts(now,30,48,
        (first.astimezone(UTC)+timedelta(hours=52)).astimezone(TZ))
    assert len(starts)==96
    assert starts[0].astimezone(UTC)==(first.astimezone(UTC)+timedelta(minutes=30))
    assert all((b.astimezone(UTC)-a.astimezone(UTC))==timedelta(minutes=30)
               for a,b in zip(starts,starts[1:]))
    state={"attributes":{"status":"published_shadow","horizon_complete":True,
        "forecast_slots":97,"last_updated":datetime.now(UTC).isoformat(),
        "forecast":[{"start":(first.astimezone(UTC)+timedelta(minutes=30*i)).astimezone(TZ).isoformat(),
                     "dhw_kwh":0.1} for i in range(97)]}}
    values,reason=_thermal_values(state,starts,max_age_minutes=30)
    assert reason=="thermal_ready"
    assert len(values)==96

def test_autumn_weather_interpolation_uses_utc_order():
    a=datetime.fromisoformat("2026-10-25T01:00:00+01:00").astimezone(TZ)
    b=datetime.fromisoformat("2026-10-25T01:00:00+00:00").astimezone(TZ)
    half=(a.astimezone(UTC)+timedelta(minutes=30)).astimezone(TZ)
    assert interpolate_temperature([(a,8.0),(b,10.0)],half)==9.0
