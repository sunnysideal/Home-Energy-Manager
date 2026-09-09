"""Persistence primitives for the ASHP forecaster DHW thermal model.

This module deliberately contains storage/schema concerns only. The thermal model,
learning and forecast selection remain owned by the ASHP forecaster and are wired in
separate commits. Keeping schema creation idempotent allows existing installations
to upgrade without rebuilding or deleting the legacy DHW training data.
"""
from __future__ import annotations

import sqlite3


DHW_MODEL_SCHEMA_VERSION = 6


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column idempotently, including when several helper processes migrate at once.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. A PRAGMA pre-check alone
    is racy: two processes can both observe a missing column and then both attempt the
    ALTER. The second ALTER is harmless, so explicitly tolerate only that duplicate-column
    outcome and re-raise every other database error.
    """
    columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
        # Another process completed the migration between our PRAGMA and ALTER.
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            raise


def ensure_dhw_model_schema(db: sqlite3.Connection) -> None:
    """Create/migrate DHW thermal-model tables without modifying legacy forecast data."""
    # All passive helpers call this independently at startup. Create the shared metadata
    # table here rather than relying on the legacy forecaster or collector to win the race.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_thermal_samples (
            timestamp TEXT PRIMARY KEY,
            upper_temp_c REAL,
            lower_temp_c REAL,
            dhw_heating INTEGER NOT NULL DEFAULT 0,
            immersion_heating INTEGER NOT NULL DEFAULT 0,
            dhw_energy_delta_kwh REAL,
            dhw_energy_total_kwh REAL,
            target_temp_c REAL,
            ambient_temp_c REAL,
            outdoor_temp_c REAL,
            valid INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    _ensure_column(db, "dhw_thermal_samples", "dhw_energy_total_kwh", "REAL")
    _ensure_column(db, "dhw_thermal_samples", "target_temp_c", "REAL")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_draw_events (
            timestamp TEXT PRIMARY KEY,
            estimated_thermal_kwh REAL NOT NULL,
            confidence REAL NOT NULL,
            upper_before_c REAL,
            lower_before_c REAL,
            upper_after_c REAL,
            lower_after_c REAL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_heating_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL,
            end_ts TEXT NOT NULL,
            start_upper_c REAL,
            start_lower_c REAL,
            end_upper_c REAL,
            end_lower_c REAL,
            target_temp_c REAL,
            electrical_kwh REAL,
            outdoor_temp_c REAL,
            cycle_type TEXT NOT NULL,
            valid INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    _ensure_column(db, "dhw_heating_cycles", "target_temp_c", "REAL")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_demand_profile (
            day_type TEXT NOT NULL,
            slot_index INTEGER NOT NULL,
            expected_kwh REAL NOT NULL,
            probability REAL NOT NULL,
            typical_kwh REAL NOT NULL,
            sample_days INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(day_type, slot_index)
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_model_parameters (
            name TEXT PRIMARY KEY,
            value REAL NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            error REAL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS dhw_forecast_validation (
            forecast_ts TEXT NOT NULL,
            target_ts TEXT NOT NULL,
            predicted_upper_c REAL,
            predicted_lower_c REAL,
            predicted_dhw_kwh REAL,
            actual_upper_c REAL,
            actual_lower_c REAL,
            model_source TEXT NOT NULL,
            PRIMARY KEY (forecast_ts, target_ts)
        )
        """
    )
    _ensure_column(db, "dhw_forecast_validation", "predicted_dhw_kwh", "REAL")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dhw_thermal_samples_valid_time "
        "ON dhw_thermal_samples(valid, timestamp)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dhw_heating_cycles_start "
        "ON dhw_heating_cycles(start_ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dhw_forecast_validation_target "
        "ON dhw_forecast_validation(target_ts)"
    )
    db.execute(
        "INSERT INTO metadata(key, value) VALUES('dhw_model_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(DHW_MODEL_SCHEMA_VERSION),),
    )
    db.commit()
