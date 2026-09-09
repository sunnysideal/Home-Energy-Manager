import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_demand_learner.py"

spec = importlib.util.spec_from_file_location("dhw_demand_learner", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def db_with_schema():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE dhw_thermal_samples(timestamp TEXT, valid INTEGER)")
    db.execute(
        "CREATE TABLE dhw_draw_events(timestamp TEXT,estimated_thermal_kwh REAL,confidence REAL)"
    )
    db.execute(
        "CREATE TABLE dhw_demand_profile("
        "day_type TEXT,slot_index INTEGER,expected_kwh REAL,probability REAL,typical_kwh REAL,"
        "sample_days INTEGER,updated_at TEXT,PRIMARY KEY(day_type,slot_index))"
    )
    return db


def add_observed_day(db, day, *, sample_minutes=30):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    for idx in range((8 * 60) // sample_minutes):
        db.execute(
            "INSERT INTO dhw_thermal_samples VALUES(?,1)",
            ((start + timedelta(minutes=idx * sample_minutes)).isoformat(),),
        )


def add_draw(db, when, energy=1.0, confidence=0.8):
    db.execute(
        "INSERT INTO dhw_draw_events VALUES(?,?,?)",
        (when.isoformat(), energy, confidence),
    )


def test_zero_use_days_reduce_expected_energy():
    db = db_with_schema()
    # Three observed weekdays, but only one has a 07:00 draw.
    days = [datetime(2026, 9, 7).date(), datetime(2026, 9, 8).date(), datetime(2026, 9, 9).date()]
    for day in days:
        add_observed_day(db, day, sample_minutes=30)
    add_draw(db, datetime(2026, 9, 8, 7, 5, tzinfo=timezone.utc), energy=1.5)

    profile = mod.learn_demand_profile(
        db, timezone_name="UTC", sample_minutes=30, minimum_observed_days=3, history_days=365,
    )
    slot = next(x for x in profile if x.day_type == "weekday" and x.slot_index == 14)
    assert abs(slot.probability - (1 / 3)) < 1e-9
    assert abs(slot.typical_kwh - 1.5) < 1e-9
    assert abs(slot.expected_kwh - 0.5) < 1e-9


def test_weekend_and_weekday_profiles_are_separate():
    db = db_with_schema()
    weekday_days = [datetime(2026, 9, 7).date(), datetime(2026, 9, 8).date(), datetime(2026, 9, 9).date()]
    weekend_days = [datetime(2026, 9, 5).date(), datetime(2026, 9, 6).date(), datetime(2026, 9, 12).date()]
    for day in weekday_days + weekend_days:
        add_observed_day(db, day, sample_minutes=30)
    for day in weekend_days:
        add_draw(db, datetime(day.year, day.month, day.day, 9, 5, tzinfo=timezone.utc), energy=2.0)

    profile = mod.learn_demand_profile(
        db, timezone_name="UTC", sample_minutes=30, minimum_observed_days=3, history_days=365,
    )
    weekday = next(x for x in profile if x.day_type == "weekday" and x.slot_index == 18)
    weekend = next(x for x in profile if x.day_type == "weekend" and x.slot_index == 18)
    assert weekday.expected_kwh == 0.0
    assert weekend.expected_kwh == 2.0


def test_local_timezone_controls_slot_assignment():
    db = db_with_schema()
    days = [datetime(2026, 9, 7).date(), datetime(2026, 9, 8).date(), datetime(2026, 9, 9).date()]
    for day in days:
        add_observed_day(db, day, sample_minutes=30)
    # 06:05 UTC in September is 07:05 Europe/London -> local slot 14.
    add_draw(db, datetime(2026, 9, 8, 6, 5, tzinfo=timezone.utc), energy=1.2)

    profile = mod.learn_demand_profile(
        db, timezone_name="Europe/London", sample_minutes=30, minimum_observed_days=3, history_days=365,
    )
    slot = next(x for x in profile if x.day_type == "weekday" and x.slot_index == 14)
    assert slot.probability > 0.0


def test_persist_profile_upserts_slots():
    db = db_with_schema()
    slots = [mod.DemandSlot("weekday", 14, 0.5, 0.25, 2.0, 8)]
    mod.persist_demand_profile(db, slots)
    mod.persist_demand_profile(db, [mod.DemandSlot("weekday", 14, 0.6, 0.3, 2.0, 10)])
    row = db.execute(
        "SELECT expected_kwh,probability,sample_days FROM dhw_demand_profile "
        "WHERE day_type='weekday' AND slot_index=14"
    ).fetchone()
    assert row == (0.6, 0.3, 10)
