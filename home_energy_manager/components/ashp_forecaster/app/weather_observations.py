from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable


DEFAULT_MATCH_TOLERANCE_MINUTES = 15
DEFAULT_BACKFILL_LOOKBACK_HOURS = 48
DEFAULT_SCORE_MAX_HORIZON_HOURS = 48.0
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
