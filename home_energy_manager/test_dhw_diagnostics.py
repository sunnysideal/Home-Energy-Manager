import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"

for name in ("dhw_simulator", "dhw_validation", "dhw_diagnostics_runner"):
    path = APP / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

mod = sys.modules["dhw_diagnostics_runner"]


def db_with_params():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_model_parameters("
        "name TEXT PRIMARY KEY,value REAL,sample_count INTEGER,updated_at TEXT,error REAL)"
    )
    return db


def status(**overrides):
    values = dict(
        promotion_ready=False,
        thermal_ready=False,
        cycle_count=0,
        demand_days=0,
        draw_count=0,
        validation_count=0,
        energy_validation_days=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_status_progresses_through_training_stages():
    assert mod._status(status()) == "learning_heating_cycles"
    assert mod._status(status(
        cycle_count=5, demand_days=2, draw_count=3,
    )) == "learning_hot_water_demand"
    assert mod._status(status(
        cycle_count=5, demand_days=7, draw_count=10, validation_count=5,
    )) == "validating_shadow_forecast"
    assert mod._status(status(
        thermal_ready=True, cycle_count=5, demand_days=7, draw_count=10,
        validation_count=20, energy_validation_days=3,
    )) == "validating_against_legacy"
    assert mod._status(status(
        thermal_ready=True, cycle_count=5, demand_days=7, draw_count=10,
        validation_count=20, energy_validation_days=7,
    )) == "thermal_model_ready"
    assert mod._status(status(
        promotion_ready=True, thermal_ready=True, cycle_count=5, demand_days=7,
        draw_count=10, validation_count=20, energy_validation_days=7,
    )) == "ready_for_promotion"


def test_available_energy_respects_configured_tank_volume_and_minimum_temperature():
    db = db_with_params()
    small = mod._usable_energy(
        db, 50.0, 50.0, tank_volume_l=100.0, minimum_useful_temperature_c=40.0,
    )
    large = mod._usable_energy(
        db, 50.0, 50.0, tank_volume_l=250.0, minimum_useful_temperature_c=40.0,
    )
    higher_minimum = mod._usable_energy(
        db, 50.0, 50.0, tank_volume_l=250.0, minimum_useful_temperature_c=45.0,
    )
    assert large > small
    assert higher_minimum < large
    assert abs(large / small - 2.5) < 1e-9


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
    assert abs(predicted - 1.2) < 1e-9
