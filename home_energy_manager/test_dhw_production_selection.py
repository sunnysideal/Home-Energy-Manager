from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dhw_model import ensure_dhw_model_schema
from dhw_passive_learner import PassiveFit, persist_passive_fit
from dhw_production_selector import EXPECTED_PRODUCTION_SLOTS, SOURCE_KEY, select_dhw_forecast
from dhw_validation import confidence_result


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    ensure_dhw_model_schema(db)
    return db


def _param(db: sqlite3.Connection, name: str, value: float, count: int = 0) -> None:
    db.execute(
        "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) VALUES(?,?,?,?,NULL) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value,sample_count=excluded.sample_count,"
        "updated_at=excluded.updated_at",
        (name, value, count, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def _source(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM metadata WHERE key=?", (SOURCE_KEY,)).fetchone()
    return str(row[0]) if row else None


def _starts() -> list[datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(minute=30 if now.minute >= 30 else 0, second=0, microsecond=0) + timedelta(minutes=30)
    return [start + timedelta(minutes=30 * i) for i in range(EXPECTED_PRODUCTION_SLOTS)]


def _fresh_state(starts: list[datetime], values: list[float], *, complete: bool = True) -> dict:
    return {
        "state": str(sum(values)),
        "attributes": {
            "status": "published_shadow",
            "horizon_complete": complete,
            "forecast_slots": len(starts),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "forecast": [
                {"start": start.isoformat(), "dhw_kwh": value}
                for start, value in zip(starts, values)
            ],
        },
    }


def test_passive_fit_is_visible_to_readiness_gate() -> None:
    db = _db()
    persist_passive_fit(db, PassiveFit(1.1, 1.9, 0.2, 3609, 0.05))
    result = confidence_result(db)
    assert result.passive_samples == 3609


def test_selector_rejects_non_96_production_contract() -> None:
    db = _db()
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with pytest.raises(RuntimeError, match="requires 96 aligned slots"):
        select_dhw_forecast(db, [start], [0.5], lambda _: {})
    assert _source(db) == "invalid"


def test_selector_requires_trial_ready_instead_of_using_legacy() -> None:
    db = _db()
    starts = _starts()
    legacy = [0.5] * len(starts)
    state = _fresh_state(starts, [0.8] * len(starts))
    with pytest.raises(RuntimeError, match="structurally ready"):
        select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert _source(db) == "invalid"


def test_selector_uses_authoritative_thermal_when_trial_ready() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    thermal = [0.4 if i % 2 == 0 else 0.8 for i in range(len(starts))]
    state = _fresh_state(starts, thermal)
    legacy = [0.1] * len(starts)

    result = select_dhw_forecast(db, starts, legacy, lambda _: state)

    assert result.source == "thermal"
    assert result.values == thermal
    assert result.reason == "thermal_authoritative"
    assert _source(db) == "thermal"


def test_selector_rejects_stale_thermal_forecast_without_legacy_fallback() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    stale = _fresh_state(starts, [0.9] * len(starts))
    stale["attributes"]["last_updated"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with pytest.raises(RuntimeError, match="thermal_forecast_stale"):
        select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: stale)
    assert _source(db) == "invalid"


def test_selector_rejects_horizon_marked_incomplete_without_fallback() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts), complete=False)
    with pytest.raises(RuntimeError, match="thermal_horizon_incomplete"):
        select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)
    assert _source(db) == "invalid"


def test_selector_rejects_missing_requested_slot_without_fallback() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts))
    state["attributes"]["forecast"] = state["attributes"]["forecast"][:-1]
    state["attributes"]["forecast_slots"] = len(starts)
    with pytest.raises(RuntimeError, match="thermal_horizon_incomplete"):
        select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)
    assert _source(db) == "invalid"


def test_selector_rejects_shifted_96_slot_forecast_without_fallback() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    shifted = [start + timedelta(minutes=30) for start in starts]
    state = _fresh_state(shifted, [0.7] * len(shifted))
    with pytest.raises(RuntimeError, match="thermal_forecast_incomplete"):
        select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)
    assert _source(db) == "invalid"


def test_performance_diagnostic_does_not_demote_authoritative_thermal() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    _param(db, "dhw_performance_bad", 1.0)
    starts = _starts()
    thermal = [0.7] * len(starts)
    state = _fresh_state(starts, thermal)

    first = select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)
    second = select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)

    assert first.source == "thermal"
    assert second.source == "thermal"
    assert first.reason == second.reason == "thermal_authoritative"
    assert first.values == second.values == thermal


def test_promotion_diagnostic_does_not_gate_authoritative_thermal() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    _param(db, "dhw_promotion_ready", 0.0)
    starts = _starts()
    thermal = [0.7] * len(starts)
    state = _fresh_state(starts, thermal)

    result = select_dhw_forecast(db, starts, [0.2] * len(starts), lambda _: state)

    assert result.source == "thermal"
    assert result.reason == "thermal_authoritative"
    assert result.values == thermal
