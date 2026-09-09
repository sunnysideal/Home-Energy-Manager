"""Guarded selection of legacy vs enhanced thermal DHW forecast.

This module belongs entirely to the ASHP forecaster. It does not alter controller or
whole-home behaviour. Once the learned physical/demand model is structurally ready,
production uses the thermal forecast immediately while legacy continues in parallel as
comparator and fallback. Safety failures fall back immediately; proven poor performance
falls back with hysteresis and requires sustained good evidence before retrying thermal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Callable


THERMAL_FORECAST_ENTITY = "sensor.ashp_dhw_thermal_forecast_next_48h"
SOURCE_KEY = "dhw_production_source"
PROMOTION_STREAK_KEY = "dhw_production_promotion_streak"
DEMOTION_STREAK_KEY = "dhw_production_demotion_streak"
FALLBACK_LATCH_KEY = "dhw_production_quality_fallback_latched"
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


def _metadata_text(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _metadata_int(db: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(_metadata_text(db, key, str(default)))
    except ValueError:
        return default


def _set_metadata(db: sqlite3.Connection, key: str, value: str | int) -> None:
    db.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _persist_state(
    db: sqlite3.Connection,
    source: str,
    promotion_streak: int,
    demotion_streak: int,
    *,
    fallback_latched: int | None = None,
) -> None:
    with db:
        _set_metadata(db, SOURCE_KEY, source)
        _set_metadata(db, PROMOTION_STREAK_KEY, max(0, promotion_streak))
        _set_metadata(db, DEMOTION_STREAK_KEY, max(0, demotion_streak))
        if fallback_latched is not None:
            _set_metadata(db, FALLBACK_LATCH_KEY, 1 if fallback_latched else 0)


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
    """Select production DHW source using optimistic trial plus guarded fallback.

    Normal path:
    * legacy is used only until ``dhw_trial_ready`` says the learned model has enough
      physical/demand evidence and a fresh, complete 96-slot thermal forecast exists;
    * thermal then becomes authoritative immediately, before forward validation exists;
    * legacy continues to be forecast and scored in parallel;
    * stale/missing/incomplete thermal data or loss of structural readiness falls back
      immediately;
    * ``dhw_performance_bad`` must persist for ``demotion_failures`` production runs before
      latching a quality fallback to legacy;
    * once quality fallback is latched, thermal is retried only after ``dhw_promotion_ready``
      is true for ``retry_successes`` consecutive runs. This prevents a known-poor model from
      bouncing straight back into production merely because its structure remains valid.
    """
    legacy = list(legacy_values)
    if len(starts) != len(legacy):
        _persist_state(db, "legacy", 0, 0)
        return SelectionResult(legacy, "legacy", "legacy_length_mismatch")
    if len(starts) != EXPECTED_PRODUCTION_SLOTS:
        _persist_state(db, "legacy", 0, 0)
        return SelectionResult(legacy, "legacy", "unsupported_production_horizon")

    current_source = _metadata_text(db, SOURCE_KEY, "legacy")
    if current_source not in {"legacy", "thermal"}:
        current_source = "legacy"
    promotion_streak = _metadata_int(db, PROMOTION_STREAK_KEY, 0)
    demotion_streak = _metadata_int(db, DEMOTION_STREAK_KEY, 0)
    fallback_latched = _metadata_int(db, FALLBACK_LATCH_KEY, 0) != 0

    trial_ready = _parameter_value(db, "dhw_trial_ready")
    if trial_ready is None or trial_ready < 0.5:
        _persist_state(db, "legacy", 0, 0)
        return SelectionResult(legacy, "legacy", "trial_not_ready")

    try:
        state = get_state(THERMAL_FORECAST_ENTITY)
    except Exception:
        _persist_state(db, "legacy", 0, 0)
        return SelectionResult(legacy, "legacy", "thermal_entity_read_failed")

    thermal, thermal_reason = _thermal_values(state, starts, max_age_minutes=max_age_minutes)
    if thermal is None:
        # Integrity failures are immediate safety fallbacks. Do not latch them as quality
        # failures: a later fresh/complete thermal forecast may resume normally.
        _persist_state(db, "legacy", 0, 0)
        return SelectionResult(legacy, "legacy", thermal_reason)

    performance_bad_value = _parameter_value(db, "dhw_performance_bad")
    performance_bad = bool(performance_bad_value is not None and performance_bad_value >= 0.5)
    promotion_value = _parameter_value(db, "dhw_promotion_ready")
    proven_good = bool(promotion_value is not None and promotion_value >= 0.5)

    if current_source == "thermal":
        if not performance_bad:
            _persist_state(db, "thermal", 0, 0)
            return SelectionResult(thermal, "thermal", "thermal_trial_active")
        demotion_streak += 1
        required_failures = max(1, demotion_failures)
        if demotion_streak >= required_failures:
            _persist_state(db, "legacy", 0, 0, fallback_latched=1)
            return SelectionResult(legacy, "legacy", "performance_fallback_latched")
        _persist_state(db, "thermal", 0, demotion_streak)
        return SelectionResult(
            thermal,
            "thermal",
            f"performance_warning_{demotion_streak}_of_{required_failures}",
        )

    if fallback_latched:
        if not proven_good:
            _persist_state(db, "legacy", 0, 0, fallback_latched=1)
            return SelectionResult(legacy, "legacy", "quality_fallback_waiting_for_recovery")
        promotion_streak += 1
        required = max(1, retry_successes)
        if promotion_streak >= required:
            _persist_state(db, "thermal", 0, 0, fallback_latched=0)
            return SelectionResult(thermal, "thermal", "recovered_after_sustained_validation")
        _persist_state(db, "legacy", promotion_streak, 0, fallback_latched=1)
        return SelectionResult(
            legacy,
            "legacy",
            f"recovery_streak_{promotion_streak}_of_{required}",
        )

    # First use of the enhanced model is intentionally optimistic: structural readiness
    # plus a valid horizon is sufficient. Real validation now determines whether it stays.
    _persist_state(db, "thermal", 0, 0, fallback_latched=0)
    return SelectionResult(thermal, "thermal", "trial_ready_using_thermal")
