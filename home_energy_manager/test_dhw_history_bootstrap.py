import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
for name in ("dhw_draw_detector", "dhw_cycle_tracker", "dhw_model", "dhw_history_bootstrap"):
    spec = importlib.util.spec_from_file_location(name, APP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

bootstrap = sys.modules["dhw_history_bootstrap"]
dhw_model = sys.modules["dhw_model"]


def _state(entity_id, ts, value):
    return {"entity_id": entity_id, "state": str(value), "last_changed": ts.isoformat()}


def test_bootstrap_imports_history_and_is_idempotent(monkeypatch):
    db = sqlite3.connect(":memory:")
    dhw_model.ensure_dhw_model_schema(db)
    tz = ZoneInfo("Europe/London")
    day = datetime.now(tz).date() - timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    upper = "sensor.upper"
    lower = "sensor.lower"
    energy = "sensor.dhw_energy"
    target = "number.target"
    outdoor = "sensor.outdoor"

    data = {
        upper: [_state(upper, start - timedelta(hours=1), 48.0), _state(upper, start + timedelta(hours=1), 46.0), _state(upper, start + timedelta(hours=2), 53.0)],
        lower: [_state(lower, start - timedelta(hours=1), 47.0), _state(lower, start + timedelta(hours=1), 43.0), _state(lower, start + timedelta(hours=2), 50.0)],
        energy: [_state(energy, start - timedelta(hours=1), 100.0), _state(energy, start + timedelta(hours=1, minutes=5), 100.2), _state(energy, start + timedelta(hours=1, minutes=10), 100.5), _state(energy, start + timedelta(hours=1, minutes=15), 100.8), _state(energy, start + timedelta(hours=1, minutes=20), 101.0)],
        target: [_state(target, start - timedelta(hours=1), 52.0)],
        outdoor: [_state(outdoor, start - timedelta(hours=1), 10.0)],
    }

    monkeypatch.setattr(bootstrap, "_history", lambda token, entities, a, b: {e: list(data.get(e, [])) for e in entities})
    cfg = {
        "dhw_tank_upper_temperature_entity": upper,
        "dhw_tank_lower_temperature_entity": lower,
        "dhw_energy_entity": energy,
        "dhw_target_temperature_entity": target,
        "outdoor_temperature_entity": outdoor,
        "dhw_history_days": 1,
        "dhw_thermal_sample_minutes": 5,
        "dhw_tank_volume_l": 250,
        "dhw_activity_threshold_kwh": 0.2,
    }

    first = bootstrap.bootstrap_history(db, "token", cfg, timezone_name="Europe/London")
    assert first is not None
    assert first.days_imported == 1
    assert first.samples_inserted > 0
    assert db.execute("SELECT COUNT(*) FROM dhw_thermal_samples").fetchone()[0] > 0

    second = bootstrap.bootstrap_history(db, "token", cfg, timezone_name="Europe/London")
    assert second is None


def test_failed_or_incomplete_bootstrap_is_not_marked_complete(monkeypatch):
    db = sqlite3.connect(":memory:")
    dhw_model.ensure_dhw_model_schema(db)
    cfg = {
        "dhw_tank_upper_temperature_entity": "sensor.upper",
        "dhw_tank_lower_temperature_entity": "sensor.lower",
        "dhw_energy_entity": "sensor.energy",
        "dhw_history_days": 1,
    }
    monkeypatch.setattr(bootstrap, "_history", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("history unavailable")))
    try:
        bootstrap.bootstrap_history(db, "token", cfg, timezone_name="Europe/London")
    except RuntimeError:
        pass
    row = db.execute("SELECT value FROM metadata WHERE key=?", (bootstrap.BOOTSTRAP_COMPLETE_KEY,)).fetchone()
    assert row is None
