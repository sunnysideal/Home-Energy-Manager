from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent / "components" / "ashp_forecaster" / "app"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as legacy
from ch_schedule import ensure_schema, ensure_prefix, read_schedule, state_for, capture, learned_models


def test_half_hour_schedule_and_dst_fold():
    tz = ZoneInfo("Europe/London")
    mask = 1 << 2
    bits = {f"{day}_{half}": 0 for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday") for half in ("am", "pm")}
    bits["sunday_am"] = mask
    a = datetime(2026, 10, 25, 1, 0, tzinfo=tz, fold=0)
    b = a.replace(fold=1)
    assert a.utcoffset() != b.utcoffset()
    assert state_for(bits, a) == state_for(bits, b) == 1
    assert state_for(bits, a.replace(minute=30)) == 0
    assert state_for(bits, datetime(2026, 3, 29, 2, 0, tzinfo=tz)) == 0


class Fake:
    def __init__(self, mask=1):
        self.mask = mask
    def get_state(self, entity):
        return {"state": str(self.mask)}
    def set_sensor(self, *args):
        pass


def test_schedule_requires_all_valid_entities():
    f = Fake()
    bits = read_schedule(f, "number.x_")
    assert len(bits) == 14
    assert bits["monday_am"] == 1
    f.mask = float("nan")
    assert read_schedule(f, "number.x_") is None


def test_capture_is_persistent_and_fail_closed(tmp_path):
    db = legacy.Store(tmp_path / "db").db
    tz = ZoneInfo("Europe/London")
    ensure_schema(db)
    when = datetime(2026, 10, 25, 1, 11, tzinfo=tz, fold=0)
    next_fold = when.replace(fold=1)
    f = Fake(0)
    capture(f, db, "number.x_", tz, now=when)
    capture(f, db, "number.x_", tz, now=next_fold)
    assert db.execute("SELECT COUNT(*) FROM ch_schedule_observations").fetchone()[0] == 2
    f.mask = 1
    capture(f, db, "number.x_", tz, now=when)
    # Same actual interval observed with conflicting settings is unknown.
    assert db.execute("SELECT state FROM ch_schedule_observations ORDER BY start_utc").fetchone()[0] is None
    ensure_prefix(db, "number.y_")
    assert db.execute("SELECT COUNT(*) FROM ch_schedule_observations").fetchone()[0] == 0


def test_grouped_weighted_learning_and_fallback(tmp_path):
    store = legacy.Store(tmp_path / "db")
    db = store.db
    ensure_schema(db)
    day = "2026-10-24"
    base = datetime(2026, 10, 24, tzinfo=timezone.utc)
    rows = []
    for i in range(8):
        rows.append({
            "start_ts": (base + timedelta(minutes=30*i)).isoformat(),
            "degree_days": 0.1,
            "ch_kwh": 0.2 if i < 4 else 0.05,
            "mean_temperature_c": 10.0,
            "schedule_state": int(i < 4),
        })
    store.replace_day(day, rows, 0.8, 1.0, 10.0, True)
    models = learned_models(db, "2026-10-01", 1.55)
    assert abs(models["normal"]["coefficient"] - 2.0) < 1e-8
    assert abs(models["setback"]["coefficient"] - 0.5) < 1e-8
    assert models["normal"]["learned"] and models["setback"]["learned"]
    db.execute("UPDATE slot_training SET schedule_state=NULL WHERE schedule_state=0")
    models = learned_models(db, "2026-10-01", 1.55)
    assert models["setback"]["coefficient"] == 1.55
    assert models["setback"]["learned"] is False


def test_ch_slot_selected_model_in_forecast(monkeypatch):
    tz = ZoneInfo("UTC")
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    class Client:
        ch_schedule_bits = {f"{d}_{h}": 0 for d in ("monday","tuesday","wednesday","thursday","friday","saturday","sunday") for h in ("am","pm")}
        ch_schedule_models = {
            "normal": {"coefficient": 2.0},
            "setback": {"coefficient": 0.5},
        }
        def get_hourly_weather(self, entity):
            return [{"datetime": (now+timedelta(hours=i)).isoformat(), "temperature": 5.0} for i in range(72)]
    c = Client()
    cfg = legacy.Config("sensor.ch", "sensor.outside", "weather.x", forecast_hours=2)
    monkeypatch.setattr(legacy, "build_dhw_forecast", lambda *args: [0]*100)
    monkeypatch.setattr(legacy, "infer_current_heating_mode", lambda *args: "winter")
    one = legacy.build_forecast(c, None, cfg, 1.55, tz)
    assert one and all(x["ch_schedule_state"] == "setback" for x in one)
    assert all(x["ch_kwh_per_degree_day"] == 0.5 for x in one)
    c.ch_schedule_bits = None
    fallback = legacy.build_forecast(c, None, cfg, 1.55, tz)
    assert all(x["ch_schedule_state"] == "unknown" for x in fallback)
    assert all(x["ch_kwh_per_degree_day"] == 1.55 for x in fallback)
