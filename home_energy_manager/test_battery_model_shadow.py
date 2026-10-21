import importlib.util
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "home_forecaster" / "app"
sys.path.insert(0, str(APP))

import battery_model_contract as contract


class Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")

    def meta_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, str(value)))
        self.db.commit()


def _load_runtime():
    # Mirror the packaged wrapper chain used by the add-on. health_runtime imports
    # battery_idle_core, which is a Docker-time alias for battery_idle_runtime.py.
    # These tests exercise only the shadow model's pure learning/model helpers.
    base = types.SimpleNamespace(
        make_forecast=lambda *args: ({"attributes": {}}, []),
        publish_health=lambda *args: None,
        main=lambda: None,
        HEALTH_ENTITY="sensor.home_energy_forecast_health",
        LOG=types.SimpleNamespace(warning=lambda *args: None),
    )
    idle_core = types.ModuleType("battery_idle_core")
    idle_core.base = base
    sys.modules["battery_idle_core"] = idle_core
    sys.modules.pop("health_runtime", None)

    spec = importlib.util.spec_from_file_location("battery_model_runtime_test", APP / "battery_model_runtime.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_shadow_derivation_matches_controller_band_semantics():
    runtime = _load_runtime()
    start = datetime(2026, 9, 13, 1, 0, tzinfo=timezone.utc)
    points = [
        (start, 90.0),
        (start + timedelta(minutes=10), 92.0),
        (start + timedelta(minutes=20), 95.0),
    ]
    obs = runtime._derive_observations(points, 3455.0, 13.5)
    band = next(item for item in obs if item["band_lo"] == 90 and item["band_hi"] == 95)
    expected_kw = 13.5 * .05 / (20 / 60)
    assert abs(band["effective_factor"] - expected_kw / 3.455) < 1e-9
    assert band["coverage"] == 1.0


def test_shadow_model_uses_same_effective_factor_blend_as_controller():
    runtime = _load_runtime()
    store = Store()
    runtime._ensure_schema(store)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    store.db.execute(
        "INSERT INTO battery_charge_bands(band_lo,band_hi,learned_factor,confidence,full_equiv,obs_count,updated_at) VALUES(?,?,?,?,?,?,?)",
        (90, 95, .70, .5, 2.0, 2, now.isoformat()),
    )
    store.db.commit()

    class Config:
        def section(self, name):
            return {"generic_dwell_minutes": 15} if name == "controller" else {}

    attrs = runtime._model_attributes(store, Config(), now)
    band = next(item for item in attrs["bands"] if item["soc_lo"] == 90 and item["soc_hi"] == 95)
    assert band["generic_factor"] == .88
    assert band["effective_factor"] == .79
    assert attrs["diagnostics"]["mode"] == "shadow"
    assert attrs["diagnostics"]["controller_consumes_model"] is False
    assert attrs["diagnostics"]["forecast_consumes_model"] is False


def test_shadow_contract_remains_valid_with_no_learned_observations():
    runtime = _load_runtime()
    store = Store()
    runtime._ensure_schema(store)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

    class Config:
        def section(self, name):
            return {"generic_dwell_minutes": 15} if name == "controller" else {}

    attrs = runtime._model_attributes(store, Config(), now)
    ok, reason = contract.validate_battery_model_attributes(attrs, now=now)
    assert ok, reason
    assert attrs["diagnostics"]["top_completion"]["migration_state"] == "awaiting_seed"


def test_shadow_runtime_is_the_packaged_home_forecaster_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    assert "COPY components/home_forecaster/app/health_runtime.py /app/runtime/home_forecaster/health_runtime.py" in dockerfile
    assert "COPY components/home_forecaster/app/battery_model_contract.py /app/runtime/home_forecaster/battery_model_contract.py" in dockerfile
    assert "COPY components/home_forecaster/app/battery_model_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py" in dockerfile
    assert "/app/runtime/home_forecaster/axle_pricing_runner.py" in launcher


def test_charge_learning_reads_configured_charge_rate_w_and_records_observation():
    runtime = _load_runtime()
    runtime.base.LOG = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
    runtime.base.bool_state = lambda state, fallback: state is not None and state.get("state") == "on"
    runtime.base.numeric_state = lambda state: float(state["state"]) if state else None
    runtime.base.in_daily_window = lambda now, start, end: True
    runtime.base.parse_hhmm = lambda value: tuple(map(int, value.split(":")[:2]))
    runtime.base.latest_before = lambda states, boundary: next(
        (float(item["state"]) for item in states if item["last_changed"] <= boundary.isoformat()), None
    )
    runtime.base.parse_dt = datetime.fromisoformat

    now = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)
    states = {
        "enabled": "on", "rate": "3450", "start": "01:00", "end": "06:00",
        "soc": "52", "capacity": "13.77",
    }
    class Client:
        def state_optional(self, entity):
            return {"state": states[entity]} if entity in states else None

        def history(self, entities, start, end, timeout=45):
            return {"soc": [
                {"state": "50", "last_changed": (now - timedelta(minutes=5)).isoformat()},
                {"state": "52", "last_changed": now.isoformat()},
            ]}

    class Config:
        def section(self, name):
            return {"charge_schedule_enabled": "enabled", "charge_rate_w": "rate",
                    "charge_start_1": "start", "charge_end_1": "end",
                    "soc": "soc", "capacity_kwh": "capacity"}

    store = Store()
    runtime._ensure_schema(store)
    runtime._observe(Client(), store, Config(), now)
    rows = store.db.execute("SELECT * FROM battery_charge_observations").fetchall()
    assert rows
    assert all(row["requested_rate_w"] == 3450 for row in rows)
    assert store.db.execute("SELECT COUNT(*) FROM battery_charge_bands WHERE learned_factor IS NOT NULL").fetchone()[0] > 0


def test_charge_learning_warns_for_missing_rate_in_active_window():
    runtime = _load_runtime()
    warnings = []
    runtime.base.LOG = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: warnings.append(args))
    runtime.base.bool_state = lambda state, fallback: state is not None and state.get("state") == "on"
    runtime.base.numeric_state = lambda state: float(state["state"]) if state else None
    runtime.base.in_daily_window = lambda now, start, end: True
    class Client:
        def state_optional(self, entity):
            return {"state": {"enabled": "on", "start": "01:00", "end": "06:00"}[entity]} if entity in ("enabled", "start", "end") else None
    class Config:
        def section(self, name):
            return {"charge_schedule_enabled": "enabled", "charge_rate_w": "rate",
                    "charge_start_1": "start", "charge_end_1": "end"}
    assert runtime._active_charge_window(Client(), Config(), datetime(2026, 10, 22, 2, tzinfo=timezone.utc)) is None
    assert warnings
    assert "charge_rate_unavailable" in warnings[0][0]


def test_migrated_top_completion_allowance_is_disabled_without_changing_soc_bands():
    runtime = _load_runtime()
    store = Store()
    runtime._ensure_schema(store)
    store.meta_set("battery_model_top_completion_allowance_minutes", "35")
    store.meta_set("battery_model_top_completion_attempts", "2")
    store.meta_set("battery_model_top_completion_misses", "2")
    now = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)
    store.db.execute(
        "INSERT INTO battery_charge_bands(band_lo,band_hi,learned_factor,confidence,full_equiv,obs_count,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (90, 95, .70, .5, 2.0, 2, now.isoformat()),
    )
    store.db.commit()

    class Config:
        def section(self, name):
            return {"generic_dwell_minutes": 15} if name == "controller" else {}

    attrs = runtime._model_attributes(store, Config(), now)
    assert attrs["top_completion_allowance_minutes"] == 0.0
    assert attrs["generic_top_completion_minutes"] == 0.0
    assert attrs["diagnostics"]["top_completion"]["enabled"] is False
    assert attrs["diagnostics"]["top_completion"]["attempts"] == 2
    assert store.meta_get("battery_model_top_completion_allowance_minutes") == "35"
    assert next(b for b in attrs["bands"] if b["soc_lo"] == 90)["effective_factor"] == .79
    assert contract.validate_battery_model_attributes(attrs, now=now) == (True, "ok")

    from components.controller import controller_battery
    ok, reason, _age = controller_battery.validate_forecaster_model(attrs, now)
    assert ok, reason
    actual = controller_battery.forecaster_charge_minutes(4, 100, 3442, 13.77, attrs)
    expected = sum(
        (13.77 * (max(0, min(100, b["soc_hi"]) - max(4, b["soc_lo"])) / 100.0))
        / (3442 / 1000.0 * b["effective_factor"]) * 60
        for b in attrs["bands"]
    )
    assert abs(actual - expected) < 1e-9
