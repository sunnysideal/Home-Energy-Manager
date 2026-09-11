import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
sys.path.insert(0, str(APP))
MODULE_PATH = APP / "dhw_efficiency_learner.py"
spec = importlib.util.spec_from_file_location("dhw_efficiency_learner", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def db_with_schema():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_thermal_samples("
        "timestamp TEXT PRIMARY KEY,upper_temp_c REAL,lower_temp_c REAL,dhw_heating INTEGER,"
        "immersion_heating INTEGER,dhw_energy_delta_kwh REAL,valid INTEGER)"
    )
    db.execute("CREATE TABLE dhw_draw_events(timestamp TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE dhw_heating_cycles("
        "start_ts TEXT,end_ts TEXT,start_upper_c REAL,start_lower_c REAL,end_upper_c REAL,end_lower_c REAL,"
        "target_temp_c REAL,electrical_kwh REAL,outdoor_temp_c REAL,cycle_type TEXT,valid INTEGER)"
    )
    db.execute(
        "CREATE TABLE dhw_model_parameters("
        "name TEXT PRIMARY KEY,value REAL,sample_count INTEGER,updated_at TEXT,error REAL)"
    )
    return db


def add_heating_series(db):
    start = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    upper = 29.0
    lower = 27.0
    for idx in range(61):
        ts = start + timedelta(minutes=5 * idx)
        effective = (upper + lower) / 2.0
        # Electrical cost per degree deliberately rises with tank temperature.
        cost_per_c = 0.020 + max(0.0, effective - 25.0) * 0.0012
        rise = 0.5
        energy = cost_per_c * rise if idx else 0.0
        db.execute(
            "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,?,?,?)",
            (ts.isoformat(), upper, lower, int(idx > 0), 0, energy, 1),
        )
        upper += rise
        lower += rise
    db.commit()


def test_learns_monotonic_temperature_cost_curve():
    db = db_with_schema()
    add_heating_series(db)
    fit = mod.learn_temperature_efficiency(db)
    assert fit is not None
    assert fit.sample_count >= 20
    assert len(fit.bins) >= 3
    multipliers = [item.multiplier for item in fit.bins]
    assert multipliers == sorted(multipliers)
    assert fit.multiplier_at(52.0) > fit.multiplier_at(32.0)


def test_curve_prediction_charges_more_for_hotter_part_of_lift():
    db = db_with_schema()
    add_heating_series(db)
    fit = mod.learn_temperature_efficiency(db)
    assert fit is not None
    cold_lift = fit.integrated_multiplier(30.0, 40.0)
    hot_lift = fit.integrated_multiplier(45.0, 55.0)
    assert hot_lift > cold_lift


def test_draw_interval_is_excluded():
    db = db_with_schema()
    add_heating_series(db)
    draw_ts = datetime(2026, 9, 1, 4, 50, tzinfo=timezone.utc).isoformat()
    db.execute("INSERT INTO dhw_draw_events(timestamp) VALUES(?)", (draw_ts,))
    db.commit()
    fit = mod.learn_temperature_efficiency(db)
    assert fit is not None
    assert fit.sample_count == 59


def test_persist_keeps_shadow_metrics_separate():
    db = db_with_schema()
    add_heating_series(db)
    fit = mod.learn_temperature_efficiency(db)
    assert fit is not None
    mod.persist_temperature_efficiency_fit(
        db, fit, baseline_mae_kwh=0.4, curve_mae_kwh=0.3, validation_cycles=12
    )
    values = dict(db.execute("SELECT name,value FROM dhw_model_parameters"))
    assert values["dhw_efficiency_shadow_baseline_mae_kwh"] == 0.4
    assert values["dhw_efficiency_shadow_curve_mae_kwh"] == 0.3
    assert not any(name.startswith("dhw_cycle_") for name in values)
