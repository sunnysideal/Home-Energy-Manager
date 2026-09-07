import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD_PATH = ROOT / "components" / "home_forecaster" / "app" / "main.py"
spec = importlib.util.spec_from_file_location("hem_home_forecaster", MOD_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

class DummyCfg:
    def __init__(self, meter, tariff):
        self._sections = {"meter": meter, "tariff": tariff}
    def section(self, name):
        return self._sections[name]

class ExplodingClient:
    def state_optional(self, entity):
        raise AssertionError("selection must not probe HA state for explicitly configured true meters")

def test_explicit_true_meters_are_authoritative_without_state_probe():
    cfg = DummyCfg(
        {"import_energy_total_kwh": "sensor.smart_import", "export_energy_total_kwh": "sensor.smart_export"},
        {"import_energy_total_kwh": "sensor.battery_import", "export_energy_total_kwh": "sensor.battery_export"},
    )
    assert mod.effective_grid_energy_entities(ExplodingClient(), cfg) == (
        "sensor.smart_import", "sensor.smart_export", "true_meter", "true_meter"
    )

def test_fallback_only_when_true_meter_not_configured():
    cfg = DummyCfg(
        {"import_energy_total_kwh": "", "export_energy_total_kwh": ""},
        {"import_energy_total_kwh": "sensor.battery_import", "export_energy_total_kwh": "sensor.battery_export"},
    )
    assert mod.effective_grid_energy_entities(ExplodingClient(), cfg) == (
        "sensor.battery_import", "sensor.battery_export", "battery", "battery"
    )
