"""ASHP thermal and Home Energy forecast slot alignment across DST."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from common.forecast_slots import production_slot_start
from components.ashp_forecaster.app.dhw_production_selector import _thermal_values

TZ=ZoneInfo("Europe/London")
UTC=timezone.utc


@pytest.mark.parametrize("date",[
    "2026-10-23","2026-10-24","2026-10-25","2026-10-26",
    "2027-03-26","2027-03-27","2027-03-28","2027-03-29"])
def test_96_slot_ashp_horizon_across_all_dst_boundaries(date):
    # Include earlier-day forecasts, not only the instant clocks switch.
    now=datetime.fromisoformat(date+"T10:13:00").replace(tzinfo=TZ)
    first=production_slot_start(now)
    starts=[(first.astimezone(UTC)+timedelta(minutes=i*30)).astimezone(TZ)
            for i in range(96)]
    assert len({p.astimezone(UTC) for p in starts})==96
    assert all((b.astimezone(UTC)-a.astimezone(UTC))==timedelta(minutes=30)
               for a,b in zip(starts,starts[1:]))
    state={"attributes":{
        "status":"published_shadow","horizon_complete":True,
        "forecast_slots":96,"last_updated":datetime.now(UTC).isoformat(),
        "forecast":[{"start":s.isoformat(),"dhw_kwh":0.12} for s in starts]}}
    values,reason=_thermal_values(state,starts,max_age_minutes=25)
    assert reason=="thermal_ready"
    assert len(values)==96


@pytest.mark.parametrize("date",["2026-10-24","2027-03-27"])
def test_ashp_partial_forecast_is_rejected_not_silently_filled(date):
    now=datetime.fromisoformat(date+"T21:30:00").replace(tzinfo=TZ)
    first=production_slot_start(now)
    starts=[(first.astimezone(UTC)+timedelta(minutes=i*30)).astimezone(TZ)
            for i in range(96)]
    state={"attributes":{
        "status":"published_shadow","horizon_complete":True,
        "forecast_slots":96,"last_updated":datetime.now(UTC).isoformat(),
        "forecast":[{"start":s.isoformat(),"dhw_kwh":0.15} for s in starts[:33]]}}
    values,reason=_thermal_values(state,starts,max_age_minutes=25)
    assert values is None
    assert "incomplete" in reason
