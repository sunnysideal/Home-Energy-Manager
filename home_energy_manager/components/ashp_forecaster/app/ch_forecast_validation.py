from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from temperature_bias import temperature_bands

HORIZON_BUCKETS = (
    ("0_6h", 0.0, 6.0),
    ("6_12h", 6.0, 12.0),
    ("12_24h", 12.0, 24.0),
    ("24_36h", 24.0, 36.0),
    ("36_48h", 36.0, 48.000001),
)
DEFAULT_WINDOW_DAYS = 30
DEFAULT_LOOKBACK_HOURS = 72
OBSERVATIONAL_SAMPLES = 12
OBSERVATIONAL_DAYS = 2
RELIABLE_SAMPLES = 48
RELIABLE_DAYS = 3


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("CH forecast validation timestamps must be timezone-aware")
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ch_forecast_observations (
            issued_at TEXT NOT NULL,
            target_start TEXT NOT NULL,
            target_end TEXT NOT NULL,
            horizon_hours REAL NOT NULL,
            forecast_ch_kwh REAL NOT NULL,
            forecast_temperature_c REAL,
            forecast_degree_days REAL,
            coefficient_kwh_per_dd REAL,
            forecast_heating_mode TEXT,
            forecast_heating_enabled INTEGER NOT NULL,
            dhw_active INTEGER,
            winter_mode_below_c REAL,
            summer_mode_above_c REAL,
            forecast_status TEXT NOT NULL,
            actual_ch_kwh REAL,
            actual_mean_temperature_c REAL,
            error_kwh REAL,
            completed_at TEXT,
            data_quality TEXT,
            PRIMARY KEY (issued_at, target_start)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ch_forecast_target "
        "ON ch_forecast_observations(target_start, target_end)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ch_forecast_pending "
        "ON ch_forecast_observations(actual_ch_kwh, target_end)"
    )
    db.commit()


def record_published_forecast(
    db: sqlite3.Connection,
    *,
    issued_at: datetime,
    forecast: Iterable[dict[str, Any]],
    interval_minutes: int,
    coefficient_kwh_per_dd: float | None,
    winter_mode_below_c: float | None,
    summer_mode_above_c: float | None,
    forecast_status: str,
) -> int:
    ensure_schema(db)
    if issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    interval = timedelta(minutes=max(1, int(interval_minutes)))
    issued = _iso(issued_at)
    rows: list[tuple[Any, ...]] = []
    for slot in forecast:
        try:
            start = _parse_dt(str(slot["start"]))
            ch_kwh = float(slot["ch_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None or not math.isfinite(ch_kwh):
            continue
        horizon = (start - issued_at).total_seconds() / 3600.0
        if horizon < 0:
            continue
        end = start + interval
        def finite_or_none(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) else None

        rows.append(
            (
                issued,
                _iso(start),
                _iso(end),
                horizon,
                ch_kwh,
                finite_or_none(slot.get("temperature_c")),
                finite_or_none(slot.get("degree_days")),
                finite_or_none(coefficient_kwh_per_dd),
                str(slot.get("heating_mode") or ""),
                int(bool(slot.get("heating_enabled"))),
                None if slot.get("dhw_active") is None else int(bool(slot.get("dhw_active"))),
                finite_or_none(winter_mode_below_c),
                finite_or_none(summer_mode_above_c),
                str(forecast_status),
            )
        )
    if not rows:
        return 0
    before = db.total_changes
    with db:
        db.executemany(
            """
            INSERT OR IGNORE INTO ch_forecast_observations(
                issued_at,target_start,target_end,horizon_hours,forecast_ch_kwh,
                forecast_temperature_c,forecast_degree_days,coefficient_kwh_per_dd,
                forecast_heating_mode,forecast_heating_enabled,dhw_active,
                winter_mode_below_c,summer_mode_above_c,forecast_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    return db.total_changes - before


def _numeric_history(history: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for row in history:
        try:
            value = float(row["state"])
            stamp = _parse_dt(str(row.get("last_changed") or row.get("last_updated")))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append((stamp, value))
    out.sort(key=lambda item: item[0])
    return out


def _value_at_or_before(series: list[tuple[datetime, float]], at: datetime) -> float | None:
    latest: float | None = None
    for stamp, value in series:
        if stamp <= at:
            latest = value
        else:
            break
    return latest


def _energy_delta_strict(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> tuple[float | None, str]:
    start_value = _value_at_or_before(series, start)
    end_value = _value_at_or_before(series, end)
    if start_value is None or end_value is None:
        return None, "missing_energy_history"
    points = [(stamp, value) for stamp, value in series if start < stamp <= end]
    previous = start_value
    total = 0.0
    for _, value in points + [(end, end_value)]:
        delta = value - previous
        if delta < -1e-6:
            return None, "energy_counter_reset"
        if delta > 0:
            total += delta
        previous = value
    return total, "complete"


def _time_weighted_mean(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    current = _value_at_or_before(series, start)
    if current is None:
        return None
    cursor = start
    weighted = 0.0
    seconds = 0.0
    for stamp, value in series:
        if stamp <= start:
            continue
        if stamp >= end:
            break
        span = (stamp - cursor).total_seconds()
        weighted += current * span
        seconds += span
        current = value
        cursor = stamp
    span = (end - cursor).total_seconds()
    weighted += current * span
    seconds += span
    return weighted / seconds if seconds > 0 else None


def complete_pending_actuals(
    db: sqlite3.Connection,
    *,
    get_history: Callable[[str, datetime, datetime], list[dict[str, Any]]],
    ch_energy_entity: str,
    outdoor_temperature_entity: str,
    now: datetime,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> tuple[int, int, int]:
    ensure_schema(db)
    earliest = now - timedelta(hours=max(1, int(lookback_hours)))
    rows = db.execute(
        """
        SELECT DISTINCT target_start,target_end
        FROM ch_forecast_observations
        WHERE actual_ch_kwh IS NULL
          AND completed_at IS NULL
          AND target_end <= ?
          AND target_end >= ?
        ORDER BY target_start
        """,
        (_iso(now), _iso(earliest)),
    ).fetchall()
    if not rows:
        return 0, 0, 0

    slots = [(_parse_dt(str(start)), _parse_dt(str(end))) for start, end in rows]
    history_start = min(start for start, _ in slots) - timedelta(minutes=10)
    history_end = max(end for _, end in slots)
    energy = _numeric_history(get_history(ch_energy_entity, history_start, history_end))
    temperature = _numeric_history(get_history(outdoor_temperature_entity, history_start, history_end))

    completed = invalid = missing = 0
    with db:
        for start, end in slots:
            actual_kwh, quality = _energy_delta_strict(energy, start, end)
            actual_temp = _time_weighted_mean(temperature, start, end)
            if actual_kwh is None:
                if quality == "energy_counter_reset":
                    invalid += 1
                    db.execute(
                        """
                        UPDATE ch_forecast_observations
                        SET completed_at=?, data_quality=?
                        WHERE target_start=? AND actual_ch_kwh IS NULL AND completed_at IS NULL
                        """,
                        (_iso(now), quality, _iso(start)),
                    )
                else:
                    missing += 1
                continue
            if actual_temp is None:
                missing += 1
                continue
            before = db.total_changes
            db.execute(
                """
                UPDATE ch_forecast_observations
                SET actual_ch_kwh=?,
                    actual_mean_temperature_c=?,
                    error_kwh=forecast_ch_kwh-?,
                    completed_at=?,
                    data_quality='complete'
                WHERE target_start=? AND actual_ch_kwh IS NULL AND completed_at IS NULL
                """,
                (actual_kwh, actual_temp, actual_kwh, _iso(now), _iso(start)),
            )
            completed += db.total_changes - before
    return completed, invalid, missing


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "samples": 0,
            "distinct_days": 0,
            "forecast_kwh": 0.0,
            "actual_kwh": 0.0,
            "mean_bias_kwh": None,
            "mae_kwh": None,
            "rmse_kwh": None,
            "wape_pct": None,
        }
    errors = [float(row["error"]) for row in rows]
    actual = [float(row["actual"]) for row in rows]
    forecast = [float(row["forecast"]) for row in rows]
    actual_sum = sum(actual)
    return {
        "samples": len(rows),
        "distinct_days": len({row["day"] for row in rows}),
        "forecast_kwh": sum(forecast),
        "actual_kwh": actual_sum,
        "mean_bias_kwh": sum(errors) / len(errors),
        "mae_kwh": sum(abs(value) for value in errors) / len(errors),
        "rmse_kwh": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "wape_pct": (sum(abs(value) for value in errors) / actual_sum * 100.0) if actual_sum > 1e-9 else None,
    }


def _evidence(samples: int, days: int) -> str:
    if samples >= RELIABLE_SAMPLES and days >= RELIABLE_DAYS:
        return "reliable"
    if samples >= OBSERVATIONAL_SAMPLES and days >= OBSERVATIONAL_DAYS:
        return "observational"
    return "insufficient"


def accuracy_summary(
    db: sqlite3.Connection,
    *,
    now: datetime,
    winter_threshold_c: float,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_horizon_hours: float = 48.0,
) -> dict[str, Any]:
    ensure_schema(db)
    cutoff = now - timedelta(days=max(1, int(window_days)))
    raw = db.execute(
        """
        SELECT issued_at,target_start,horizon_hours,forecast_ch_kwh,actual_ch_kwh,
               error_kwh,actual_mean_temperature_c,forecast_heating_enabled,
               coefficient_kwh_per_dd,forecast_status
        FROM ch_forecast_observations
        WHERE data_quality='complete'
          AND actual_ch_kwh IS NOT NULL
          AND error_kwh IS NOT NULL
          AND completed_at >= ?
          AND completed_at <= ?
          AND horizon_hours >= 0
          AND horizon_hours <= ?
        ORDER BY completed_at,issued_at,target_start
        """,
        (_iso(cutoff), _iso(now), float(max_horizon_hours)),
    ).fetchall()

    rows: list[dict[str, Any]] = []
    for issued, target, horizon, forecast, actual, error, temp, heating_enabled, coefficient, status in raw:
        try:
            values = [float(horizon), float(forecast), float(actual), float(error), float(temp)]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in values):
            continue
        rows.append(
            {
                "issued": str(issued),
                "target": str(target),
                "horizon": values[0],
                "forecast": values[1],
                "actual": values[2],
                "error": values[3],
                "temperature": values[4],
                "heating_enabled": bool(heating_enabled),
                "coefficient": None if coefficient is None else float(coefficient),
                "status": str(status),
                "day": _parse_dt(str(target)).date().isoformat(),
            }
        )

    overall = _metrics(rows)
    horizons: dict[str, Any] = {}
    for name, lower, upper in HORIZON_BUCKETS:
        bucket = [row for row in rows if row["horizon"] >= lower and row["horizon"] < upper]
        horizons[name] = _metrics(bucket)

    bands: dict[str, Any] = {}
    for name, lower, upper in temperature_bands(float(winter_threshold_c)):
        selected = [
            row for row in rows
            if (lower is None or row["temperature"] >= lower)
            and (upper is None or row["temperature"] < upper)
        ]
        result = _metrics(selected)
        result.update({"lower_c": lower, "upper_c": upper, "evidence": _evidence(result["samples"], result["distinct_days"])})
        bands[name] = result

    heating_rows = [row for row in rows if row["heating_enabled"]]
    heating = _metrics(heating_rows)
    heating["evidence"] = _evidence(heating["samples"], heating["distinct_days"])
    heating["definition"] = "forecast_heating_enabled"

    return {
        "window_days": max(1, int(window_days)),
        "window_start": _iso(cutoff),
        "window_end": _iso(now),
        "error_definition": "forecast_minus_actual",
        "overall": overall,
        "horizons": horizons,
        "temperature_analysis": {
            "winter_threshold_c": float(winter_threshold_c),
            "bands": bands,
        },
        "heating_active": heating,
        "oldest_target": rows[0]["target"] if rows else None,
        "newest_target": rows[-1]["target"] if rows else None,
    }
