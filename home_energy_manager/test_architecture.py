from pathlib import Path
import ast
import re
import yaml

ROOT = Path(__file__).resolve().parent
COMPONENTS = {
    "ashp_forecaster": ROOT / "components" / "ashp_forecaster",
    "home_forecaster": ROOT / "components" / "home_forecaster",
    "axle": ROOT / "components" / "axle",
    "controller": ROOT / "components" / "controller",
}

for name, path in COMPONENTS.items():
    assert path.is_dir(), f"Missing component directory: {name}"

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
for required in ("ashp_forecaster", "home_forecaster", "axle", "controller", "OPTIONS_PATH"):
    assert required in launcher
for forbidden in ("battery_soc", "export_generated", "charge_rate_w", "degree_day", "pv_kwh"):
    assert forbidden not in launcher, f"Energy/control logic leaked into launcher: {forbidden}"

cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
canonical_public = {
    "battery", "grid", "inverter", "tariff", "solar", "heat_pump",
    "dhw", "ev", "energy_strategy", "advanced",
}
assert set(cfg["options"]) == canonical_public | {"axle"}
assert set(cfg["schema"]) == canonical_public | {"axle"}
assert "axle_only" in cfg["schema"]["energy_strategy"]["operation_mode"]

print("Architecture separation checks passed.")

mqtt_path = ROOT / "common" / "mqtt.py"
assert mqtt_path.is_file(), "Shared MQTT transport missing"
mqtt_text = mqtt_path.read_text(encoding="utf-8")
for forbidden in ("export_generated", "degree_day", "battery_soc", "charge_target", "offpeak"):
    assert forbidden not in mqtt_text, f"Energy/control logic leaked into MQTT transport: {forbidden}"

for path in (
    ROOT / "components" / "ashp_forecaster" / "app" / "main.py",
    ROOT / "components" / "home_forecaster" / "app" / "main.py",
    ROOT / "components" / "axle" / "main.py",
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
        "components/home_forecaster/app/health_runtime.py": "/app/runtime/home_forecaster/axle_pricing_runner.py",
        "components/axle/main.py": "/app/runtime/axle/main.py",
    }
    for source, runtime in expected.items():
        assert (ROOT / source).is_file(), source
        assert f"COPY {source} {runtime}" in dockerfile
        assert runtime in launcher
    assert "COPY components/home_forecaster/app/axle_pricing_runner.py /app/runtime/home_forecaster/axle_pricing_core.py" in dockerfile

    # Controller runtime uses explicit wrappers at the public entrypoints so
    # write-boundary invariants can be enforced without duplicating the planner.
    controller_expected = {
        "components/controller/app.py": "/app/runtime/controller/app_core.py",
        "components/controller/controller_utils.py": "/app/runtime/controller/controller_utils.py",
        "components/controller/controller_db.py": "/app/runtime/controller/controller_db.py",
        "components/controller/controller_ha.py": "/app/runtime/controller/controller_ha.py",
        "components/controller/minimise_export_runtime.py": "/app/runtime/controller/minimise_export_runtime.py",
        "components/controller/active_runtime.py": "/app/runtime/controller/app.py",
        "components/controller/axle_only.py": "/app/runtime/controller/axle_only_core.py",
        "components/controller/axle_only_runtime.py": "/app/runtime/controller/axle_only.py",
    }
    for source, runtime in controller_expected.items():
        assert (ROOT / source).is_file(), source
        assert f"COPY {source} {runtime}" in dockerfile
    assert "/app/runtime/controller/app.py" in launcher
    assert "/app/runtime/controller/axle_only.py" in launcher
    assert "COPY components/home_forecaster/app/main.py /app/runtime/home_forecaster/main.py" in dockerfile


def test_ashp_runtime_local_imports_are_packaged():
    app_dir = ROOT / "components" / "ashp_forecaster" / "app"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_re = re.compile(
        r"^COPY components/ashp_forecaster/app/([A-Za-z0-9_]+\.py) "
        r"/app/runtime/ashp_forecaster/\1$",
        re.MULTILINE,
    )
    packaged = {Path(name).stem for name in copy_re.findall(dockerfile)}
    local_modules = {path.stem for path in app_dir.glob("*.py")}

    pending = list(packaged)
    checked = set()
    while pending:
        module = pending.pop()
        if module in checked:
            continue
        checked.add(module)
        source = app_dir / f"{module}.py"
        assert source.is_file(), f"Dockerfile packages missing ASHP source module: {module}"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported_local = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_local.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_local.add(node.module.split(".")[0])
        imported_local &= local_modules
        missing = imported_local - packaged
        assert not missing, (
            f"ASHP runtime module {module}.py imports local module(s) not copied "
            f"into the image: {sorted(missing)}"
        )
        pending.extend(imported_local - checked)


def test_axle_boundary_is_hacs_only_and_axle_controller_is_isolated():
    adapter = (ROOT / "components" / "axle" / "main.py").read_text()
    axle_controller = (ROOT / "components" / "controller" / "axle_only.py").read_text()
    for entity_id in (
        "sensor.axle_vpp_axle_start_time",
        "sensor.axle_vpp_axle_end_time",
        "sensor.axle_vpp_axle_import_export",
        "sensor.axle_vpp_axle_event_window_state",
        "sensor.axle_vpp_axle_updated_at",
    ):
        assert entity_id in adapter
    assert "sensor.home_energy_manager_axle" in adapter
    assert "api.axle" not in adapter.lower()
    assert "axle_only" in axle_controller
    assert "components.controller.app" not in axle_controller
    assert "maximise_export" not in axle_controller
    assert "export_generated" not in axle_controller


def test_controller_infrastructure_is_extracted_from_core():
    controller_dir = ROOT / "components" / "controller"
    core_text = (controller_dir / "app.py").read_text(encoding="utf-8")
    core_tree = ast.parse(core_text, filename=str(controller_dir / "app.py"))
    top_level_classes = {node.name for node in core_tree.body if isinstance(node, ast.ClassDef)}
    top_level_functions = {node.name for node in core_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "DB" not in top_level_classes
    assert "HA" not in top_level_classes
    for name in ("as_float", "parse_dt", "clamp", "iso", "weighted_quantile", "recency_weight"):
        assert name not in top_level_functions
    assert "from controller_db import DB" in core_text
    assert "from controller_ha import HA" in core_text
    assert "from controller_utils import as_float, parse_dt, clamp, iso, weighted_quantile, recency_weight" in core_text
