import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location("dhw_shadow_runner_test", APP / "dhw_shadow_runner.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def slot(start, dhw, draw, upper, lower):
    return SimpleNamespace(
        start=start,
        dhw_kwh=dhw,
        draw_kwh=draw,
        upper_temp_c=upper,
        lower_temp_c=lower,
    )


def test_future_publication_uses_complete_clock_aligned_half_hours():
    tz = ZoneInfo("Europe/London")
    start = datetime(2026, 9, 9, 13, 35, tzinfo=tz)
    slots = [
        slot(start + timedelta(minutes=5 * i), 0.1, 0.02, 50.0 + i * 0.1, 45.0 + i * 0.1)
        for i in range(12)
    ]
    production = [datetime(2026, 9, 9, 14, 0, tzinfo=tz)]

    rows, complete = module._published_half_hours(slots, production)

    assert complete is False
    assert len(rows) == 1
    assert rows[0]["start"] == production[0].isoformat()
    assert rows[0]["dhw_kwh"] == 0.6
    assert rows[0]["forecast_remaining_dhw_kwh"] == 0.6
    assert rows[0]["actual_dhw_kwh"] == 0.0
    assert rows[0]["predicted_draw_kwh"] == 0.12
    assert rows[0]["dhw_active"] is True
    assert rows[0]["current_slot"] is False


def test_current_slot_combines_actual_so_far_with_forecast_remainder():
    tz = ZoneInfo("Europe/London")
    production = datetime(2026, 9, 14, 8, 30, tzinfo=tz)
    now = datetime(2026, 9, 14, 8, 39, tzinfo=tz)
    simulation_start = datetime(2026, 9, 14, 8, 40, tzinfo=tz)
    slots = [
        slot(simulation_start + timedelta(minutes=5 * i), 0.1, 0.02, 50.0 + i * 0.1, 45.0 + i * 0.1)
        for i in range(4)
    ]

    rows, complete = module._published_half_hours(
        slots,
        [production],
        now=now,
        actual_current_dhw_kwh=0.25,
        current_upper_c=49.5,
        current_lower_c=44.5,
    )

    assert complete is False
    assert len(rows) == 1
    assert rows[0]["start"] == production.isoformat()
    assert rows[0]["actual_dhw_kwh"] == 0.25
    assert rows[0]["forecast_remaining_dhw_kwh"] == 0.4
    assert rows[0]["dhw_kwh"] == 0.65
    assert rows[0]["current_slot"] is True
    assert rows[0]["actual_through"] == now.isoformat()


def test_current_slot_at_exact_boundary_is_all_forecast():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 9, 14, 8, 30, tzinfo=tz)
    slots = [
        slot(now + timedelta(minutes=5 * i), 0.1, 0.0, 50.0, 45.0)
        for i in range(6)
    ]
    rows, _ = module._published_half_hours(slots, [now], now=now)
    assert rows[0]["actual_dhw_kwh"] == 0.0
    assert rows[0]["forecast_remaining_dhw_kwh"] == 0.6
    assert rows[0]["dhw_kwh"] == 0.6


def test_current_slot_near_end_can_be_actual_only():
    tz = ZoneInfo("Europe/London")
    production = datetime(2026, 9, 14, 8, 30, tzinfo=tz)
    now = datetime(2026, 9, 14, 8, 59, 59, tzinfo=tz)
    rows, _ = module._published_half_hours(
        [], [production], now=now, actual_current_dhw_kwh=0.7,
        current_upper_c=55.0, current_lower_c=50.0,
    )
    assert rows[0]["dhw_kwh"] == 0.7
    assert rows[0]["forecast_remaining_dhw_kwh"] == 0.0
    assert rows[0]["upper_temperature_c"] == 55.0


def test_measured_current_slot_dhw_energy_is_summed_from_thermal_samples():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_thermal_samples (timestamp TEXT, valid INTEGER, dhw_energy_delta_kwh REAL)"
    )
    db.executemany(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?)",
        [
            ("2026-09-14T08:29:59+01:00", 1, 0.5),
            ("2026-09-14T08:35:00+01:00", 1, 0.10),
            ("2026-09-14T08:38:00+01:00", 1, 0.15),
            ("2026-09-14T08:39:00+01:00", 0, 9.0),
            ("2026-09-14T08:40:00+01:00", 1, 0.20),
        ],
    )
    start = datetime.fromisoformat("2026-09-14T08:30:00+01:00")
    end = datetime.fromisoformat("2026-09-14T08:39:30+01:00")
    assert module._actual_dhw_since(db, start, end) == 0.25


def test_thermal_publication_is_the_dhw_model_entity():
    assert module.THERMAL_FORECAST_ENTITY == "sensor.ashp_dhw_thermal_forecast_next_48h"
    assert not hasattr(module, "LEGACY_FORECAST_ENTITY")
    assert not hasattr(module, "COMPARISON_ENTITY")


def test_production_always_starts_at_current_half_hour():
    tz = ZoneInfo("Europe/London")
    cases = [
        (datetime(2026, 9, 14, 6, 29, 59, tzinfo=tz), datetime(2026, 9, 14, 6, 0, tzinfo=tz)),
        (datetime(2026, 9, 14, 6, 30, 0, tzinfo=tz), datetime(2026, 9, 14, 6, 30, tzinfo=tz)),
        (datetime(2026, 9, 14, 6, 30, 6, tzinfo=tz), datetime(2026, 9, 14, 6, 30, tzinfo=tz)),
        (datetime(2026, 9, 14, 6, 31, 1, tzinfo=tz), datetime(2026, 9, 14, 6, 30, tzinfo=tz)),
        (datetime(2026, 9, 14, 6, 59, 59, tzinfo=tz), datetime(2026, 9, 14, 6, 30, tzinfo=tz)),
    ]
    for now, expected in cases:
        assert module._production_starts(now)[0] == expected


def test_simulation_starts_from_next_thermal_step_not_past_slot_state():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 9, 14, 6, 39, 1, tzinfo=tz)
    production = module._production_starts(now)
    assert production[0] == datetime(2026, 9, 14, 6, 30, tzinfo=tz)
    assert module._simulation_start(now, production) == datetime(2026, 9, 14, 6, 40, tzinfo=tz)


def test_boundary_alignment_is_unambiguous_across_dst_fall_back():
    tz = ZoneInfo("Europe/London")
    for fold in (0, 1):
        now = datetime(2026, 10, 25, 1, 30, 6, tzinfo=tz, fold=fold)
        production = module._production_starts(now)
        assert production[0].fold == fold
        assert production[0].timestamp() <= now.timestamp()
        assert module._simulation_start(now, production).timestamp() >= now.timestamp()


def test_boundary_alignment_survives_dst_spring_forward():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 3, 29, 2, 30, 6, tzinfo=tz)
    production = module._production_starts(now)
    assert production[0].timestamp() <= now.timestamp()
    assert module._simulation_start(now, production).timestamp() >= now.timestamp()
