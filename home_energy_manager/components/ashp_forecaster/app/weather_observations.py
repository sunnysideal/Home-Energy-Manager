from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Callable, Iterable


DEFAULT_MATCH_TOLERANCE_MINUTES = 15
DEFAULT_BACKFILL_LOOKBACK_HOURS = 48
DEFAULT_SCORE_MAX_HORIZON_HOURS = 48.0
DEFAULT_CALIBRATION_WINDOW_DAYS = 30
DEFAULT_MIN_GLOBAL_SAMPLES = 24
DEFAULT_MIN_HORIZON_SAMPLES = 12
DEFAULT_MAX_ABS_BIAS_C = 5.0
HORIZON_BUCKETS = (
    ("0_6h", 0.0, 6.0),
    ("6_12h", 6.0, 12.0),
    ("12_24h", 12.0, 24.0),
    ("24_36h", 24.0, 36.0),
    ("36_48h", 36.0, 48.000001),
)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("weather observation timestamps must be timezone-aware")
    return dt.isoformat()


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_forecast_observations (
            issued_at TEXT NOT NULL,
            target_ts TEXT NOT NULL,
            horizon_hours REAL NOT NULL,
            forecast_temperature_c REAL NOT NULL,
            source_entity TEXT NOT NULL,
            actual_temperature_c REAL,
            actual_observed_at TEXT,
            error_c REAL,
            PRIMARY KEY (issued_at, target_ts)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_weather_observations_target "
        "ON weather_forecast_observations(target_ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_weather_observations_pending "
        "ON weather_forecast_observations(actual_temperature_c, target_ts)"
    )
    db.commit()


def record_forecast_snapshot(
    db: sqlite3.Connection,
    *,
    issued_at: datetime,
    source_entity: str,
    points: Iterable[tuple[datetime, float]],
) -> int:
    """Persist one provider forecast snapshot without affecting forecast behaviour."""
    ensure_schema(db)
    issued = _iso(issued_at)
    rows: list[tuple[str, str, float, float, str]] = []
    for target, temperature in points:
        if target.tzinfo is None or not math.isfinite(float(temperature)):
            continue
        horizon_hours = (target - issued_at).total_seconds() / 3600.0
        if horizon_hours < 0:
            continue
        rows.append((issued, _iso(target), horizon_hours, float(temperature), source_entity))

    if not rows:
        return 0

    before = db.total_changes
    with db:
        db.executemany(
            """
            INSERT OR IGNORE INTO weather_forecast_observations(
                issued_at, target_ts, horizon_hours, forecast_temperature_c, source_entity
            ) VALUES(?,?,?,?,?)
            """,
            rows,
        )
    return db.total_changes - before


def _numeric_history(history: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    values: list[tuple[datetime, float]] = []
    for row in history:
        try:
            value = float(row["state"])
            if not math.isfinite(value):
                continue
            stamp = row.get("last_changed") or row.get("last_updated")
            if not stamp:
                continue
            values.append((_parse_dt(str(stamp)), value))
        except (KeyError, TypeError, ValueError):
            continue
    values.sort(key=lambda item: item[0])
    return values


def complete_pending_actuals(
    db: sqlite3.Connection,
    *,
    get_history: Callable[[str, datetime, datetime], list[dict[str, Any]]],
    actual_entity: str,
    now: datetime,
    match_tolerance_minutes: int = DEFAULT_MATCH_TOLERANCE_MINUTES,
    lookback_hours: int = DEFAULT_BACKFILL_LOOKBACK_HOURS,
) -> tuple[int, int]:
    """Attach the nearest EcoMAX observation to due forecast targets.

    Targets are only eligible once the full +/- tolerance window has elapsed. A
    bounded lookback prevents an old Recorder gap from causing an ever-growing
    history request on every forecast cycle. Unmatched rows remain in the database
    and can still be inspected later.

    Returns (completed_snapshot_rows, unmatched_target_count).
    """
    ensure_schema(db)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    tolerance = timedelta(minutes=max(1, int(match_tolerance_minutes)))
    lookback = timedelta(hours=max(1, int(lookback_hours)))
    latest_target = now - tolerance
    earliest_target = now - lookback

    target_rows = db.execute(
        """
        SELECT DISTINCT target_ts
        FROM weather_forecast_observations
        WHERE actual_temperature_c IS NULL
          AND target_ts >= ?
          AND target_ts <= ?
        ORDER BY target_ts
        """,
        (_iso(earliest_target), _iso(latest_target)),
    ).fetchall()
    if not target_rows:
        return 0, 0

    targets = [_parse_dt(str(row[0])) for row in target_rows]
    history_start = min(targets) - tolerance
    history_end = min(now, max(targets) + tolerance)
    series = _numeric_history(get_history(actual_entity, history_start, history_end))
    if not series:
        return 0, len(targets)

    completed = 0
    unmatched = 0
    with db:
        for target in targets:
            nearest: tuple[datetime, float] | None = None
            nearest_delta: float | None = None
            for observed_at, value in series:
                delta = abs((observed_at - target).total_seconds())
                if delta > tolerance.total_seconds():
                    continue
                if nearest_delta is None or delta < nearest_delta:
                    nearest = (observed_at, value)
                    nearest_delta = delta
            if nearest is None:
                unmatched += 1
                continue

            observed_at, actual_temp = nearest
            before = db.total_changes
            db.execute(
                """
                UPDATE weather_forecast_observations
                SET actual_temperature_c = ?,
                    actual_observed_at = ?,
                    error_c = ? - forecast_temperature_c
                WHERE target_ts = ? AND actual_temperature_c IS NULL
                """,
                (actual_temp, _iso(observed_at), actual_temp, _iso(target)),
            )
            completed += db.total_changes - before

    return completed, unmatched


def _score_errors(errors: list[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in errors if math.isfinite(float(value))]
    if not clean:
        return {"samples": 0, "mean_bias_c": None, "mae_c": None, "rmse_c": None}
    count = len(clean)
    mean_bias = sum(clean) / count
    mae = sum(abs(value) for value in clean) / count
    rmse = math.sqrt(sum(value * value for value in clean) / count)
    return {
        "samples": count,
        "mean_bias_c": mean_bias,
        "mae_c": mae,
        "rmse_c": rmse,
    }


def _clamp_bias(value: float, limit: float) -> float:
    return max(-abs(float(limit)), min(abs(float(limit)), float(value)))


def _improvement_pct(raw_mae: float | None, corrected_mae: float | None) -> float | None:
    if raw_mae is None or corrected_mae is None or raw_mae <= 0:
        return None
    return (raw_mae - corrected_mae) / raw_mae * 100.0


def raw_forecast_scores(
    db: sqlite3.Connection,
    *,
    max_horizon_hours: float = DEFAULT_SCORE_MAX_HORIZON_HOURS,
) -> dict[str, Any]:
    """Score completed raw provider forecasts without applying any calibration.

    error_c is defined as actual - forecast, so a negative mean bias means the
    provider forecast was warmer than the EcoMAX outdoor sensor on average.
    Only genuine provider points at horizons from 0 through max_horizon_hours are
    scored. Synthetic horizon-extension points are never stored by Phase 1.
    """
    ensure_schema(db)
    rows = db.execute(
        """
        SELECT horizon_hours, error_c
        FROM weather_forecast_observations
        WHERE actual_temperature_c IS NOT NULL
          AND error_c IS NOT NULL
          AND horizon_hours >= 0
          AND horizon_hours <= ?
        ORDER BY issued_at, target_ts
        """,
        (float(max_horizon_hours),),
    ).fetchall()

    pairs = [
        (float(horizon), float(error))
        for horizon, error in rows
        if math.isfinite(float(horizon)) and math.isfinite(float(error))
    ]
    overall = _score_errors([error for _, error in pairs])
    buckets: dict[str, dict[str, float | int | None]] = {}
    for name, lower, upper in HORIZON_BUCKETS:
        if lower >= max_horizon_hours:
            bucket_errors: list[float] = []
        else:
            effective_upper = min(upper, max_horizon_hours + 0.000001)
            bucket_errors = [
                error
                for horizon, error in pairs
                if horizon >= lower and horizon < effective_upper
            ]
        buckets[name] = _score_errors(bucket_errors)

    return {
        "max_horizon_hours": float(max_horizon_hours),
        "overall": overall,
        "horizons": buckets,
    }


def shadow_weather_calibration(
    db: sqlite3.Connection,
    *,
    now: datetime,
    window_days: int = DEFAULT_CALIBRATION_WINDOW_DAYS,
    min_global_samples: int = DEFAULT_MIN_GLOBAL_SAMPLES,
    min_horizon_samples: int = DEFAULT_MIN_HORIZON_SAMPLES,
    max_abs_bias_c: float = DEFAULT_MAX_ABS_BIAS_C,
    max_horizon_hours: float = DEFAULT_SCORE_MAX_HORIZON_HOURS,
) -> dict[str, Any]:
    """Learn robust weather bias and score it in shadow without changing production.

    Bias uses the issue's ``actual - forecast`` sign convention. The robust learned
    correction is the median completed error in the rolling window, clamped to a
    conservative absolute limit. Horizon buckets with too few samples fall back to
    the global median when the global sample floor is met; otherwise shadow
    correction is zero until enough evidence exists.
    """
    ensure_schema(db)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    effective_window_days = max(1, int(window_days))
    cutoff = now - timedelta(days=effective_window_days)
    rows = db.execute(
        """
        SELECT horizon_hours, error_c, actual_observed_at
        FROM weather_forecast_observations
        WHERE actual_temperature_c IS NOT NULL
          AND error_c IS NOT NULL
          AND actual_observed_at IS NOT NULL
          AND actual_observed_at >= ?
          AND actual_observed_at <= ?
          AND horizon_hours >= 0
          AND horizon_hours <= ?
        ORDER BY actual_observed_at, issued_at, target_ts
        """,
        (_iso(cutoff), _iso(now), float(max_horizon_hours)),
    ).fetchall()

    samples: list[tuple[float, float, datetime]] = []
    for horizon, error, observed_at in rows:
        try:
            h = float(horizon)
            e = float(error)
            stamp = _parse_dt(str(observed_at))
        except (TypeError, ValueError):
            continue
        if math.isfinite(h) and math.isfinite(e):
            samples.append((h, e, stamp))

    errors = [error for _, error, _ in samples]
    raw_overall = _score_errors(errors)
    global_median = median(errors) if errors else None
    global_usable = len(errors) >= max(1, int(min_global_samples))
    global_correction = (
        _clamp_bias(float(global_median), max_abs_bias_c)
        if global_median is not None and global_usable
        else 0.0
    )

    horizon_results: dict[str, dict[str, Any]] = {}
    corrected_errors: list[float] = []
    for name, lower, upper in HORIZON_BUCKETS:
        bucket = [(error, observed) for horizon, error, observed in samples if horizon >= lower and horizon < upper]
        bucket_errors = [error for error, _ in bucket]
        bucket_median = median(bucket_errors) if bucket_errors else None
        bucket_usable = len(bucket_errors) >= max(1, int(min_horizon_samples))
        if bucket_median is not None and bucket_usable:
            correction = _clamp_bias(float(bucket_median), max_abs_bias_c)
            correction_source = "horizon"
        elif global_usable:
            correction = global_correction
            correction_source = "global_fallback"
        else:
            correction = 0.0
            correction_source = "insufficient_samples"
        bucket_corrected = [error - correction for error in bucket_errors]
        corrected_errors.extend(bucket_corrected)
        raw_score = _score_errors(bucket_errors)
        corrected_score = _score_errors(bucket_corrected)
        horizon_results[name] = {
            "samples": len(bucket_errors),
            "median_bias_c": bucket_median,
            "usable": bucket_usable,
            "correction_c": correction,
            "correction_source": correction_source,
            "raw_mean_bias_c": raw_score["mean_bias_c"],
            "raw_mae_c": raw_score["mae_c"],
            "raw_rmse_c": raw_score["rmse_c"],
            "shadow_mean_bias_c": corrected_score["mean_bias_c"],
            "shadow_mae_c": corrected_score["mae_c"],
            "shadow_rmse_c": corrected_score["rmse_c"],
            "improvement_pct": _improvement_pct(raw_score["mae_c"], corrected_score["mae_c"]),
        }

    corrected_overall = _score_errors(corrected_errors)
    observed_times = [stamp for _, _, stamp in samples]
    return {
        "calibration_applied": False,
        "window_days": effective_window_days,
        "window_start": _iso(cutoff),
        "window_end": _iso(now),
        "samples": len(samples),
        "oldest_sample_at": _iso(min(observed_times)) if observed_times else None,
        "newest_sample_at": _iso(max(observed_times)) if observed_times else None,
        "min_global_samples": max(1, int(min_global_samples)),
        "min_horizon_samples": max(1, int(min_horizon_samples)),
        "max_abs_bias_c": abs(float(max_abs_bias_c)),
        "global_median_bias_c": global_median,
        "global_bias_usable": global_usable,
        "global_correction_c": global_correction,
        "raw": raw_overall,
        "shadow_corrected": corrected_overall,
        "improvement_pct": _improvement_pct(raw_overall["mae_c"], corrected_overall["mae_c"]),
        "horizons": horizon_results,
    }
