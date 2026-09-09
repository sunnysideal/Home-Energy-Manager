"""Guarded selection of legacy vs enhanced thermal DHW forecast.

This module belongs entirely to the ASHP forecaster. It does not alter controller or
whole-home behaviour. Production may consume the thermal forecast only after the
persisted validation gate says promotion is ready and the published thermal forecast
is fresh and covers every requested production slot. Otherwise legacy is returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Callable


THERMAL_FORECAST_ENTITY = "sensor.ashp_dhw_thermal_forecast_next_48h"


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

    updated = _parse_timestamp(attrs.get("last_updated"))
    if updated is None:
        return None, "thermal_forecast_missing_timestamp"
    age = datetime.now(timezone.utc) - updated
    if age < timedelta(minutes=-2) or age > timedelta(minutes=max_age_minutes):
        return None, "thermal_forecast_stale"

    rows = attrs.get("forecast") if isinstance(attrs.get("forecast"), list) else []
    by_start: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _parse_timestamp(row.get("start"))
        try:
            value = max(0.0, float(row.get("dhw_kwh", 0.0)))
        except (TypeError, ValueError):
            continue
        if ts is not None:
            by_start[ts.isoformat()] = value

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
) -> SelectionResult:
    """Return thermal values only when all production safety gates pass.

    The selector is deliberately fail-safe: every missing/stale/partial/error condition
    returns the already-built legacy forecast. ``dhw_promotion_ready`` is written by the
    independent validation process after sufficient forward comparison against actuals.
    """
    if len(starts) != len(legacy_values):
        return SelectionResult(list(legacy_values), "legacy", "legacy_length_mismatch")

    promotion = _parameter_value(db, "dhw_promotion_ready")
    if promotion is None or promotion < 0.5:
        return SelectionResult(list(legacy_values), "legacy", "promotion_not_ready")

    thermal_ready = _parameter_value(db, "dhw_thermal_ready")
    if thermal_ready is None or thermal_ready < 0.5:
        return SelectionResult(list(legacy_values), "legacy", "thermal_not_ready")

    try:
        state = get_state(THERMAL_FORECAST_ENTITY)
    except Exception:
        return SelectionResult(list(legacy_values), "legacy", "thermal_entity_read_failed")

    thermal, reason = _thermal_values(state, starts, max_age_minutes=max_age_minutes)
    if thermal is None:
        return SelectionResult(list(legacy_values), "legacy", reason)
    return SelectionResult(thermal, "thermal", "promotion_ready_and_forecast_valid")
