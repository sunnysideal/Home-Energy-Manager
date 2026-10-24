"""Real DHW thermal simulation through UK clock changes, including publication and validation."""
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from test_dhw_shadow_forecast import mod, base_model, validation_db
from dhw_shadow_runner import (_production_starts, _simulation_start,
                               _required_simulation_hours, _published_half_hours,
                               PRODUCTION_COVERAGE_SLOTS)

LONDON=ZoneInfo("Europe/London")
UTC=timezone.utc


@pytest.mark.parametrize("instant", [
    "2026-10-23T11:00:00+00:00",
    "2026-10-24T10:30:00+00:00",
    "2026-10-24T22:20:00+00:00",
    "2026-10-25T00:35:00+00:00",
    "2026-10-25T01:35:00+00:00",
    "2026-10-26T10:20:00+00:00",
    "2027-03-26T11:00:00+00:00",
    "2027-03-27T10:30:00+00:00",
    "2027-03-27T22:20:00+00:00",
    "2027-03-28T00:35:00+00:00",
    "2027-03-28T01:35:00+00:00",
    "2027-03-29T10:20:00+00:00",
])
def test_real_simulation_produces_full_dst_thermal_forecast(instant):
    now=datetime.fromisoformat(instant).astimezone(LONDON)
    production=_production_starts(now)
    start=_simulation_start(now,production)
    hours=_required_simulation_hours(start,production)
    slots=mod.build_shadow_forecast(
        start=start,initial_upper_c=53.2,initial_lower_c=49.9,
        target_temp_c=55,hysteresis_c=5,mode="off",schedule_bits={},
        model=base_model(),horizon_hours=hours,step_minutes=5)
    times=[s.start.astimezone(UTC) for s in slots]
    assert len(times)==len(set(times))
    assert all(b-a==timedelta(minutes=5) for a,b in zip(times,times[1:]))
    published,complete=_published_half_hours(
        slots,production,now=now,current_upper_c=53.2,current_lower_c=49.9)
    assert complete, (instant,len(published),len(slots))
    assert len(published)==PRODUCTION_COVERAGE_SLOTS==97
    assert [datetime.fromisoformat(r["start"]).astimezone(UTC) for r in published]==[
        dt.astimezone(UTC) for dt in production]
    db=validation_db()
    checkpoint_count=mod.persist_shadow_validation(db,slots,
        forecast_ts=datetime(2026,10,24,10,0,tzinfo=UTC))
    assert checkpoint_count>=95
    actual=db.execute("SELECT COUNT(DISTINCT target_ts) FROM dhw_forecast_validation").fetchone()[0]
    assert actual==checkpoint_count


def test_repeated_autumn_hour_is_retained_as_four_distinct_half_hours():
    now=datetime(2026,10,24,22,20,tzinfo=UTC).astimezone(LONDON)
    production=_production_starts(now)
    start=_simulation_start(now,production)
    slots=mod.build_shadow_forecast(
        start=start,initial_upper_c=53.2,initial_lower_c=49.9,
        target_temp_c=55,hysteresis_c=5,mode="off",schedule_bits={},
        model=base_model(),horizon_hours=_required_simulation_hours(start,production))
    db=validation_db()
    count=mod.persist_shadow_validation(db,slots)
    keys=[datetime.fromisoformat(r[0]).astimezone(LONDON)
          for r in db.execute("SELECT target_ts FROM dhw_forecast_validation")]
    repeated=[k for k in keys if k.date().isoformat()=="2026-10-25" and k.hour==1]
    assert len(repeated)==4
    assert {k.fold for k in repeated}=={0,1}
    assert count>=95
