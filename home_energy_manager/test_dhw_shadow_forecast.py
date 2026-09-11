import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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
        intercept_kwh=0.2, upper_kwh_per_c=0.04, lower_kwh_per_c=0.08,
        typical_power_kw=2.0, upper_c_per_kwh=4.0, lower_c_per_kwh=6.0,
        upper_loss_w_per_k=1.2, lower_loss_w_per_k=0.8, coupling_w_per_k=3.0,
        demand=demand or {},
    )


def validation_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE dhw_forecast_validation(forecast_ts TEXT,target_ts TEXT,predicted_upper_c REAL,predicted_lower_c REAL,predicted_dhw_kwh REAL,legacy_dhw_kwh REAL,actual_dhw_kwh REAL,actual_upper_c REAL,actual_lower_c REAL,model_source TEXT,PRIMARY KEY(forecast_ts,target_ts))")
    return db


def test_shadow_forecast_always_covers_full_48_hours():
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=50.0, initial_lower_c=45.0, target_temp_c=50.0, hysteresis_c=5.0, mode="off", schedule_bits={}, model=base_model())
    assert len(result) == 576


def test_hot_control_sensor_does_not_trigger_unnecessary_cycle():
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=55.0, initial_lower_c=48.0, target_temp_c=50.0, hysteresis_c=5.0, mode="on", schedule_bits={}, model=base_model(), target_sensor="lower", horizon_hours=2)
    assert sum(slot.dhw_kwh for slot in result) == 0.0


def test_cold_tank_heats_until_configured_lower_target_sensor_reaches_target():
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=40.0, initial_lower_c=30.0, target_temp_c=50.0, hysteresis_c=5.0, mode="on", schedule_bits={}, model=base_model(), target_sensor="lower", horizon_hours=2)
    heated = [slot for slot in result if slot.dhw_kwh > 0.0]
    assert heated
    assert any(abs(slot.lower_temp_c - 50.0) < 1e-5 for slot in heated)


def test_upper_reaching_target_does_not_stop_lower_control_sensor_cycle():
    model = mod.ShadowModel(
        intercept_kwh=0.0, upper_kwh_per_c=0.0, lower_kwh_per_c=0.0,
        typical_power_kw=3.0, upper_c_per_kwh=10.0, lower_c_per_kwh=2.0,
        upper_loss_w_per_k=0.0, lower_loss_w_per_k=0.0, coupling_w_per_k=0.0, demand={},
    )
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=49.0, initial_lower_c=40.0, target_temp_c=50.0, hysteresis_c=5.0, mode="on", schedule_bits={}, model=model, target_sensor="lower", horizon_hours=2)
    heated = [slot for slot in result if slot.heating]
    assert heated
    assert heated[0].upper_temp_c >= 50.0
    assert heated[0].lower_temp_c < 50.0
    assert len(heated) > 1
    assert abs(heated[-1].lower_temp_c - 50.0) < 1e-5
    assert heated[-1].upper_temp_c > 50.0


def test_cycle_energy_regression_cannot_stop_heating_below_control_target():
    model = mod.ShadowModel(
        intercept_kwh=0.01, upper_kwh_per_c=0.0, lower_kwh_per_c=0.0,
        typical_power_kw=2.0, upper_c_per_kwh=4.0, lower_c_per_kwh=4.0,
        upper_loss_w_per_k=0.0, lower_loss_w_per_k=0.0, coupling_w_per_k=0.0, demand={},
    )
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=40.0, initial_lower_c=40.0, target_temp_c=50.0, hysteresis_c=5.0, mode="on", schedule_bits={}, model=model, target_sensor="lower", horizon_hours=2)
    used = sum(slot.dhw_kwh for slot in result)
    assert used > model.cycle_energy(40.0, 40.0, 50.0)
    assert any(abs(slot.lower_temp_c - 50.0) < 1e-5 for slot in result)


def test_target_seeking_cycle_uses_partial_final_step_for_lower_sensor():
    model = mod.ShadowModel(
        intercept_kwh=0.0, upper_kwh_per_c=0.0, lower_kwh_per_c=0.0,
        typical_power_kw=3.0, upper_c_per_kwh=5.0, lower_c_per_kwh=5.0,
        upper_loss_w_per_k=0.0, lower_loss_w_per_k=0.0, coupling_w_per_k=0.0, demand={},
    )
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=49.0, initial_lower_c=49.0, target_temp_c=50.0, hysteresis_c=2.0, mode="on", schedule_bits={}, model=model, target_sensor="lower", horizon_hours=1)
    assert sum(slot.dhw_kwh for slot in result) == 0.0
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=47.0, initial_lower_c=47.0, target_temp_c=50.0, hysteresis_c=2.0, mode="on", schedule_bits={}, model=model, target_sensor="lower", horizon_hours=1)
    assert abs(sum(slot.dhw_kwh for slot in result if slot.heating) - 0.6) < 1e-4
    assert any(abs(slot.lower_temp_c - 50.0) < 1e-5 for slot in result)


def test_shadow_forecast_never_leaves_lower_zone_above_upper_after_heating():
    aggressive_lower_response = mod.ShadowModel(
        intercept_kwh=0.2, upper_kwh_per_c=0.04, lower_kwh_per_c=0.08,
        typical_power_kw=3.0, upper_c_per_kwh=1.0, lower_c_per_kwh=12.0,
        upper_loss_w_per_k=0.0, lower_loss_w_per_k=0.0, coupling_w_per_k=0.0, demand={},
    )
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=44.0, initial_lower_c=43.0, target_temp_c=50.0, hysteresis_c=5.0, mode="on", schedule_bits={}, model=aggressive_lower_response, target_sensor="lower", horizon_hours=1)
    assert any(slot.dhw_kwh > 0 for slot in result)
    assert all(slot.lower_temp_c <= slot.upper_temp_c + 1e-9 for slot in result)


def test_schedule_mode_only_heats_in_enabled_half_hour():
    schedule = {"wednesday_pm": 1}
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=40.0, initial_lower_c=30.0, target_temp_c=50.0, hysteresis_c=5.0, mode="schedule", schedule_bits=schedule, model=base_model(), target_sensor="lower", horizon_hours=1)
    assert sum(slot.dhw_kwh for slot in result[:6]) > 0.0
    assert sum(slot.dhw_kwh for slot in result[6:]) == 0.0


def test_expected_draw_is_distributed_across_half_hour():
    demand = {("weekday", 24): 1.2}
    result = mod.build_shadow_forecast(start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc), initial_upper_c=50.0, initial_lower_c=45.0, target_temp_c=50.0, hysteresis_c=5.0, mode="off", schedule_bits={}, model=base_model(demand), horizon_hours=0.5)
    assert abs(sum(slot.draw_kwh for slot in result) - 1.2) < 1e-9


def test_shadow_model_load_requires_cycle_learning_but_can_use_default_passive_values():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE dhw_model_parameters(name TEXT,value REAL)")
    db.execute("CREATE TABLE dhw_demand_profile(day_type TEXT,slot_index INTEGER,expected_kwh REAL)")
    assert mod.load_shadow_model(db) is None
    values = {
        "dhw_cycle_intercept_kwh": 0.2, "dhw_cycle_upper_kwh_per_c": 0.04,
        "dhw_cycle_lower_kwh_per_c": 0.08, "dhw_cycle_power_kw": 2.0,
        "dhw_cycle_upper_c_per_kwh": 4.0, "dhw_cycle_lower_c_per_kwh": 6.0,
    }
    db.executemany("INSERT INTO dhw_model_parameters VALUES(?,?)", values.items())
    loaded = mod.load_shadow_model(db)
    assert loaded is not None
    assert loaded.upper_loss_w_per_k == 1.2
    assert loaded.lower_loss_w_per_k == 0.8


def test_validation_persists_matching_legacy_half_hour_value():
    db = validation_db()
    start = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    slots = [mod.ShadowSlot(start + timedelta(minutes=5 * idx), 50.0, 42.0, 0.0, 0.1, True) for idx in range(6)]
    written = mod.persist_shadow_validation(db, slots, forecast_ts=start - timedelta(minutes=1), legacy_dhw_by_start={start.isoformat(): 0.8})
    assert written == 1
    row = db.execute("SELECT target_ts,predicted_dhw_kwh,legacy_dhw_kwh FROM dhw_forecast_validation").fetchone()
    assert row[0] == (start + timedelta(minutes=30)).isoformat()
    assert abs(row[1] - 0.6) < 1e-9
    assert row[2] == 0.8


def test_validation_skips_partial_start_and_uses_next_complete_clock_half_hour():
    db = validation_db()
    start = datetime(2026, 9, 9, 13, 35, tzinfo=timezone.utc)
    slots = [mod.ShadowSlot(start + timedelta(minutes=5 * idx), 50.0, 42.0, 0.0, 0.1, True) for idx in range(11)]
    complete_start = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    written = mod.persist_shadow_validation(db, slots, forecast_ts=start - timedelta(minutes=1), legacy_dhw_by_start={complete_start.isoformat(): 0.7})
    assert written == 1
    row = db.execute("SELECT target_ts,legacy_dhw_kwh FROM dhw_forecast_validation").fetchone()
    assert row == ((complete_start + timedelta(minutes=30)).isoformat(), 0.7)
