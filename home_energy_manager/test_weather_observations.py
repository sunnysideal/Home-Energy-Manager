from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from components.ashp_forecaster.app.weather_observations import (
    complete_pending_actuals,
    ensure_schema,
    raw_forecast_scores,
    record_forecast_snapshot,
    shadow_weather_calibration,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    ensure_schema(db)
    return db


def test_records_each_provider_snapshot_with_horizon() -> None:
    db = _db()
    issued = datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc)
    points = [(datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc), 10.2), (datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc), 9.7)]
    assert record_forecast_snapshot(db, issued_at=issued, source_entity="weather.forecast_home", points=points) == 2
    rows = db.execute("SELECT target_ts,horizon_hours,forecast_temperature_c,source_entity FROM weather_forecast_observations ORDER BY target_ts").fetchall()
    assert len(rows) == 2
    assert rows[0][1] == 55 / 60
    assert rows[0][2:] == (10.2, "weather.forecast_home")


def test_repeated_snapshot_is_idempotent() -> None:
    db = _db()
    issued = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    points = [(datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc), 10.0)]
    assert record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=points) == 1
    assert record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=points) == 0
    assert db.execute("SELECT COUNT(*) FROM weather_forecast_observations").fetchone()[0] == 1


def test_backfill_uses_nearest_actual_within_tolerance_for_all_snapshots() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    for issued, forecast in [(datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc), 9.5), (datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 10.0)]:
        record_forecast_snapshot(db, issued_at=issued, source_entity="weather.forecast_home", points=[(target, forecast)])
    def history(entity, start, end):
        assert entity == "sensor.ecomax360i_outdoor_temperature"
        return [{"state": "8.8", "last_changed": "2026-09-12T12:51:00+00:00"}, {"state": "8.6", "last_changed": "2026-09-12T13:03:00+00:00"}, {"state": "8.5", "last_changed": "2026-09-12T13:09:00+00:00"}]
    completed, unmatched = complete_pending_actuals(db, get_history=history, actual_entity="sensor.ecomax360i_outdoor_temperature", now=datetime(2026, 9, 12, 13, 20, tzinfo=timezone.utc))
    assert (completed, unmatched) == (2, 0)
    rows = db.execute("SELECT forecast_temperature_c,actual_temperature_c,actual_observed_at,error_c FROM weather_forecast_observations ORDER BY issued_at").fetchall()
    assert rows[0][0:2] == (9.5, 8.6)
    assert rows[0][2] == "2026-09-12T13:03:00+00:00"
    assert round(rows[0][3], 3) == -0.9
    assert round(rows[1][3], 3) == -1.4


def test_backfill_waits_until_tolerance_window_has_elapsed() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    record_forecast_snapshot(db, issued_at=target - timedelta(hours=1), source_entity="weather.forecast_home", points=[(target, 10.0)])
    called = False
    def history(entity, start, end):
        nonlocal called
        called = True
        return []
    completed, unmatched = complete_pending_actuals(db, get_history=history, actual_entity="sensor.outdoor", now=target + timedelta(minutes=10))
    assert (completed, unmatched) == (0, 0)
    assert called is False


def test_unmatched_target_remains_pending() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    record_forecast_snapshot(db, issued_at=target - timedelta(hours=2), source_entity="weather.forecast_home", points=[(target, 10.0)])
    def history(entity, start, end):
        return [{"state": "8.0", "last_changed": "2026-09-12T13:20:00+00:00"}]
    assert complete_pending_actuals(db, get_history=history, actual_entity="sensor.outdoor", now=target + timedelta(minutes=30)) == (0, 1)
    assert db.execute("SELECT actual_temperature_c,error_c FROM weather_forecast_observations").fetchone() == (None, None)


def _insert_scored(db: sqlite3.Connection, horizon: float, error: float, suffix: int) -> None:
    issued = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=suffix)
    _insert_scored_at(db, horizon, error, issued)


def _insert_scored_at(db: sqlite3.Connection, horizon: float, error: float, issued: datetime) -> None:
    target = issued + timedelta(hours=horizon)
    forecast = 10.0
    actual = forecast + error
    db.execute(
        "INSERT INTO weather_forecast_observations(issued_at,target_ts,horizon_hours,forecast_temperature_c,source_entity,actual_temperature_c,actual_observed_at,error_c) VALUES(?,?,?,?,?,?,?,?)",
        (issued.isoformat(), target.isoformat(), horizon, forecast, "weather.home", actual, target.isoformat(), error),
    )
    db.commit()


def test_raw_scores_compute_bias_mae_rmse_and_horizon_buckets() -> None:
    db = _db()
    samples = [(1.0, -1.0), (5.0, 1.0), (8.0, -2.0), (18.0, 2.0), (30.0, -3.0), (42.0, 3.0)]
    for index, (horizon, error) in enumerate(samples):
        _insert_scored(db, horizon, error, index)
    scores = raw_forecast_scores(db)
    overall = scores["overall"]
    assert overall["samples"] == 6
    assert overall["mean_bias_c"] == 0.0
    assert overall["mae_c"] == 2.0
    assert math.isclose(overall["rmse_c"], math.sqrt(28 / 6))
    assert scores["horizons"]["0_6h"]["samples"] == 2
    assert scores["horizons"]["0_6h"]["mae_c"] == 1.0
    assert scores["horizons"]["6_12h"]["mean_bias_c"] == -2.0
    assert scores["horizons"]["12_24h"]["mean_bias_c"] == 2.0
    assert scores["horizons"]["24_36h"]["mean_bias_c"] == -3.0
    assert scores["horizons"]["36_48h"]["mean_bias_c"] == 3.0


def test_raw_scores_ignore_pending_and_beyond_48_hours() -> None:
    db = _db()
    _insert_scored(db, 12.0, -1.5, 1)
    _insert_scored(db, 49.0, 20.0, 2)
    issued = datetime(2026, 9, 1, tzinfo=timezone.utc)
    target = issued + timedelta(hours=3)
    record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=[(target, 99.0)])
    scores = raw_forecast_scores(db)
    assert scores["overall"]["samples"] == 1
    assert scores["overall"]["mean_bias_c"] == -1.5
    assert scores["horizons"]["12_24h"]["samples"] == 1


def test_raw_scores_empty_database_is_explicitly_untrained() -> None:
    scores = raw_forecast_scores(_db())
    assert scores["overall"] == {"samples": 0, "mean_bias_c": None, "mae_c": None, "rmse_c": None}
    assert all(bucket["samples"] == 0 for bucket in scores["horizons"].values())


def test_shadow_calibration_uses_robust_median_not_outlier_mean() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    errors = [1.0, 1.1, 0.9, 1.0, 20.0]
    for index, error in enumerate(errors):
        _insert_scored_at(db, 3.0, error, now - timedelta(hours=8, minutes=index))
    result = shadow_weather_calibration(db, now=now, min_global_samples=3, min_horizon_samples=3)
    assert result["global_median_bias_c"] == 1.0
    assert result["global_correction_c"] == 1.0
    assert result["horizons"]["0_6h"]["correction_source"] == "horizon"
    assert result["horizons"]["0_6h"]["correction_c"] == 1.0


def test_shadow_calibration_filters_to_rolling_window() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    _insert_scored_at(db, 1.0, 4.0, now - timedelta(days=40, hours=1))
    for index in range(3):
        _insert_scored_at(db, 1.0, -0.5, now - timedelta(days=2, hours=index + 2))
    result = shadow_weather_calibration(db, now=now, window_days=30, min_global_samples=2, min_horizon_samples=2)
    assert result["samples"] == 3
    assert result["global_median_bias_c"] == -0.5
    assert result["oldest_sample_at"] is not None
    assert "2026-08" not in result["oldest_sample_at"]


def test_sparse_horizon_falls_back_to_global_bias() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    for index in range(6):
        _insert_scored_at(db, 3.0, 0.8, now - timedelta(hours=12 + index))
    _insert_scored_at(db, 30.0, -3.0, now - timedelta(days=2))
    result = shadow_weather_calibration(db, now=now, min_global_samples=4, min_horizon_samples=3)
    sparse = result["horizons"]["24_36h"]
    assert sparse["samples"] == 1
    assert sparse["usable"] is False
    assert sparse["correction_source"] == "global_fallback"
    assert sparse["correction_c"] == 0.8


def test_shadow_calibration_clamps_extreme_bias() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    for index in range(4):
        _insert_scored_at(db, 8.0, 12.0, now - timedelta(hours=20 + index))
    result = shadow_weather_calibration(db, now=now, min_global_samples=2, min_horizon_samples=2, max_abs_bias_c=5.0)
    assert result["global_median_bias_c"] == 12.0
    assert result["global_correction_c"] == 5.0
    assert result["horizons"]["6_12h"]["correction_c"] == 5.0


def test_shadow_scoring_reports_improvement_without_applying_calibration() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    for index, error in enumerate([1.0, 1.2, 0.8, 1.1]):
        _insert_scored_at(db, 18.0, error, now - timedelta(days=1, hours=index + 18))
    result = shadow_weather_calibration(db, now=now, min_global_samples=2, min_horizon_samples=2)
    assert result["calibration_applied"] is False
    assert result["raw"]["mae_c"] > result["shadow_corrected"]["mae_c"]
    assert result["improvement_pct"] > 0
    assert result["horizons"]["12_24h"]["shadow_mae_c"] < result["horizons"]["12_24h"]["raw_mae_c"]


def test_shadow_calibration_ignores_beyond_48h_and_pending_rows() -> None:
    db = _db()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    _insert_scored_at(db, 49.0, 10.0, now - timedelta(days=2, hours=49))
    issued = now - timedelta(hours=2)
    record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=[(issued + timedelta(hours=1), 99.0)])
    result = shadow_weather_calibration(db, now=now, min_global_samples=1, min_horizon_samples=1)
    assert result["samples"] == 0
    assert result["global_bias_usable"] is False
    assert result["global_correction_c"] == 0.0
