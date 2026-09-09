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
    start = datetime.now(timezone.utc).replace(minute=30 if datetime.now(timezone.utc).minute >= 30 else 0, second=0, microsecond=0)
    start += timedelta(minutes=30)
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


def test_selector_stays_legacy_until_promotion_ready() -> None:
    db = _db()
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    legacy = [0.5] * len(starts)
    state = _fresh_state(starts, [0.8] * len(starts))
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "promotion_not_ready"


def test_selector_requires_sustained_promotion_before_using_thermal() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    thermal = [0.4 if i % 2 == 0 else 0.8 for i in range(len(starts))]
    state = _fresh_state(starts, thermal)
    legacy = [0.1] * len(starts)

    first = select_dhw_forecast(db, starts, legacy, lambda _: state)
    second = select_dhw_forecast(db, starts, legacy, lambda _: state)
    third = select_dhw_forecast(db, starts, legacy, lambda _: state)

    assert first.source == "legacy" and first.reason == "promotion_streak_1_of_3"
    assert second.source == "legacy" and second.reason == "promotion_streak_2_of_3"
    assert third.source == "thermal"
    assert third.values == thermal
    assert third.reason == "promoted_after_sustained_validation"


def test_selector_falls_back_immediately_when_thermal_forecast_is_stale() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    thermal = [0.9] * len(starts)
    fresh = _fresh_state(starts, thermal)
    legacy = [0.2] * len(starts)
    for _ in range(3):
        select_dhw_forecast(db, starts, legacy, lambda _: fresh)

    stale = _fresh_state(starts, thermal)
    stale["attributes"]["last_updated"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    result = select_dhw_forecast(db, starts, legacy, lambda _: stale)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "thermal_forecast_stale"


def test_selector_rejects_horizon_marked_incomplete() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts), complete=False)
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "thermal_horizon_incomplete"


def test_selector_falls_back_when_any_requested_slot_is_missing() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.9] * len(starts))
    state["attributes"]["forecast"] = state["attributes"]["forecast"][:-1]
    state["attributes"]["forecast_slots"] = len(starts)  # metadata alone must not make it complete
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.values == legacy
    assert result.reason == "thermal_horizon_incomplete"


def test_selector_rejects_shifted_96_slot_forecast() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    shifted = [start + timedelta(minutes=30) for start in starts]
    state = _fresh_state(shifted, [0.7] * len(shifted))
    legacy = [0.2] * len(starts)
    result = select_dhw_forecast(db, starts, legacy, lambda _: state)
    assert result.source == "legacy"
    assert result.reason == "thermal_forecast_incomplete"


def test_active_thermal_needs_two_quality_failures_before_demotion() -> None:
    db = _db()
    _param(db, "dhw_promotion_ready", 1.0)
    _param(db, "dhw_thermal_ready", 1.0)
    starts = _starts()
    state = _fresh_state(starts, [0.7] * len(starts))
    legacy = [0.2] * len(starts)
    for _ in range(3):
        select_dhw_forecast(db, starts, legacy, lambda _: state)

    _param(db, "dhw_promotion_ready", 0.0)
    warning = select_dhw_forecast(db, starts, legacy, lambda _: state)
    demoted = select_dhw_forecast(db, starts, legacy, lambda _: state)

    assert warning.source == "thermal"
    assert warning.reason == "quality_gate_warning_1_of_2"
    assert demoted.source == "legacy"
    assert demoted.reason == "quality_gate_failed"
