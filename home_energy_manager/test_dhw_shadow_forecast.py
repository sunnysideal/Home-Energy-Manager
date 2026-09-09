import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"

for name in ("dhw_simulator", "dhw_shadow_forecast"):
    path = APP / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

mod = sys.modules["dhw_shadow_forecast"]


def base_model(demand=None):
    return mod.ShadowModel(
        intercept_kwh=0.2,
        upper_kwh_per_c=0.04,
        lower_kwh_per_c=0.08,
        typical_power_kw=2.0,
        upper_c_per_kwh=4.0,
        lower_c_per_kwh=6.0,
        upper_loss_w_per_k=1.2,
        lower_loss_w_per_k=0.8,
        coupling_w_per_k=3.0,
        demand=demand or {},
    )


def test_shadow_forecast_always_covers_full_48_hours():
    result = mod.build_shadow_forecast(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        initial_upper_c=50.0,
        initial_lower_c=45.0,
        target_temp_c=50.0,
        hysteresis_c=5.0,
        mode="off",
        schedule_bits={},
        model=base_model(),
    )
    assert len(result) == 576


def test_hot_tank_does_not_trigger_unnecessary_cycle():
    result = mod.build_shadow_forecast(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        initial_upper_c=50.0,
        initial_lower_c=48.0,
        target_temp_c=50.0,
        hysteresis_c=5.0,
        mode="on",
        schedule_bits={},
        model=base_model(),
        horizon_hours=2,
    )
    assert sum(slot.dhw_kwh for slot in result) == 0.0


def test_cold_tank_triggers_cycle_when_mode_on():
    result = mod.build_shadow_forecast(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        initial_upper_c=40.0,
        initial_lower_c=30.0,
        target_temp_c=50.0,
        hysteresis_c=5.0,
        mode="on",
        schedule_bits={},
        model=base_model(),
        horizon_hours=2,
    )
    assert sum(slot.dhw_kwh for slot in result) > 0.0
    assert result[-1].upper_temp_c > 40.0
    assert result[-1].lower_temp_c > 30.0


def test_schedule_mode_only_heats_in_enabled_half_hour():
    # Wednesday 12:00 local is PM bit 0 (absolute half-hour 24 -> PM bit 0).
    schedule = {"wednesday_pm": 1}
    result = mod.build_shadow_forecast(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        initial_upper_c=40.0,
        initial_lower_c=30.0,
        target_temp_c=50.0,
        hysteresis_c=5.0,
        mode="schedule",
        schedule_bits=schedule,
        model=base_model(),
        horizon_hours=1,
    )
    first_half = sum(slot.dhw_kwh for slot in result[:6])
    second_half = sum(slot.dhw_kwh for slot in result[6:])
    assert first_half > 0.0
    assert second_half == 0.0


def test_expected_draw_is_distributed_across_half_hour():
    demand = {("weekday", 24): 1.2}
    result = mod.build_shadow_forecast(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        initial_upper_c=50.0,
        initial_lower_c=45.0,
        target_temp_c=50.0,
        hysteresis_c=5.0,
        mode="off",
        schedule_bits={},
        model=base_model(demand),
        horizon_hours=0.5,
    )
    assert abs(sum(slot.draw_kwh for slot in result) - 1.2) < 1e-9


def test_shadow_model_load_requires_cycle_learning_but_can_use_default_passive_values():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE dhw_model_parameters(name TEXT,value REAL)")
    db.execute("CREATE TABLE dhw_demand_profile(day_type TEXT,slot_index INTEGER,expected_kwh REAL)")
    assert mod.load_shadow_model(db) is None
    values = {
        "dhw_cycle_intercept_kwh": 0.2,
        "dhw_cycle_upper_kwh_per_c": 0.04,
        "dhw_cycle_lower_kwh_per_c": 0.08,
        "dhw_cycle_power_kw": 2.0,
        "dhw_cycle_upper_c_per_kwh": 4.0,
        "dhw_cycle_lower_c_per_kwh": 6.0,
    }
    db.executemany("INSERT INTO dhw_model_parameters VALUES(?,?)", values.items())
    loaded = mod.load_shadow_model(db)
    assert loaded is not None
    assert loaded.upper_loss_w_per_k == 1.2
    assert loaded.lower_loss_w_per_k == 0.8
