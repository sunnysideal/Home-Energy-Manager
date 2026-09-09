import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_cycle_learner.py"

spec = importlib.util.spec_from_file_location("dhw_cycle_learner", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def db_with_schema():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_heating_cycles ("
        "start_ts TEXT,end_ts TEXT,start_upper_c REAL,start_lower_c REAL,end_upper_c REAL,"
        "end_lower_c REAL,target_temp_c REAL,electrical_kwh REAL,cycle_type TEXT,valid INTEGER)"
    )
    db.execute(
        "CREATE TABLE dhw_model_parameters ("
        "name TEXT PRIMARY KEY,value REAL,sample_count INTEGER,updated_at TEXT,error REAL)"
    )
    return db


def add_cycle(db, idx, upper, lower, target, energy, cycle_type="normal_dhw", valid=1):
    start = datetime(2026, 9, min(idx, 28), 5, 0, tzinfo=timezone.utc)
    duration_h = max(0.25, energy / 2.0)
    end = start + timedelta(hours=duration_h)
    end_upper = upper + 4.0 * energy
    end_lower = lower + 6.0 * energy
    db.execute(
        "INSERT INTO dhw_heating_cycles VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            start.isoformat(), end.isoformat(), upper, lower, end_upper, end_lower,
            target, energy, cycle_type, valid,
        ),
    )


def test_energy_model_requires_enough_valid_normal_cycles():
    db = db_with_schema()
    for idx in range(1, 5):
        add_cycle(db, idx, 45, 35, 50, 1.5)
    assert mod.learn_cycle_energy(db) is None


def test_energy_model_learns_positive_temperature_deficit_coefficients_and_response():
    db = db_with_schema()
    rows = [
        (45, 35, 50), (46, 36, 50), (44, 34, 50),
        (47, 38, 52), (43, 33, 50), (48, 40, 54),
        (46, 39, 53), (42, 32, 50),
    ]
    for idx, (upper, lower, target) in enumerate(rows, 1):
        energy = 0.2 + 0.04 * max(target - upper, 0) + 0.08 * max(target - lower, 0)
        add_cycle(db, idx, upper, lower, target, energy)
    fit = mod.learn_cycle_energy(db)
    assert fit is not None
    assert fit.sample_count == len(rows)
    assert fit.upper_kwh_per_c > 0
    assert fit.lower_kwh_per_c > 0
    assert fit.rmse_kwh < 0.05
    assert abs(fit.typical_power_kw - 2.0) < 0.2
    assert abs(fit.upper_c_per_kwh - 4.0) < 0.2
    assert abs(fit.lower_c_per_kwh - 6.0) < 0.2
    predicted = fit.predict(45, 35, 50)
    assert abs(predicted - 1.2) < 0.1


def test_high_temp_and_invalid_cycles_are_excluded():
    db = db_with_schema()
    for idx in range(1, 6):
        add_cycle(db, idx, 45 - idx, 35 - idx, 50, 1.0 + 0.2 * idx)
    add_cycle(db, 6, 30, 20, 60, 8.0, cycle_type="high_temp_dhw")
    add_cycle(db, 7, 30, 20, 50, 8.0, valid=0)
    fit = mod.learn_cycle_energy(db)
    assert fit is not None
    assert fit.sample_count == 5


def test_cycle_energy_fit_is_persisted_with_error_and_sample_count():
    db = db_with_schema()
    fit = mod.CycleEnergyFit(0.2, 0.04, 0.08, 2.0, 4.0, 6.0, 7, 0.15)
    mod.persist_cycle_energy_fit(db, fit)
    rows = dict(db.execute("SELECT name,value FROM dhw_model_parameters"))
    assert rows["dhw_cycle_intercept_kwh"] == 0.2
    assert rows["dhw_cycle_upper_kwh_per_c"] == 0.04
    assert rows["dhw_cycle_lower_kwh_per_c"] == 0.08
    assert rows["dhw_cycle_power_kw"] == 2.0
    assert rows["dhw_cycle_upper_c_per_kwh"] == 4.0
    assert rows["dhw_cycle_lower_c_per_kwh"] == 6.0
    count, error = db.execute(
        "SELECT sample_count,error FROM dhw_model_parameters WHERE name='dhw_cycle_intercept_kwh'"
    ).fetchone()
    assert count == 7
    assert error == 0.15
