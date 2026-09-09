import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_validation.py"

spec = importlib.util.spec_from_file_location("dhw_validation", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def db_with_schema():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dhw_forecast_validation("
        "forecast_ts TEXT,target_ts TEXT,predicted_upper_c REAL,predicted_lower_c REAL,"
        "predicted_dhw_kwh REAL,actual_upper_c REAL,actual_lower_c REAL,model_source TEXT,"
        "PRIMARY KEY(forecast_ts,target_ts))"
    )
    db.execute(
        "CREATE TABLE dhw_thermal_samples("
        "timestamp TEXT,upper_temp_c REAL,lower_temp_c REAL,valid INTEGER)"
    )
    db.execute(
        "CREATE TABLE dhw_model_parameters("
        "name TEXT PRIMARY KEY,value REAL,sample_count INTEGER,updated_at TEXT,error REAL)"
    )
    db.execute(
        "CREATE TABLE dhw_demand_profile(day_type TEXT,slot_index INTEGER,sample_days INTEGER)"
    )
    db.execute("CREATE TABLE dhw_draw_events(timestamp TEXT)")
    return db


def test_actual_sample_is_matched_to_due_shadow_prediction():
    db = db_with_schema()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    forecast = now - timedelta(hours=2)
    target = now - timedelta(minutes=1)
    db.execute(
        "INSERT INTO dhw_forecast_validation VALUES(?,?,?,?,?,?,?,?)",
        (forecast.isoformat(), target.isoformat(), 50.0, 42.0, 0.0, None, None, "thermal_shadow"),
    )
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,1)",
        ((target + timedelta(minutes=1)).isoformat(), 49.5, 41.0),
    )
    assert mod.apply_actuals(db) == 1
    actual = db.execute(
        "SELECT actual_upper_c,actual_lower_c FROM dhw_forecast_validation"
    ).fetchone()
    assert actual == (49.5, 41.0)


def test_horizon_metrics_are_bucketed_by_forecast_horizon():
    db = db_with_schema()
    base = datetime.now(timezone.utc) - timedelta(days=1)
    cases = [(3, 1.0, 2.0), (9, 2.0, 3.0), (18, 3.0, 4.0), (30, 4.0, 5.0), (42, 5.0, 6.0)]
    for horizon, upper_error, lower_error in cases:
        target = base + timedelta(hours=horizon)
        db.execute(
            "INSERT INTO dhw_forecast_validation VALUES(?,?,?,?,?,?,?,?)",
            (
                base.isoformat(), target.isoformat(), 50.0, 40.0, 0.0,
                50.0 - upper_error, 40.0 - lower_error, "thermal_shadow",
            ),
        )
    metrics = {metric.name: metric for metric in mod.horizon_metrics(db, lookback_days=7)}
    assert metrics["0_6h"].upper_mae_c == 1.0
    assert metrics["6_12h"].lower_mae_c == 3.0
    assert metrics["12_24h"].upper_mae_c == 3.0
    assert metrics["24_36h"].lower_mae_c == 5.0
    assert metrics["36_48h"].upper_mae_c == 5.0


def test_confidence_can_be_thermal_ready_but_never_promotes_without_legacy_comparison():
    db = db_with_schema()
    now = datetime.now(timezone.utc)
    db.execute("INSERT INTO dhw_thermal_samples VALUES(?,?,?,1)", (now.isoformat(), 50.0, 42.0))
    for name in ("dhw_upper_loss_w_per_k", "dhw_lower_loss_w_per_k", "dhw_coupling_w_per_k"):
        db.execute("INSERT INTO dhw_model_parameters VALUES(?,?,?,?,?)", (name, 1.0, 30, now.isoformat(), 0.1))
    for name in ("dhw_cycle_intercept_kwh", "dhw_cycle_upper_kwh_per_c", "dhw_cycle_lower_kwh_per_c"):
        db.execute("INSERT INTO dhw_model_parameters VALUES(?,?,?,?,?)", (name, 1.0, 6, now.isoformat(), 0.1))
    db.execute("INSERT INTO dhw_demand_profile VALUES('weekday',14,8)")
    for idx in range(10):
        db.execute("INSERT INTO dhw_draw_events VALUES(?)", ((now - timedelta(hours=idx)).isoformat(),))
    for idx in range(20):
        forecast = now - timedelta(hours=1, minutes=idx)
        target = forecast + timedelta(hours=2)
        db.execute(
            "INSERT INTO dhw_forecast_validation VALUES(?,?,?,?,?,?,?,?)",
            (forecast.isoformat(), target.isoformat(), 50.0, 42.0, 0.0, 49.0, 40.0, "thermal_shadow"),
        )
    result = mod.confidence_result(db)
    assert result.thermal_ready is True
    assert result.promotion_ready is False
    assert result.confidence > 0.7


def test_confidence_persistence_writes_separate_readiness_flags():
    db = db_with_schema()
    result = mod.ConfidenceResult(0.6, True, False, 30, 6, 8, 12, 25, 1.2, 2.5)
    mod.persist_confidence(db, result)
    values = dict(db.execute("SELECT name,value FROM dhw_model_parameters"))
    assert values["dhw_model_confidence"] == 0.6
    assert values["dhw_thermal_ready"] == 1.0
    assert values["dhw_promotion_ready"] == 0.0
