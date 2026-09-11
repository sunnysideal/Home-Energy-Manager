"""Thermal-only production selection for the DHW forecast.

Legacy DHW values may continue to be calculated and scored as a comparator, but they
are never substituted into production.  Once the learned thermal model is structurally
ready, a fresh complete thermal horizon is required; an invalid/missing thermal horizon
is surfaced as an error rather than silently publishing a contradictory fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Callable


THERMAL_FORECAST_ENTITY = "sensor.ashp_dhw_thermal_forecast_next_48h"
SOURCE_KEY = "dhw_production_source"
EXPECTED_PRODUCTION_SLOTS = 96


@dataclass(frozen=True)
class SelectionResult:
    values: list[float]
    source: str
    reason: str


def _parameter_value(db: sqlite3.Connection, name: str) -> float | None:
    row = db.execute("SELECT value FROM dhw_model_parameters WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _set_metadata(db: sqlite3.Connection, key: str, value: str | int) -> None:
    db.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _persist_source(db: sqlite3.Connection, source: str) -> None:
    with db:
        _set_metadata(db, SOURCE_KEY, source)


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _thermal_values(
    state: dict[str, Any] | None,
    starts: list[datetime],
    *,
    max_age_minutes: float,
) -> tuple[list[float] | None, str]:
    if not state:
        return None, "thermal_entity_unavailable"
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    if attrs.get("status") != "published_shadow":
        return None, "thermal_forecast_not_published"
    if attrs.get("horizon_complete") is not True:
        return None, "thermal_horizon_incomplete"
    try:
        published_slots = int(attrs.get("forecast_slots"))
    except (TypeError, ValueError):
        return None, "thermal_horizon_incomplete"
    if published_slots != len(starts):
        return None, "thermal_horizon_incomplete"

    updated = _parse_timestamp(attrs.get("last_updated"))
    if updated is None:
        return None, "thermal_forecast_missing_timestamp"
    age = datetime.now(timezone.utc) - updated
    if age < timedelta(minutes=-2) or age > timedelta(minutes=max_age_minutes):
        return None, "thermal_forecast_stale"

    rows = attrs.get("forecast") if isinstance(attrs.get("forecast"), list) else []
    if len(rows) != len(starts):
        return None, "thermal_horizon_incomplete"

    by_start: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _parse_timestamp(row.get("start"))
        try:
            value = max(0.0, float(row.get("dhw_kwh", 0.0)))
        except (TypeError, ValueError):
            continue
        if ts is None:
            continue
        key = ts.isoformat()
        if key in by_start:
            return None, "thermal_forecast_duplicate_slot"
        by_start[key] = value

    out: list[float] = []
    for start in starts:
        key = start.astimezone(timezone.utc).isoformat()
        if key not in by_start:
            return None, "thermal_forecast_incomplete"
        out.append(by_start[key])
    return out, "thermal_ready"


def select_dhw_forecast(
    db: sqlite3.Connection,
    starts: list[datetime],
    legacy_values: list[float],
    get_state: Callable[[str], dict[str, Any]],
    *,
    max_age_minutes: float = 25.0,
    retry_successes: int = 3,
    demotion_failures: int = 2,
) -> SelectionResult:
    """Return the aligned thermal DHW production forecast, with no legacy fallback.

    ``legacy_values`` is deliberately retained in the interface so the existing legacy
    calculation can continue to feed validation/comparison without changing the public
    ASHP component boundary.  It is never selected for production.
    """
    del retry_successes, demotion_failures
    if len(starts) != len(legacy_values):
        _persist_source(db, "invalid")
        raise RuntimeError("DHW comparator length does not match production horizon")
    if len(starts) != EXPECTED_PRODUCTION_SLOTS:
        _persist_source(db, "invalid")
        raise RuntimeError(
            f"DHW thermal production requires {EXPECTED_PRODUCTION_SLOTS} aligned slots; got {len(starts)}"
        )

    trial_ready = _parameter_value(db, "dhw_trial_ready")
    if trial_ready is None or trial_ready < 0.5:
        _persist_source(db, "invalid")
        raise RuntimeError("DHW thermal model is not structurally ready; legacy fallback is disabled")

    try:
        state = get_state(THERMAL_FORECAST_ENTITY)
    except Exception as exc:
        _persist_source(db, "invalid")
        raise RuntimeError("DHW thermal forecast entity could not be read; legacy fallback is disabled") from exc

    thermal, reason = _thermal_values(state, starts, max_age_minutes=max_age_minutes)
    if thermal is None:
        _persist_source(db, "invalid")
        raise RuntimeError(f"DHW thermal forecast unavailable ({reason}); legacy fallback is disabled")

    _persist_source(db, "thermal")
    return SelectionResult(thermal, "thermal", "thermal_authoritative")
