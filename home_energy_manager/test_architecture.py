from pathlib import Path
import ast
import yaml

ROOT = Path(__file__).resolve().parent
COMPONENTS = {
    "ashp_forecaster": ROOT / "components" / "ashp_forecaster",
    "home_forecaster": ROOT / "components" / "home_forecaster",
    "controller": ROOT / "components" / "controller",
}

for name, path in COMPONENTS.items():
    assert path.is_dir(), f"Missing component directory: {name}"

# No component may directly import another component.
component_names = set(COMPONENTS)
for owner, directory in COMPONENTS.items():
    for py in directory.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module.split(".")[0]}
            else:
                continue
            forbidden = (imported & component_names) - {owner}
            assert not forbidden, f"{owner} directly imports sibling component(s) {sorted(forbidden)} in {py}"

launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
for required in ("ashp_forecaster", "home_forecaster", "controller", "OPTIONS_PATH"):
    assert required in launcher
for forbidden in ("battery_soc", "export_generated", "charge_rate_w", "degree_day", "pv_kwh"):
    assert forbidden not in launcher, f"Energy/control logic leaked into launcher: {forbidden}"

cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
# Until the user-domain schema is switched on, the public 0.1.x options remain the
# three private component sections plus MQTT. The migration layer may accept more.
assert set(cfg["options"]) == component_names | {"mqtt"}
assert set(cfg["schema"]) == component_names | {"mqtt"}

print("Architecture separation checks passed.")


# MQTT is shared transport only and must remain outside the component directories.
mqtt_path = ROOT / "common" / "mqtt.py"
assert mqtt_path.is_file(), "Shared MQTT transport missing"
mqtt_text = mqtt_path.read_text(encoding="utf-8")
for forbidden in ("export_generated", "degree_day", "battery_soc", "charge_target", "offpeak"):
    assert forbidden not in mqtt_text, f"Energy/control logic leaked into MQTT transport: {forbidden}"

# All components may use common MQTT transport, but must not import one another.
for path in (
    ROOT / "components" / "ashp_forecaster" / "app" / "main.py",
    ROOT / "components" / "home_forecaster" / "app" / "main.py",
    ROOT / "components" / "controller" / "app.py",
):
    text = path.read_text(encoding="utf-8")
    assert "from common.mqtt import MQTTPublisher" in text, f"MQTT publisher not wired into {path}"

print("MQTT presentation architecture checks passed.")


def test_manager_runtime_entrypoints_are_explicitly_copied_and_launched():
    dockerfile = (ROOT / "Dockerfile").read_text()
    launcher = (ROOT / "launcher.py").read_text()
    expected = {
        "components/ashp_forecaster/app/runner.py": "/app/runtime/ashp_forecaster/runner.py",
        "components/home_forecaster/app/main.py": "/app/runtime/home_forecaster/main.py",
        "components/controller/app.py": "/app/runtime/controller/app.py",
    }
    for source, runtime in expected.items():
        assert (ROOT / source).is_file(), source
        assert f"COPY {source} {runtime}" in dockerfile
        assert runtime in launcher
