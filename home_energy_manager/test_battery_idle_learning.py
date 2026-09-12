import importlib.util
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "home_forecaster" / "app"

# Load the real Home Forecaster module, then provide the small runtime-layer
# dependency expected by battery_idle_runtime without importing the Axle wrapper.
main_spec = importlib.util.spec_from_file_location("idle_test_main", APP / "main.py")
main = importlib.util.module_from_spec(main_spec)
sys.modules[main_spec.name] = main
main_spec.loader.exec_module(main)
sys.modules["axle_pricing_core"] = types.SimpleNamespace(base=main)

spec = importlib.util.spec_from_file_location("battery_idle_runtime_test", APP / "battery_idle_runtime.py")
idle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = idle
spec.loader.exec_module(idle)


class Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    def meta_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    def meta_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, str(value)))
        self.db.commit()


class Cfg:
    def __init__(self):
        self.sections = {
            "battery": {
                "battery_power_w": "sensor.battery_power",
                "soc": "sensor.soc",
                "pause_mode": "select.pause",
                "pause_start": "time.pause_start",
                "pause_end": "time.pause_end",
                "charge_schedule_enabled": "switch.charge",
                "discharge_schedule_enabled": "switch.discharge",
                "charge_start_1": "time.cs1", "charge_end_1": "time.ce1",
                "charge_start_2": "time.cs2", "charge_end_2": "time.ce2",
                "discharge_start_1": "time.ds1", "discharge_end_1": "time.de1",
                "discharge_start_2": "time.ds2", "discharge_end_2": "time.de2",
                "battery_charge_energy_total_kwh": "sensor.charge_energy",
                "battery_discharge_energy_total_kwh": "sensor.discharge_energy",
                "battery_idle_threshold_w": 75,
                "battery_idle_history_days": 14,
                "battery_idle_min_samples": 3,
                "battery_idle_soc_delta_pct": 0.15,
            },
            "solar": {"energy_total_kwh": "sensor.pv_energy"},
        }
    def section(self, name):
        return self.sections[name]


def st(value, when):
    return {"state": str(value), "last_changed": when.isoformat(), "last_updated": when.isoformat()}


class Client:
    def __init__(self, now, power=39.0, soc_values=(50.0, 50.05), pv_delta=0.0, pause="PauseBoth"):
        old = now - timedelta(minutes=30)
        self.now = now
        self.states = {
            "select.pause": st(pause, old),
            "time.pause_start": st("12:00:00", old), "time.pause_end": st("14:00:00", old),
            "switch.charge": st("off", old), "switch.discharge": st("off", old),
            "time.cs1": st("00:00:00", old), "time.ce1": st("00:00:00", old),
            "time.cs2": st("00:00:00", old), "time.ce2": st("00:00:00", old),
            "time.ds1": st("00:00:00", old), "time.de1": st("00:00:00", old),
            "time.ds2": st("00:00:00", old), "time.de2": st("00:00:00", old),
            "sensor.battery_power": st(power, now), "sensor.soc": st(soc_values[-1], now),
        }
        start = now - timedelta(minutes=10)
        self.history_map = {
            "sensor.battery_power": [st(power, start), st(power, now)],
            "sensor.soc": [st(soc_values[0], start), st(soc_values[-1], now)],
            "sensor.pv_energy": [st(100.0, start), st(100.0 + pv_delta, now)],
            "sensor.charge_energy": [st(20.0, start), st(20.0, now)],
            "sensor.discharge_energy": [st(30.0, start), st(30.0, now)],
        }
    def state_optional(self, entity):
        if entity == idle.AXLE_ENTITY:
            return {"state": "idle", "attributes": {"active": False}}
        return self.states.get(entity)
    def history(self, entities, start, end, timeout=45):
        return {entity: self.history_map.get(entity, []) for entity in entities}


def test_clean_pause_both_samples_learn_robust_median_and_readiness():
    store = Store(); cfg = Cfg()
    base_time = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)
    for offset, watts in enumerate((39.0, 41.0, 38.0)):
        model = idle.learn_idle_loss(Client(base_time + timedelta(minutes=5 * offset), power=watts), store, cfg, base_time + timedelta(minutes=5 * offset))
    assert model["sample_count"] == 3
    assert model["ready"] is True
    assert model["learned_w"] == 39.0
    assert model["forecast_applied"] is True


def test_rejects_wrong_pause_high_power_unstable_soc_and_pv():
    now = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)
    cases = [
        Client(now, pause="PauseDischarge"),
        Client(now, power=157.0),
        Client(now, soc_values=(50.0, 50.5)),
        Client(now, pv_delta=0.02),
    ]
    for client in cases:
        store = Store(); model = idle.learn_idle_loss(client, store, Cfg(), now)
        assert model["sample_count"] == 0
        assert model["ready"] is False


def test_persistence_pruning_and_fallback_before_ready():
    store = Store(); cfg = Cfg(); idle._ensure_schema(store)
    now = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)
    store.db.execute("INSERT INTO battery_idle_samples VALUES(?,?,?,?)", ((now - timedelta(days=20)).isoformat(), 20.0, 50.0, 20.0))
    store.db.execute("INSERT INTO battery_idle_samples VALUES(?,?,?,?)", ((now - timedelta(days=1)).isoformat(), 40.0, 50.0, 20.0))
    store.db.commit()
    model = idle._model(store, cfg, now)
    assert model["sample_count"] == 1
    assert model["learned_w"] == 40.0
    assert model["ready"] is False
    assert model["forecast_applied"] is False


def test_ready_standby_loss_reduces_soc_only_when_battery_is_idle(monkeypatch):
    batt = {"capacity": 10.0, "reserve": 4.0}
    slot = {"duration_h": 0.5}
    idle._active_standby_w = 40.0
    monkeypatch.setattr(idle, "_original_simulate_battery", lambda *args: (5.0, 0.0, 0.2, 0.0))
    soc, battery_kwh, imp, exp = idle.simulate_battery_with_idle_loss(slot, batt, 5.0, 0.95, 0.95)
    assert round(soc, 3) == 4.98
    assert (battery_kwh, imp, exp) == (0.0, 0.2, 0.0)

    monkeypatch.setattr(idle, "_original_simulate_battery", lambda *args: (5.0, 0.3, 0.0, 0.0))
    soc, *_ = idle.simulate_battery_with_idle_loss(slot, batt, 5.0, 0.95, 0.95)
    assert soc == 5.0
