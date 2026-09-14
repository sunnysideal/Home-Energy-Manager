import importlib.util
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


def test_publication_uses_only_complete_clock_aligned_half_hours():
    tz = ZoneInfo("Europe/London")
    start = datetime(2026, 9, 9, 13, 35, tzinfo=tz)
    slots = [
        slot(start + timedelta(minutes=5 * i), 0.1, 0.02, 50.0 + i * 0.1, 45.0 + i * 0.1)
        for i in range(12)
    ]
    production = [datetime(2026, 9, 9, 14, 0, tzinfo=tz)]

    rows, complete = module._published_half_hours(slots, production)

    assert complete is False  # one valid row, not the complete 96-slot production horizon
    assert len(rows) == 1
    assert rows[0]["start"] == production[0].isoformat()
    assert rows[0]["dhw_kwh"] == 0.6
    assert rows[0]["predicted_draw_kwh"] == 0.12
    assert rows[0]["dhw_active"] is True


def test_publication_preserves_temperature_and_energy_fields():
    tz = ZoneInfo("Europe/London")
    start = datetime(2026, 9, 9, 14, 0, tzinfo=tz)
    slots = [
        slot(start + timedelta(minutes=5 * i), 0.0, 0.0, 52.0 - i * 0.1, 47.0 - i * 0.1)
        for i in range(6)
    ]

    rows, _ = module._published_half_hours(slots, [start])
    row = rows[0]

    assert set(row) == {
        "start",
        "dhw_active",
        "dhw_kwh",
        "predicted_draw_kwh",
        "upper_temperature_c",
        "lower_temperature_c",
    }
    assert row["dhw_kwh"] == 0.0
    assert row["dhw_active"] is False
    assert row["upper_temperature_c"] == 51.5
    assert row["lower_temperature_c"] == 46.5


def test_thermal_publication_is_the_dhw_model_entity():
    assert module.THERMAL_FORECAST_ENTITY == "sensor.ashp_dhw_thermal_forecast_next_48h"
    assert not hasattr(module, "LEGACY_FORECAST_ENTITY")
    assert not hasattr(module, "COMPARISON_ENTITY")


def test_simulation_start_covers_production_boundary_during_grace_window():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 9, 14, 6, 30, 6, tzinfo=tz)
    production = module._production_starts(now)

    assert production[0] == datetime(2026, 9, 14, 6, 30, tzinfo=tz)
    assert module._ceil_local(now, 5) == datetime(2026, 9, 14, 6, 35, tzinfo=tz)
    assert module._simulation_start(now, production) == production[0]


def test_simulation_start_uses_next_thermal_step_outside_boundary_grace():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 9, 14, 6, 31, 1, tzinfo=tz)
    production = module._production_starts(now)

    assert production[0] == datetime(2026, 9, 14, 7, 0, tzinfo=tz)
    assert module._simulation_start(now, production) == datetime(2026, 9, 14, 6, 35, tzinfo=tz)


def test_boundary_cases_keep_simulation_at_or_before_first_production_slot():
    tz = ZoneInfo("Europe/London")
    cases = [
        datetime(2026, 9, 14, 6, 29, 59, tzinfo=tz),
        datetime(2026, 9, 14, 6, 30, 0, tzinfo=tz),
        datetime(2026, 9, 14, 6, 30, 1, tzinfo=tz),
        datetime(2026, 9, 14, 6, 30, 30, tzinfo=tz),
        datetime(2026, 9, 14, 6, 31, 1, tzinfo=tz),
    ]
    for now in cases:
        production = module._production_starts(now)
        simulation = module._simulation_start(now, production)
        assert simulation.timestamp() <= production[0].timestamp()


def test_boundary_alignment_is_unambiguous_across_dst_fall_back():
    tz = ZoneInfo("Europe/London")
    # The repeated 01:30 local boundary occurs twice; timestamp comparison must keep
    # each production/simulation pair aligned to its own UTC instant.
    for fold in (0, 1):
        now = datetime(2026, 10, 25, 1, 30, 6, tzinfo=tz, fold=fold)
        production = module._production_starts(now)
        simulation = module._simulation_start(now, production)
        assert simulation.timestamp() == production[0].timestamp()


def test_boundary_alignment_survives_dst_spring_forward():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 3, 29, 2, 30, 6, tzinfo=tz)
    production = module._production_starts(now)
    simulation = module._simulation_start(now, production)
    assert simulation.timestamp() == production[0].timestamp()
