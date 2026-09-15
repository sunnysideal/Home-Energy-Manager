import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "home_forecaster" / "app"
sys.path.insert(0, str(APP))


def _load_wrapper():
    base = types.SimpleNamespace()

    def legacy_sim(slot, batt, soc_kwh, charge_eff, discharge_eff):
        return (soc_kwh + charge_eff, -charge_eff, 1.0, 0.0)

    base.simulate_battery = legacy_sim
    base.main = lambda: None

    core = types.ModuleType("battery_model_core")
    core.base = base
    core._ensure_schema = lambda store: None
    core._observe = lambda client, store, cfg, now: None
    core._model_attributes = lambda store, cfg, now: {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "source": "home_forecaster",
        "bands": [
            {
                "soc_lo": 0,
                "soc_hi": 100,
                "generic_factor": 0.5,
                "learned_factor": 0.7,
                "confidence": 1.0,
                "effective_factor": 0.7,
            }
        ],
        "top_completion_allowance_minutes": 15.0,
        "generic_top_completion_minutes": 15.0,
        "diagnostics": {"mode": "shadow", "forecast_consumes_model": False},
    }
    sys.modules["battery_model_core"] = core

    spec = importlib.util.spec_from_file_location(
        "battery_model_forecast_runtime_test", APP / "battery_model_forecast_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, core, base


def test_forecast_charge_compatibility_preserves_legacy_efficiency():
    runtime, _core, _base = _load_wrapper()
    attrs = {
        "bands": [
            {"soc_lo": 0, "soc_hi": 50, "effective_factor": 0.95},
            {"soc_lo": 50, "soc_hi": 100, "effective_factor": 0.35},
        ]
    }
    for soc in (10, 60, 99):
        assert runtime.forecast_charge_efficiency(
            attrs, soc_pct=soc, legacy_efficiency=0.91
        ) == 0.91


def test_simulation_routes_through_forecaster_model_without_output_change():
    runtime, _core, base = _load_wrapper()
    runtime._active_model_attributes = {
        "bands": [{"soc_lo": 0, "soc_hi": 100, "effective_factor": 0.35}]
    }
    slot = {"load_kwh": 0.2, "pv_kwh": 0.0, "duration_h": 0.5}
    batt = {"capacity": 13.5}
    expected = runtime._wrapped_simulate_battery(slot, batt, 6.75, 0.92, 0.94)
    actual = base.simulate_battery(slot, batt, 6.75, 0.92, 0.94)
    assert actual == expected


def test_published_model_is_same_authoritative_forecast_snapshot():
    runtime, core, _base = _load_wrapper()

    class Client:
        def __init__(self):
            self.published = None

        def publish(self, entity, state, attrs):
            self.published = (entity, state, attrs)

    client = Client()
    now = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)
    attrs = runtime.publish_forecaster_model(client, object(), object(), now)
    assert runtime._active_model_attributes is attrs
    assert client.published[1] == "active"
    assert attrs["diagnostics"]["mode"] == "forecaster_owned"
    assert attrs["diagnostics"]["forecast_consumes_model"] is True
    assert attrs["diagnostics"]["controller_consumes_model"] is False
    assert attrs["diagnostics"]["forecast_charge_mode"] == "legacy_flat_compatibility"


def test_issue_51_wrapper_is_packaged_after_learning_core():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    assert "COPY components/home_forecaster/app/battery_model_runtime.py /app/runtime/home_forecaster/battery_model_core.py" in dockerfile
    assert "COPY components/home_forecaster/app/battery_model_forecast_runtime.py /app/runtime/home_forecaster/battery_model_forecast_runtime.py" in dockerfile
    assert "COPY components/home_forecaster/app/offpeak_rollover_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py" in dockerfile
    assert "/app/runtime/home_forecaster/axle_pricing_runner.py" in launcher
