from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from components.ashp_forecaster.app.temperature_bias import (
    temperature_bands,
    temperature_shadow_analysis,
)
from components.ashp_forecaster.app.weather_observations import ensure_schema


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    ensure_schema(db)
    return db


def _insert(
    db: sqlite3.Connection,
    *,
    issued: datetime,
    horizon: float,
    actual: float,
    error: float,
) -> None:
    target = issued + timedelta(hours=horizon)
    forecast = actual - error
    db.execute(
        "INSERT INTO weather_forecast_observations(issued_at,target_ts,horizon_hours,forecast_temperature_c,source_entity,actual_temperature_c,actual_observed_at,error_c) VALUES(?,?,?,?,?,?,?,?)",
        (
            issued.isoformat(),
            target.isoformat(),
            horizon,
            forecast,
            "weather.home",
            actual,
            target.isoformat(),
            error,
        ),
    )
    db.commit()


def test_temperature_bands_are_fine_below_dynamic_winter_threshold() -> None:
    bands = temperature_bands(11.0)
    assert bands == [
        ("lt_0c", None, 0.0),
        ("0_2c", 0.0, 2.0),
        ("2_4c", 2.0, 4.0),
        ("4_6c", 4.0, 6.0),
        ("6_8c", 6.0, 8.0),
        ("8_10c", 8.0, 10.0),
        ("10_11c", 10.0, 11.0),
        ("11_13c", 11.0, 13.0),
        ("13_15c", 13.0, 15.0),
        ("ge_15c", 15.0, None),
    ]
    shifted = temperature_bands(9.0)
    assert ("8_9c", 8.0, 9.0) in shifted
    assert ("9_13c", 9.0, 13.0) in shifted
    assert all(name != "10_11c" for name, _, _ in shifted)


def test_heating_active_summary_uses_actual_temperature_below_threshold() -> None:
    db = _db()
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for day in range(3):
        for index in range(20):
            issued = now - timedelta(days=day + 1, hours=8, minutes=index)
            _insert(db, issued=issued, horizon=3.0, actual=9.0, error=0.5)
            _insert(db, issued=issued + timedelta(seconds=1), horizon=3.0, actual=12.0, error=1.0)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    heating = result["heating_active"]
    assert heating["threshold_c"] == 11.0
    assert heating["samples"] == 60
    assert heating["distinct_days"] >= 3
    assert heating["median_bias_c"] == 0.5
    assert result["bands"]["8_10c"]["evidence"] == "reliable"
    assert result["bands"]["11_13c"]["samples"] == 60
    assert result["calibration_applied"] is False


def test_bands_mature_independently_without_waiting_for_coldest_range() -> None:
    db = _db()
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for day in range(3):
        for index in range(20):
            issued = now - timedelta(days=day + 1, hours=6, minutes=index)
            _insert(db, issued=issued, horizon=8.0, actual=7.0, error=0.4)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    assert result["bands"]["6_8c"]["evidence"] == "reliable"
    assert result["bands"]["lt_0c"]["evidence"] == "insufficient"
    assert result["heating_active"]["evidence"] == "reliable"


def test_repeated_snapshots_from_one_day_do_not_become_reliable() -> None:
    db = _db()
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for index in range(80):
        issued = now - timedelta(hours=12, minutes=index)
        _insert(db, issued=issued, horizon=3.0, actual=7.0, error=0.5)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    band = result["bands"]["6_8c"]
    assert band["samples"] == 80
    assert band["distinct_days"] <= 2
    assert band["evidence"] != "reliable"


def test_candidate_validates_partial_heating_range_chronologically() -> None:
    db = _db()
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    # Create four distinct days over two adjacent reliable bands. Bias rises as
    # temperature falls, so a temperature model should beat a single horizon median.
    for day in range(4):
        for index in range(30):
            actual = 6.5 if index % 2 == 0 else 8.5
            error = 1.35 - 0.1 * actual
            issued = now - timedelta(days=4 - day, hours=8, minutes=index)
            _insert(db, issued=issued, horizon=3.0, actual=actual, error=error)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    candidate = result["candidate_model"]
    assert candidate["eligible"] is True
    assert candidate["validated_min_c"] == 6.5
    assert candidate["validated_max_c"] == 8.5
    assert candidate["validated_bands"] == ["6_8c", "8_10c"]
    assert candidate["training_days"] >= 2
    assert candidate["holdout_days"] >= 1
    assert candidate["temperature_linear"] is not None
    assert candidate["temperature_plus_horizon"] is not None
    assert candidate["selected_model"] in {"temperature_linear", "temperature_plus_horizon", "global_horizon"}
    assert result["bands"]["lt_0c"]["samples"] == 0


def test_open_ended_cold_band_does_not_claim_unobserved_range() -> None:
    db = _db()
    now = datetime(2027, 1, 20, 12, tzinfo=timezone.utc)
    for day in range(4):
        for index in range(30):
            actual = -3.0 if index % 2 == 0 else 1.0
            error = 0.9 - 0.1 * actual
            issued = now - timedelta(days=4 - day, hours=8, minutes=index)
            _insert(db, issued=issued, horizon=3.0, actual=actual, error=error)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    candidate = result["candidate_model"]
    assert candidate["validated_bands"] == ["lt_0c", "0_2c"]
    assert candidate["validated_min_c"] == -3.0
    assert candidate["validated_max_c"] == 1.0
    assert candidate["eligible"] is True


def test_candidate_stays_global_horizon_when_temperature_range_is_sparse() -> None:
    db = _db()
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for day in range(2):
        for index in range(20):
            issued = now - timedelta(days=day + 1, hours=7, minutes=index)
            _insert(db, issued=issued, horizon=4.0, actual=9.0, error=0.4)
    result = temperature_shadow_analysis(db, now=now, winter_threshold_c=11.0)
    candidate = result["candidate_model"]
    assert candidate["eligible"] is False
    assert candidate["selected_model"] == "global_horizon"
    assert candidate["reason"] in {
        "insufficient_reliable_temperature_range",
        "reliable_range_not_mature_enough",
    }
