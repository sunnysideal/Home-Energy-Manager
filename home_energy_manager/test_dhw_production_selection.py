from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dhw_model import ensure_dhw_model_schema
from dhw_passive_learner import PassiveFit, persist_passive_fit
from dhw_production_selector import EXPECTED_PRODUCTION_SLOTS, select_dhw_forecast
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
    result = select_dhw_forecast(db, [start], [0.5], lambda _: {})
    assert result.source == "legacy"
    assert result.reason == "unsupported_production_horizon"


def test_selector_stays_legacy_until_trial_ready() -> None:
    db = _db()
    starts = _starts()
    legacy = [0.5] * len(starts)
    state = _fresh_state(starts, [0.8] * len(starts))
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "trial_not_ready"


def test_selector_uses_thermal_immediately_when_trial_ready() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    thermal = [0.4 if i % 2 == 0 else 0.8 for i in range(len(starts))]
    state = _fresh_state(starts, thermal)
    legacy = [0.1] * len(starts)

    result = select_dhw_forecast(db, starts, legacy, lambda _: state)

    assert result.source == "thermal"
    assert result.values == thermal
    assert result.reason == "trial_ready_using_thermal"


def test_selector_falls_back_immediately_when_thermal_forecast_is_stale() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    thermal = [0.9] * len(starts)
    fresh = _fresh_state(starts, thermal)
    legacy = [0.2] * len(starts)
    first = select_dhw_forecast(db, starts, legacy, lambda _: fresh)
    assert first.source == "thermal"

    stale = _fresh_state(starts, thermal)
    stale["attributes"]["last_updated"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    result = select_dhw_forecast(db, starts, legacy, lambda _: stale)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "thermal_forecast_stale"


def test_selector_rejects_horizon_marked_incomplete() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts), complete=False)
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.reason == "thermal_horizon_incomplete"


def test_selector_falls_back_when_any_requested_slot_is_missing() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts))
    state["attributes"]["forecast"] = state["attributes"]["forecast"][:-1]
    state["attributes"]["forecast_slots"] = len(starts)
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.reason == "thermal_horizon_incomplete"


def test_selector_rejects_shifted_96_slot_forecast() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    shifted = [start + timedelta(minutes=30) for start in starts]
    state = _fresh_state(shifted, [0.7] * len(shifted))
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.reason == "thermal_forecast_incomplete"


def test_active_thermal_needs_two_bad_performance_checks_before_latched_fallback() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    _param(db, "dhw_performance_bad", 0.0)
    starts = _starts()
    state = _fresh_state(starts, [0.7] * len(starts))
    legacy = [0.2] * len(starts)
    active = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert active.source == "thermal"

    _param(db, "dhw_performance_bad", 1.0)
    warning = select_dhw_forecast(db, starts, legacy, lambda _: state)
    demoted = select_dhw_forecast(db, starts, legacy, lambda _: state)

    assert warning.source == "thermal"
    assert warning.reason == "performance_warning_1_of_2"
    assert demoted.source == "legacy"
    assert demoted.reason == "performance_fallback_latched"


def test_latched_quality_fallback_requires_sustained_proven_recovery() -> None:
    db = _db()
    _param(db, "dhw_trial_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.7] * len(starts))
    legacy = [0.2] * len(starts)
    select_dhw_forecast(db, starts, legacy, lambda _: state)
    _param(db, "dhw_performance_bad", 1.0)
    select_dhw_forecast(db, starts, legacy, lambda _: state)
    fallback = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert fallback.source == "legacy"

    _param(db, "dhw_performance_bad", 0.0)
    _param(db, "dhw_promotion_ready", 0.0)
    waiting = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert waiting.source == "legacy"
    assert waiting.reason == "quality_fallback_waiting_for_recovery"

    _param(db, "dhw_promotion_ready", 1.0)
    first = select_dhw_forecast(db, starts, legacy, lambda _: state)
    second = select_dhw_forecast(db, starts, legacy, lambda _: state)
    third = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert first.reason == "recovery_streak_1_of_3"
    assert second.reason == "recovery_streak_2_of_3"
    assert third.source == "thermal"
    assert third.reason == "recovered_after_sustained_validation"
