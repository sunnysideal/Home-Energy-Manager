"""Persistence primitives for the ASHP forecaster DHW thermal model.

This module deliberately contains storage/schema concerns only. The thermal model,
learning and forecast selection remain owned by the ASHP forecaster and are wired in
separate commits. Keeping schema creation idempotent allows existing installations
to upgrade without rebuilding or deleting the legacy DHW training data.
"""
from __future__ import annotations

import sqlite3


DHW_MODEL_SCHEMA_VERSION = 3


def ensure_dhw_model_schema(db: sqlite3.Connection) -> None:
    """Create/migrate DHW thermal-model tables without modifying legacy forecast data."""
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
    sample_columns = {row[1] for row in db.execute("PRAGMA table_info(dhw_thermal_samples)")}
    if "dhw_energy_total_kwh" not in sample_columns:
        db.execute("ALTER TABLE dhw_thermal_samples ADD COLUMN dhw_energy_total_kwh REAL")
    if "target_temp_c" not in sample_columns:
        db.execute("ALTER TABLE dhw_thermal_samples ADD COLUMN target_temp_c REAL")

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
            electrical_kwh REAL,
            outdoor_temp_c REAL,
            cycle_type TEXT NOT NULL,
            valid INTEGER NOT NULL DEFAULT 1
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
            actual_upper_c REAL,
            actual_lower_c REAL,
            model_source TEXT NOT NULL,
            PRIMARY KEY (forecast_ts, target_ts)
        )
        """
    )
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
