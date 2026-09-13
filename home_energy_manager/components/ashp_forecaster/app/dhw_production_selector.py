"""Thermal-only production selection for the DHW forecast.

Legacy DHW values may continue to be calculated and scored as a comparator, but they
are never substituted into production. A fresh, complete, correctly aligned thermal
horizon is the production-readiness contract; learned readiness/confidence flags remain
diagnostics and must not veto a forecast the thermal publisher has successfully built.
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


def _contract_diagnostics(state: dict[str, Any] | None, starts: list[datetime]) -> str:
    attrs = state.get("attributes") if state and isinstance(state.get("attributes"), dict) else {}
    requested = [s.astimezone(timezone.utc).isoformat() for s in starts]
    requested_set = set(requested)
    rows = attrs.get("forecast") if isinstance(attrs.get("forecast"), list) else []

    published: list[str] = []
    duplicates: list[str] = []
    malformed = 0
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        ts = _parse_timestamp(row.get("start"))
        if ts is None:
            malformed += 1
            continue
        key = ts.isoformat()
        if key in seen:
            duplicates.append(key)
        else:
            seen.add(key)
            published.append(key)

    published.sort()
    published_set = set(published)
    missing = [key for key in requested if key not in published_set]
    extras = [key for key in published if key not in requested_set]

    updated = _parse_timestamp(attrs.get("last_updated"))
    age_seconds = None
    if updated is not None:
        age_seconds = round((datetime.now(timezone.utc) - updated).total_seconds(), 1)

    def _first(values: list[str]) -> str:
        return values[0] if values else "none"

    def _last(values: list[str]) -> str:
        return values[-1] if values else "none"

    def _sample(values: list[str], limit: int = 5) -> str:
        if not values:
            return "[]"
        body = ",".join(values[:limit])
        suffix = f",+{len(values) - limit}more" if len(values) > limit else ""
        return f"[{body}{suffix}]"

    return (
        f"requested_start={_first(requested)},requested_end={_last(requested)},requested_rows={len(requested)},"
        f"published_start={_first(published)},published_end={_last(published)},published_rows={len(published)},"
        f"forecast_slots_attr={attrs.get('forecast_slots')},expected_slots_attr={attrs.get('expected_forecast_slots')},"
        f"coverage_slots_attr={attrs.get('coverage_forecast_slots')},horizon_complete={attrs.get('horizon_complete')},"
        f"production_start_attr={attrs.get('production_start')},production_end_attr={attrs.get('production_end')},"
        f"last_updated={attrs.get('last_updated')},age_seconds={age_seconds},status={attrs.get('status')},"
        f"missing={_sample(missing)},extras={_sample(extras)},duplicates={_sample(duplicates)},malformed_rows={malformed}"
    )


def _failure(reason: str, state: dict[str, Any] | None, starts: list[datetime]) -> tuple[None, str]:
    return None, f"{reason};{_contract_diagnostics(state, starts)}"


def _thermal_values(
    state: dict[str, Any] | None,
    starts: list[datetime],
    *,
    max_age_minutes: float,
) -> tuple[list[float] | None, str]:
    if not state:
        return _failure("thermal_entity_unavailable", state, starts)
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    if attrs.get("status") != "published_shadow":
        return _failure("thermal_forecast_not_published", state, starts)
    if attrs.get("horizon_complete") is not True:
        return _failure("thermal_horizon_incomplete", state, starts)
    try:
        published_slots = int(attrs.get("forecast_slots"))
    except (TypeError, ValueError):
        return _failure("thermal_horizon_incomplete", state, starts)
    if published_slots < len(starts):
        return _failure("thermal_horizon_incomplete", state, starts)

    updated = _parse_timestamp(attrs.get("last_updated"))
    if updated is None:
        return _failure("thermal_forecast_missing_timestamp", state, starts)
    age = datetime.now(timezone.utc) - updated
    if age < timedelta(minutes=-2) or age > timedelta(minutes=max_age_minutes):
        return _failure("thermal_forecast_stale", state, starts)

    rows = attrs.get("forecast") if isinstance(attrs.get("forecast"), list) else []
    if len(rows) < len(starts):
        return _failure("thermal_horizon_incomplete", state, starts)

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
            return _failure("thermal_forecast_duplicate_slot", state, starts)
        by_start[key] = value

    out: list[float] = []
    for start in starts:
        key = start.astimezone(timezone.utc).isoformat()
        if key not in by_start:
            return _failure("thermal_forecast_incomplete", state, starts)
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
    ASHP component boundary. It is never selected for production.

    Production readiness is proven by the published thermal horizon itself. Persisted
    model confidence/readiness flags are diagnostic state and can temporarily lag live
    sensor recovery or a freshly published forecast, so they must not block production.
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
