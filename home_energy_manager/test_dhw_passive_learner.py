import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_passive_learner.py"

spec = importlib.util.spec_from_file_location("dhw_passive_learner", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def make_db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_thermal_samples ("
        "timestamp TEXT PRIMARY KEY, upper_temp_c REAL, lower_temp_c REAL, "
        "dhw_heating INTEGER, immersion_heating INTEGER, ambient_temp_c REAL, valid INTEGER)"
    )
    db.execute(
        "CREATE TABLE dhw_draw_events (timestamp TEXT PRIMARY KEY, estimated_thermal_kwh REAL, confidence REAL, "
        "upper_before_c REAL, lower_before_c REAL, upper_after_c REAL, lower_after_c REAL)"
    )
    db.execute(
        "CREATE TABLE dhw_model_parameters (name TEXT PRIMARY KEY, value REAL, sample_count INTEGER, updated_at TEXT, error REAL)"
    )
    return db


def populate_exact_passive_series(db, *, intervals=36, upper_loss=1.2, lower_loss=0.8, coupling=3.0):
    volume = 250.0
    upper_fraction = 0.45
    upper_cap = volume * upper_fraction * mod.WATER_KWH_PER_LITRE_C
    lower_cap = volume * (1.0 - upper_fraction) * mod.WATER_KWH_PER_LITRE_C
    ambient = 20.0
    upper = 52.0
    lower = 44.0
    start = datetime.now(timezone.utc) - timedelta(hours=4)
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,?,?,?)",
        (start.isoformat(), upper, lower, 0, 0, ambient, 1),
    )
    for i in range(1, intervals + 1):
        hours = 5.0 / 60.0
        gap = max(upper - lower, 0.0)
        upper_drop = (upper_loss * (upper - ambient) + coupling * gap) * hours / 1000.0
        lower_drop = (lower_loss * (lower - ambient) - coupling * gap) * hours / 1000.0
        upper -= upper_drop / upper_cap
        lower -= lower_drop / lower_cap
        ts = start + timedelta(minutes=5 * i)
        db.execute(
            "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,?,?,?)",
            (ts.isoformat(), upper, lower, 0, 0, ambient, 1),
        )
    db.commit()


def test_exact_quiet_series_recovers_physical_parameters():
    db = make_db()
    populate_exact_passive_series(db)
    fit = mod.learn_passive_parameters(db, minimum_intervals=24)
    assert fit is not None
    assert abs(fit.upper_loss_w_per_k - 1.2) < 0.05
    assert abs(fit.lower_loss_w_per_k - 0.8) < 0.05
    assert abs(fit.coupling_w_per_k - 3.0) < 0.05
    assert fit.sample_count >= 24
    assert fit.rmse_c < 0.01


def test_insufficient_samples_do_not_create_model():
    db = make_db()
    populate_exact_passive_series(db, intervals=5)
    assert mod.learn_passive_parameters(db, minimum_intervals=24) is None


def test_persisted_fit_writes_canonical_parameters_and_legacy_aliases():
    db = make_db()
    fit = mod.PassiveFit(1.2, 0.8, 3.0, 30, 0.12)
    mod.persist_passive_fit(db, fit)
    rows = dict(db.execute("SELECT name,value FROM dhw_model_parameters"))
    assert rows == {
        "dhw_upper_loss_w_per_k": 1.2,
        "dhw_lower_loss_w_per_k": 0.8,
        "dhw_coupling_w_per_k": 3.0,
        "upper_loss_w_per_k": 1.2,
        "lower_loss_w_per_k": 0.8,
        "coupling_w_per_k": 3.0,
    }
