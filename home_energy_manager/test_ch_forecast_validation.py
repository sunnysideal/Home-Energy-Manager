from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from components.ashp_forecaster.app.ch_forecast_validation import (
    accuracy_summary,
    complete_pending_actuals,
    ensure_schema,
    record_published_forecast,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    ensure_schema(db)
    return db


def _forecast(start: datetime, *, ch_kwh: float = 0.4, temp: float = 8.0, heating: bool = True) -> list[dict]:
    return [{
        "start": start.isoformat(),
        "temperature_c": temp,
        "heating_mode": "winter" if heating else "summer",
        "heating_enabled": heating,
        "dhw_active": False,
        "degree_days": 0.15625 if heating else 0.0,
        "ch_kwh": ch_kwh,
    }]


def test_records_exact_issued_forecast_and_is_idempotent() -> None:
    db = _db()
    issued = datetime(2026, 10, 7, 8, 5, tzinfo=timezone.utc)
    target = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    kwargs = dict(
        issued_at=issued,
        forecast=_forecast(target, ch_kwh=0.42),
        interval_minutes=30,
        coefficient_kwh_per_dd=1.163,
        winter_mode_below_c=13.0,
        summer_mode_above_c=14.0,
        forecast_status="healthy",
    )
    assert record_published_forecast(db, **kwargs) == 1
    assert record_published_forecast(db, **kwargs) == 0
    row = db.execute(
        "SELECT target_start,target_end,horizon_hours,forecast_ch_kwh,"
        "coefficient_kwh_per_dd,winter_mode_below_c,summer_mode_above_c,forecast_status "
        "FROM ch_forecast_observations"
    ).fetchone()
    assert row[0] == target.isoformat()
    assert row[1] == (target + timedelta(minutes=30)).isoformat()
    assert row[2] == 55 / 60
    assert row[3:] == (0.42, 1.163, 13.0, 14.0, "healthy")


def test_one_actual_slot_completes_every_issued_snapshot_without_reforecasting() -> None:
    db = _db()
    target = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    for issued, value in [
        (target - timedelta(hours=6), 0.60),
        (target - timedelta(hours=1), 0.45),
    ]:
        record_published_forecast(
            db,
            issued_at=issued,
            forecast=_forecast(target, ch_kwh=value),
            interval_minutes=30,
            coefficient_kwh_per_dd=1.163,
            winter_mode_below_c=13.0,
            summer_mode_above_c=14.0,
            forecast_status="healthy",
        )

    def history(entity: str, start: datetime, end: datetime):
        if entity == "sensor.ch":
            return [
                {"state": "100.0", "last_changed": "2026-10-07T08:50:00+00:00"},
                {"state": "100.5", "last_changed": "2026-10-07T09:30:00+00:00"},
            ]
        if entity == "sensor.temp":
            return [
                {"state": "8.0", "last_changed": "2026-10-07T08:50:00+00:00"},
                {"state": "10.0", "last_changed": "2026-10-07T09:15:00+00:00"},
            ]
        raise AssertionError(entity)

    completed, invalid, missing = complete_pending_actuals(
        db,
        get_history=history,
        ch_energy_entity="sensor.ch",
        outdoor_temperature_entity="sensor.temp",
        now=datetime(2026, 10, 7, 9, 40, tzinfo=timezone.utc),
    )
    assert (completed, invalid, missing) == (2, 0, 0)
    rows = db.execute(
        "SELECT forecast_ch_kwh,actual_ch_kwh,error_kwh,actual_mean_temperature_c "
        "FROM ch_forecast_observations ORDER BY issued_at"
    ).fetchall()
    assert rows[0][0] == 0.60
    assert rows[1][0] == 0.45
    assert all(row[1] == 0.5 for row in rows)
    assert rows[0][2] == pytest_approx(0.10)
    assert rows[1][2] == pytest_approx(-0.05)
    assert rows[0][3] == pytest_approx(9.0)


def pytest_approx(value: float):
    import pytest
    return pytest.approx(value)


def test_genuine_zero_energy_is_valid_but_missing_history_is_not_zero() -> None:
    target = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)

    db = _db()
    record_published_forecast(
        db,
        issued_at=target - timedelta(hours=1),
        forecast=_forecast(target, ch_kwh=0.0, heating=False),
        interval_minutes=30,
        coefficient_kwh_per_dd=1.163,
        winter_mode_below_c=13.0,
        summer_mode_above_c=14.0,
        forecast_status="healthy",
    )
    def zero_history(entity: str, start: datetime, end: datetime):
        if entity == "sensor.ch":
            return [
                {"state": "25.0", "last_changed": "2026-10-07T08:50:00+00:00"},
                {"state": "25.0", "last_changed": "2026-10-07T09:30:00+00:00"},
            ]
        return [{"state": "14.0", "last_changed": "2026-10-07T08:50:00+00:00"}]
    assert complete_pending_actuals(
        db,
        get_history=zero_history,
        ch_energy_entity="sensor.ch",
        outdoor_temperature_entity="sensor.temp",
        now=target + timedelta(minutes=40),
    ) == (1, 0, 0)
    assert db.execute("SELECT actual_ch_kwh,data_quality FROM ch_forecast_observations").fetchone() == (0.0, "complete")

    missing_db = _db()
    record_published_forecast(
        missing_db,
        issued_at=target - timedelta(hours=1),
        forecast=_forecast(target),
        interval_minutes=30,
        coefficient_kwh_per_dd=1.163,
        winter_mode_below_c=13.0,
        summer_mode_above_c=14.0,
        forecast_status="healthy",
    )
    assert complete_pending_actuals(
        missing_db,
        get_history=lambda *args: [],
        ch_energy_entity="sensor.ch",
        outdoor_temperature_entity="sensor.temp",
        now=target + timedelta(minutes=40),
    ) == (0, 0, 1)
    assert missing_db.execute("SELECT actual_ch_kwh,data_quality FROM ch_forecast_observations").fetchone() == (None, None)


def test_energy_counter_reset_is_rejected_not_converted_to_consumption() -> None:
    db = _db()
    target = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    record_published_forecast(
        db,
        issued_at=target - timedelta(hours=1),
        forecast=_forecast(target),
        interval_minutes=30,
        coefficient_kwh_per_dd=1.163,
        winter_mode_below_c=13.0,
        summer_mode_above_c=14.0,
        forecast_status="healthy",
    )
    def history(entity: str, start: datetime, end: datetime):
        if entity == "sensor.ch":
            return [
                {"state": "100.0", "last_changed": "2026-10-07T08:50:00+00:00"},
                {"state": "2.0", "last_changed": "2026-10-07T09:15:00+00:00"},
                {"state": "2.2", "last_changed": "2026-10-07T09:30:00+00:00"},
            ]
        return [{"state": "8.0", "last_changed": "2026-10-07T08:50:00+00:00"}]
    assert complete_pending_actuals(
        db,
        get_history=history,
        ch_energy_entity="sensor.ch",
        outdoor_temperature_entity="sensor.temp",
        now=target + timedelta(minutes=40),
    ) == (0, 1, 0)
    assert db.execute("SELECT actual_ch_kwh,data_quality FROM ch_forecast_observations").fetchone() == (None, "energy_counter_reset")


def _insert_completed(
    db: sqlite3.Connection,
    *,
    issued: datetime,
    target: datetime,
    forecast: float,
    actual: float,
    temperature: float,
    heating: bool = True,
) -> None:
    horizon = (target - issued).total_seconds() / 3600.0
    db.execute(
        """
        INSERT INTO ch_forecast_observations(
            issued_at,target_start,target_end,horizon_hours,forecast_ch_kwh,
            forecast_temperature_c,forecast_degree_days,coefficient_kwh_per_dd,
            forecast_heating_mode,forecast_heating_enabled,dhw_active,
            winter_mode_below_c,summer_mode_above_c,forecast_status,
            actual_ch_kwh,actual_mean_temperature_c,error_kwh,completed_at,data_quality
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            issued.isoformat(),
            target.isoformat(),
            (target + timedelta(minutes=30)).isoformat(),
            horizon,
            forecast,
            temperature,
            0.1,
            1.163,
            "winter" if heating else "summer",
            int(heating),
            0,
            11.0,
            12.0,
            "healthy",
            actual,
            temperature,
            forecast - actual,
            (target + timedelta(minutes=35)).isoformat(),
            "complete",
        ),
    )
    db.commit()


def test_accuracy_summary_reports_metrics_horizons_temperature_bands_and_heating_state() -> None:
    db = _db()
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    samples = [
        (1.0, 0.6, 0.5, 7.0, True),
        (8.0, 0.2, 0.4, 9.0, True),
        (18.0, 0.5, 0.4, 10.5, True),
        (30.0, 0.0, 0.0, 12.0, False),
        (42.0, 0.1, 0.2, 14.0, False),
    ]
    for index, (horizon, forecast, actual, temp, heating) in enumerate(samples):
        target = now - timedelta(hours=2 + index)
        _insert_completed(
            db,
            issued=target - timedelta(hours=horizon),
            target=target,
            forecast=forecast,
            actual=actual,
            temperature=temp,
            heating=heating,
        )

    result = accuracy_summary(db, now=now, winter_threshold_c=11.0)
    overall = result["overall"]
    assert overall["samples"] == 5
    assert overall["mean_bias_kwh"] == pytest_approx(-0.02)
    assert overall["mae_kwh"] == pytest_approx(0.10)
    assert overall["rmse_kwh"] == pytest_approx(math.sqrt(0.07 / 5))
    assert overall["wape_pct"] == pytest_approx(1.0 / 1.5 * 100)
    assert result["horizons"]["0_6h"]["samples"] == 1
    assert result["horizons"]["6_12h"]["samples"] == 1
    assert result["horizons"]["12_24h"]["samples"] == 1
    assert result["horizons"]["24_36h"]["samples"] == 1
    assert result["horizons"]["36_48h"]["samples"] == 1
    assert result["temperature_analysis"]["bands"]["6_8c"]["samples"] == 1
    assert result["temperature_analysis"]["bands"]["8_10c"]["samples"] == 1
    assert result["temperature_analysis"]["bands"]["10_11c"]["samples"] == 1
    assert result["heating_active"]["samples"] == 3
    assert result["heating_active"]["definition"] == "forecast_heating_enabled"
    assert result["error_definition"] == "forecast_minus_actual"


def test_accuracy_summary_uses_dynamic_threshold_bands_and_rolling_window() -> None:
    db = _db()
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    recent_target = now - timedelta(hours=2)
    old_target = now - timedelta(days=40)
    _insert_completed(db, issued=recent_target - timedelta(hours=3), target=recent_target, forecast=0.4, actual=0.5, temperature=8.5)
    _insert_completed(db, issued=old_target - timedelta(hours=3), target=old_target, forecast=5.0, actual=0.5, temperature=8.5)
    result = accuracy_summary(db, now=now, winter_threshold_c=9.0, window_days=30)
    assert result["overall"]["samples"] == 1
    assert "8_9c" in result["temperature_analysis"]["bands"]
    assert "9_13c" in result["temperature_analysis"]["bands"]
    assert "10_11c" not in result["temperature_analysis"]["bands"]
