from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from components.ashp_forecaster.app.weather_observations import (
    complete_pending_actuals,
    ensure_schema,
    record_forecast_snapshot,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    ensure_schema(db)
    return db


def test_records_each_provider_snapshot_with_horizon() -> None:
    db = _db()
    issued = datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc)
    points = [
        (datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc), 10.2),
        (datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc), 9.7),
    ]

    assert record_forecast_snapshot(
        db, issued_at=issued, source_entity="weather.forecast_home", points=points
    ) == 2

    rows = db.execute(
        "SELECT target_ts,horizon_hours,forecast_temperature_c,source_entity "
        "FROM weather_forecast_observations ORDER BY target_ts"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == 55 / 60
    assert rows[0][2:] == (10.2, "weather.forecast_home")


def test_repeated_snapshot_is_idempotent() -> None:
    db = _db()
    issued = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    points = [(datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc), 10.0)]

    assert record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=points) == 1
    assert record_forecast_snapshot(db, issued_at=issued, source_entity="weather.home", points=points) == 0
    assert db.execute("SELECT COUNT(*) FROM weather_forecast_observations").fetchone()[0] == 1


def test_backfill_uses_nearest_actual_within_tolerance_for_all_snapshots() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    for issued, forecast in [
        (datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc), 9.5),
        (datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 10.0),
    ]:
        record_forecast_snapshot(
            db,
            issued_at=issued,
            source_entity="weather.forecast_home",
            points=[(target, forecast)],
        )

    def history(entity, start, end):
        assert entity == "sensor.ecomax360i_outdoor_temperature"
        return [
            {"state": "8.8", "last_changed": "2026-09-12T12:51:00+00:00"},
            {"state": "8.6", "last_changed": "2026-09-12T13:03:00+00:00"},
            {"state": "8.5", "last_changed": "2026-09-12T13:09:00+00:00"},
        ]

    completed, unmatched = complete_pending_actuals(
        db,
        get_history=history,
        actual_entity="sensor.ecomax360i_outdoor_temperature",
        now=datetime(2026, 9, 12, 13, 20, tzinfo=timezone.utc),
    )
    assert (completed, unmatched) == (2, 0)

    rows = db.execute(
        "SELECT forecast_temperature_c,actual_temperature_c,actual_observed_at,error_c "
        "FROM weather_forecast_observations ORDER BY issued_at"
    ).fetchall()
    assert rows[0][0:2] == (9.5, 8.6)
    assert rows[0][2] == "2026-09-12T13:03:00+00:00"
    assert round(rows[0][3], 3) == -0.9
    assert round(rows[1][3], 3) == -1.4


def test_backfill_waits_until_tolerance_window_has_elapsed() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    record_forecast_snapshot(
        db,
        issued_at=target - timedelta(hours=1),
        source_entity="weather.forecast_home",
        points=[(target, 10.0)],
    )
    called = False

    def history(entity, start, end):
        nonlocal called
        called = True
        return []

    completed, unmatched = complete_pending_actuals(
        db,
        get_history=history,
        actual_entity="sensor.outdoor",
        now=target + timedelta(minutes=10),
    )
    assert (completed, unmatched) == (0, 0)
    assert called is False


def test_unmatched_target_remains_pending() -> None:
    db = _db()
    target = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    record_forecast_snapshot(
        db,
        issued_at=target - timedelta(hours=2),
        source_entity="weather.forecast_home",
        points=[(target, 10.0)],
    )

    def history(entity, start, end):
        return [{"state": "8.0", "last_changed": "2026-09-12T13:20:00+00:00"}]

    assert complete_pending_actuals(
        db,
        get_history=history,
        actual_entity="sensor.outdoor",
        now=target + timedelta(minutes=30),
    ) == (0, 1)
    assert db.execute(
        "SELECT actual_temperature_c,error_c FROM weather_forecast_observations"
    ).fetchone() == (None, None)
