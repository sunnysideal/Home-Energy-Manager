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
        "predicted_dhw_kwh REAL,legacy_dhw_kwh REAL,actual_dhw_kwh REAL,"
        "actual_upper_c REAL,actual_lower_c REAL,model_source TEXT,"
        "PRIMARY KEY(forecast_ts,target_ts))"
    )
    db.execute(
        "CREATE TABLE dhw_thermal_samples("
        "timestamp TEXT,upper_temp_c REAL,lower_temp_c REAL,dhw_energy_total_kwh REAL,valid INTEGER)"
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


def insert_validation(db, forecast, target, *, thermal=0.0, legacy=0.0, actual=None, upper_actual=None, lower_actual=None):
    db.execute(
        "INSERT INTO dhw_forecast_validation VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            forecast.isoformat(), target.isoformat(), 50.0, 42.0,
            thermal, legacy, actual, upper_actual, lower_actual, "thermal_shadow",
        ),
    )


def test_actual_sample_is_matched_to_due_shadow_prediction():
    db = db_with_schema()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    forecast = now - timedelta(hours=2)
    target = now - timedelta(minutes=1)
    insert_validation(db, forecast, target)
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)",
        ((target + timedelta(minutes=1)).isoformat(), 49.5, 41.0, 100.0),
    )
    assert mod.apply_actuals(db) == 1
    actual = db.execute(
        "SELECT actual_upper_c,actual_lower_c FROM dhw_forecast_validation"
    ).fetchone()
    assert actual == (49.5, 41.0)


def test_actual_energy_uses_same_30_minute_cumulative_meter_interval():
    db = db_with_schema()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    target = now - timedelta(minutes=1)
    forecast = target - timedelta(hours=2)
    insert_validation(db, forecast, target, thermal=0.8, legacy=1.0)
    start = target - timedelta(minutes=30)
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)",
        (start.isoformat(), 45.0, 35.0, 100.0),
    )
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)",
        (target.isoformat(), 50.0, 42.0, 100.75),
    )
    assert mod.apply_actual_energy(db) == 1
    actual = db.execute("SELECT actual_dhw_kwh FROM dhw_forecast_validation").fetchone()
    assert abs(actual[0] - 0.75) < 1e-9


def test_actual_energy_rejects_cumulative_meter_reset():
    db = db_with_schema()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    target = now - timedelta(minutes=1)
    insert_validation(db, target - timedelta(hours=1), target)
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)",
        ((target - timedelta(minutes=30)).isoformat(), 45.0, 35.0, 100.0),
    )
    db.execute(
        "INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)",
        (target.isoformat(), 50.0, 42.0, 2.0),
    )
    assert mod.apply_actual_energy(db) == 0


def test_horizon_metrics_are_bucketed_by_forecast_horizon():
    db = db_with_schema()
    base = datetime.now(timezone.utc) - timedelta(days=1)
    cases = [(3, 1.0, 2.0), (9, 2.0, 3.0), (18, 3.0, 4.0), (30, 4.0, 5.0), (42, 5.0, 6.0)]
    for horizon, upper_error, lower_error in cases:
        target = base + timedelta(hours=horizon)
        insert_validation(
            db, base, target,
            upper_actual=50.0 - upper_error,
            lower_actual=42.0 - lower_error,
        )
    metrics = {metric.name: metric for metric in mod.horizon_metrics(db, lookback_days=7)}
    assert metrics["0_6h"].upper_mae_c == 1.0
    assert metrics["6_12h"].lower_mae_c == 3.0
    assert metrics["12_24h"].upper_mae_c == 3.0
    assert metrics["24_36h"].lower_mae_c == 5.0
    assert metrics["36_48h"].upper_mae_c == 5.0


def test_energy_comparison_scores_thermal_and_legacy_against_same_actuals():
    db = db_with_schema()
    now = datetime.now(timezone.utc)
    for idx, actual in enumerate((0.0, 0.5, 1.0, 0.25)):
        forecast = now - timedelta(hours=2, days=idx)
        target = forecast + timedelta(hours=2)
        insert_validation(
            db, forecast, target,
            thermal=actual + 0.1,
            legacy=actual + 0.3,
            actual=actual,
        )
    result = mod.energy_comparison(db, lookback_days=14)
    assert result.count == 4
    assert result.days == 4
    assert abs(result.thermal_mae_kwh - 0.1) < 1e-9
    assert abs(result.legacy_mae_kwh - 0.3) < 1e-9
    assert result.thermal_not_worse is True


def seed_ready_model(db, now):
    db.execute("INSERT INTO dhw_thermal_samples VALUES(?,?,?,?,1)", (now.isoformat(), 50.0, 42.0, 100.0))
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
        insert_validation(db, forecast, target, upper_actual=49.0, lower_actual=40.0)


def test_promotion_stays_false_until_seven_days_of_energy_comparison():
    db = db_with_schema()
    now = datetime.now(timezone.utc)
    seed_ready_model(db, now)
    for idx in range(20):
        forecast = now - timedelta(hours=8 + (idx % 5), minutes=idx)
        target = forecast + timedelta(hours=2)
        insert_validation(db, forecast, target, thermal=0.2, legacy=0.4, actual=0.0)
    result = mod.confidence_result(db)
    assert result.thermal_ready is True
    assert result.energy_validation_count >= 20
    assert result.energy_validation_days < 7
    assert result.promotion_ready is False


def test_promotion_can_become_ready_only_when_thermal_is_no_worse_than_legacy():
    db = db_with_schema()
    now = datetime.now(timezone.utc)
    seed_ready_model(db, now)
    for day in range(7):
        for slot in range(3):
            forecast = now - timedelta(days=day, hours=3 + slot)
            target = forecast + timedelta(hours=2)
            insert_validation(db, forecast, target, thermal=0.1, legacy=0.4, actual=0.0)
    result = mod.confidence_result(db)
    assert result.thermal_ready is True
    assert result.energy_validation_days >= 7
    assert result.thermal_energy_mae_kwh < result.legacy_energy_mae_kwh
    assert result.promotion_ready is True


def test_promotion_is_blocked_when_thermal_energy_error_is_worse():
    db = db_with_schema()
    now = datetime.now(timezone.utc)
    seed_ready_model(db, now)
    for day in range(7):
        for slot in range(3):
            forecast = now - timedelta(days=day, hours=7 + slot)
            target = forecast + timedelta(hours=2)
            insert_validation(db, forecast, target, thermal=0.5, legacy=0.1, actual=0.0)
    result = mod.confidence_result(db)
    assert result.thermal_ready is True
    assert result.promotion_ready is False


def test_confidence_persistence_writes_readiness_and_energy_mae():
    db = db_with_schema()
    result = mod.ConfidenceResult(
        confidence=0.6,
        trial_ready=True,
        thermal_ready=True,
        promotion_ready=True,
        performance_bad=False,
        passive_samples=30,
        cycle_count=6,
        demand_days=8,
        draw_count=12,
        validation_count=25,
        upper_mae_c=1.2,
        lower_mae_c=2.5,
        energy_validation_count=40,
        energy_validation_days=7,
        thermal_energy_mae_kwh=0.15,
        legacy_energy_mae_kwh=0.30,
    )
    mod.persist_confidence(db, result)
    values = dict(db.execute("SELECT name,value FROM dhw_model_parameters"))
    assert values["dhw_model_confidence"] == 0.6
    assert values["dhw_trial_ready"] == 1.0
    assert values["dhw_thermal_ready"] == 1.0
    assert values["dhw_promotion_ready"] == 1.0
    assert values["dhw_thermal_energy_mae_kwh"] == 0.15
    assert values["dhw_legacy_energy_mae_kwh"] == 0.30
