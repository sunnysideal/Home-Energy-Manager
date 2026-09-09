import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_diagnostics_runner.py"

spec = importlib.util.spec_from_file_location("dhw_diagnostics_runner", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def db_with_params():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_model_parameters ("
        "name TEXT PRIMARY KEY,value REAL,sample_count INTEGER,updated_at TEXT,error REAL)"
    )
    return db


def test_predicted_next_heating_energy_is_unknown_until_cycle_model_exists():
    db = db_with_params()
    assert mod._next_heating_energy(db, 45.0, 35.0, 50.0) is None


def test_predicted_next_heating_energy_uses_both_temperature_deficits():
    db = db_with_params()
    rows = [
        ("dhw_cycle_intercept_kwh", 0.2),
        ("dhw_cycle_upper_kwh_per_c", 0.04),
        ("dhw_cycle_lower_kwh_per_c", 0.08),
    ]
    db.executemany(
        "INSERT INTO dhw_model_parameters VALUES(?,?,0,'2026-09-09T12:00:00+00:00',NULL)",
        rows,
    )
    predicted = mod._next_heating_energy(db, 45.0, 35.0, 50.0)
    assert predicted is not None
    assert abs(predicted - 1.6) < 1e-9
