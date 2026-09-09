import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_model.py"

spec = importlib.util.spec_from_file_location("dhw_model", MODULE_PATH)
dhw_model = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(dhw_model)


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_dhw_model_schema_is_idempotent_and_keeps_legacy_tables():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE dhw_run_slots ("
        "day TEXT NOT NULL, run_index INTEGER NOT NULL, relative_slot INTEGER NOT NULL, "
        "energy_kwh REAL NOT NULL, PRIMARY KEY(day, run_index, relative_slot))"
    )
    db.execute(
        "INSERT INTO dhw_run_slots(day, run_index, relative_slot, energy_kwh) "
        "VALUES('2026-09-09', 0, 0, 1.25)"
    )

    dhw_model.ensure_dhw_model_schema(db)
    dhw_model.ensure_dhw_model_schema(db)

    tables = _table_names(db)
    assert {
        "dhw_thermal_samples",
        "dhw_draw_events",
        "dhw_heating_cycles",
        "dhw_model_parameters",
        "dhw_forecast_validation",
    }.issubset(tables)

    legacy = db.execute(
        "SELECT day, run_index, relative_slot, energy_kwh FROM dhw_run_slots"
    ).fetchone()
    assert legacy == ("2026-09-09", 0, 0, 1.25)

    version = db.execute(
        "SELECT value FROM metadata WHERE key='dhw_model_schema_version'"
    ).fetchone()
    assert version == (str(dhw_model.DHW_MODEL_SCHEMA_VERSION),)


def test_dhw_model_schema_supports_two_temperature_samples():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    dhw_model.ensure_dhw_model_schema(db)

    db.execute(
        "INSERT INTO dhw_thermal_samples("
        "timestamp, upper_temp_c, lower_temp_c, dhw_heating, immersion_heating, valid"
        ") VALUES(?, ?, ?, ?, ?, ?)",
        ("2026-09-09T10:00:00+01:00", 51.2, 37.8, 0, 0, 1),
    )
    row = db.execute(
        "SELECT upper_temp_c, lower_temp_c FROM dhw_thermal_samples"
    ).fetchone()
    assert row == (51.2, 37.8)
