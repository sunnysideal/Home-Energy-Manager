import importlib.util
import sqlite3
import sys
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
    # Runtime imports health_runtime, whose wrapper stack is safe to import in tests.
    spec = importlib.util.spec_from_file_location("battery_model_runtime_test", APP / "battery_model_runtime.py")
    module = importlib.util.module_from_spec(spec)
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
