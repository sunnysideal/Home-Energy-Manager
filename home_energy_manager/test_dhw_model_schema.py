import importlib.util
import sqlite3
import threading
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
        "dhw_demand_profile",
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


def test_fresh_database_does_not_require_metadata_to_exist_first():
    db = sqlite3.connect(":memory:")
    dhw_model.ensure_dhw_model_schema(db)
    assert "metadata" in _table_names(db)
    version = db.execute(
        "SELECT value FROM metadata WHERE key='dhw_model_schema_version'"
    ).fetchone()
    assert version == (str(dhw_model.DHW_MODEL_SCHEMA_VERSION),)


def test_concurrent_schema_initialisation_is_safe(tmp_path):
    path = tmp_path / "ashp_forecast.db"
    # Reproduce an upgrade database where new columns are genuinely absent before
    # several ASHP helper processes all initialize the schema together.
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    seed.execute(
        "CREATE TABLE dhw_thermal_samples ("
        "timestamp TEXT PRIMARY KEY,upper_temp_c REAL,lower_temp_c REAL,"
        "dhw_heating INTEGER NOT NULL DEFAULT 0,immersion_heating INTEGER NOT NULL DEFAULT 0,"
        "dhw_energy_delta_kwh REAL,ambient_temp_c REAL,outdoor_temp_c REAL,valid INTEGER NOT NULL DEFAULT 1)"
    )
    seed.execute(
        "CREATE TABLE dhw_forecast_validation ("
        "forecast_ts TEXT NOT NULL,target_ts TEXT NOT NULL,predicted_upper_c REAL,"
        "predicted_lower_c REAL,actual_upper_c REAL,actual_lower_c REAL,model_source TEXT NOT NULL,"
        "PRIMARY KEY(forecast_ts,target_ts))"
    )
    seed.commit()
    seed.close()

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def migrate() -> None:
        db = sqlite3.connect(path, timeout=10)
        try:
            barrier.wait()
            dhw_model.ensure_dhw_model_schema(db)
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    db = sqlite3.connect(path)
    thermal_columns = {row[1] for row in db.execute("PRAGMA table_info(dhw_thermal_samples)")}
    validation_columns = {row[1] for row in db.execute("PRAGMA table_info(dhw_forecast_validation)")}
    assert {"dhw_energy_total_kwh", "target_temp_c"}.issubset(thermal_columns)
    assert "predicted_dhw_kwh" in validation_columns


def test_dhw_model_schema_supports_two_temperature_samples():
    db = sqlite3.connect(":memory:")
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
