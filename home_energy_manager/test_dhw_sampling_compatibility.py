import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNNER_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "runner.py"

spec = importlib.util.spec_from_file_location("ashp_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner)


def test_thermal_only_keys_are_not_forwarded_to_legacy_forecaster(tmp_path, monkeypatch):
    full = tmp_path / "full.json"
    legacy = tmp_path / "legacy.json"
    monkeypatch.setattr(runner, "LEGACY_OPTIONS", legacy)
    full.write_text(json.dumps({
        "ch_energy_entity": "sensor.ch",
        "dhw_tank_temperature_entity": "sensor.old",
        "dhw_tank_upper_temperature_entity": "sensor.top",
        "dhw_tank_lower_temperature_entity": "sensor.bottom",
        "dhw_tank_volume_l": 250,
        "dhw_ambient_temperature_entity": "",
        "dhw_min_usable_temperature_c": 40.0,
        "dhw_thermal_sample_minutes": 5,
    }))

    result = runner._legacy_options_path(full)
    raw = json.loads(result.read_text())

    assert raw["ch_energy_entity"] == "sensor.ch"
    assert raw["dhw_tank_temperature_entity"] == "sensor.old"
    for key in runner.THERMAL_ONLY_KEYS:
        assert key not in raw


def test_merged_package_exposes_passive_dhw_settings():
    package = (ROOT / "config.yaml").read_text()
    assert "version: 0.1.26" in package
    assert "dhw_tank_upper_temperature_entity:" in package
    assert "dhw_tank_lower_temperature_entity:" in package
    assert "dhw_tank_volume_l: 250" in package
    assert "dhw_thermal_sample_minutes: 5" in package


def test_merged_image_packages_dhw_runtime_files():
    dockerfile = (ROOT / "Dockerfile").read_text()
    launcher = (ROOT / "launcher.py").read_text()
    for name in ("runner.py", "dhw_model.py", "dhw_collector.py"):
        assert f"components/ashp_forecaster/app/{name}" in dockerfile
    assert "/app/runtime/ashp_forecaster/runner.py" in launcher
