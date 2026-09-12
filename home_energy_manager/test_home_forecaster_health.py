import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "components" / "home_forecaster" / "app" / "health_runtime.py"


class FakeConfig:
    def __init__(self, tariff=None, ev=None):
        self.data = {"tariff": tariff or {}, "ev": ev or {}}

    def section(self, name):
        return self.data.get(name, {})


def load_runtime():
    base = types.SimpleNamespace(
        make_forecast=lambda *args: ({"attributes": {}}, []),
        publish_health=lambda *args: None,
        main=lambda: None,
        HEALTH_ENTITY="sensor.home_energy_forecast_health",
        LOG=types.SimpleNamespace(warning=lambda *args: None),
    )
    core = types.ModuleType("axle_pricing_core")
    core.base = base
    sys.modules["axle_pricing_core"] = core
    spec = importlib.util.spec_from_file_location("health_runtime_test", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_unconfigured_optional_features_do_not_degrade_health():
    runtime = load_runtime()
    reasons = [runtime.EXPORT_UNCONFIGURED_REASON, runtime.EV_UNAVAILABLE_REASON]
    degraded, capabilities = runtime.classify_optional_health(
        FakeConfig(ev={"smart_charging_enabled": False, "smart_charging_dispatch": ""}), reasons
    )
    assert degraded == []
    assert capabilities["export_tariff"]["status"] == "unconfigured"
    assert capabilities["ev_smart_charging"]["status"] == "unconfigured"


def test_configured_but_unavailable_optional_features_still_degrade_health():
    runtime = load_runtime()
    reasons = [runtime.EXPORT_UNCONFIGURED_REASON, runtime.EV_UNAVAILABLE_REASON]
    degraded, capabilities = runtime.classify_optional_health(
        FakeConfig(
            tariff={"export_current_rate": "sensor.export_rate"},
            ev={"smart_charging_enabled": True, "smart_charging_dispatch": "event.ev_dispatch"},
        ),
        reasons,
    )
    assert degraded == reasons
    assert capabilities["export_tariff"]["status"] == "configured"
    assert capabilities["ev_smart_charging"]["status"] == "configured"


def test_other_forecast_warnings_are_never_suppressed():
    runtime = load_runtime()
    degraded, _ = runtime.classify_optional_health(FakeConfig(), ["Solcast forecast stale"])
    assert degraded == ["Solcast forecast stale"]


def test_health_runtime_is_packaged_at_existing_launcher_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    assert "COPY components/home_forecaster/app/axle_pricing_runner.py /app/runtime/home_forecaster/axle_pricing_core.py" in dockerfile
    assert "COPY components/home_forecaster/app/health_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py" in dockerfile
    assert "/app/runtime/home_forecaster/axle_pricing_runner.py" in launcher
