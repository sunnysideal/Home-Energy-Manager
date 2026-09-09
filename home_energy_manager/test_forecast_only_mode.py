import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_launcher():
    path = ROOT / "launcher.py"
    spec = importlib.util.spec_from_file_location("hem_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_forecast_only_selects_passive_controller():
    launcher = load_launcher()
    processes = launcher.process_list(
        {"controller": {"operation_mode": "forecast_only"}}
    )
    controller = dict(processes)["controller"]
    assert controller[-1] == "/app/runtime/controller/forecast_only.py"


def test_active_modes_select_active_controller():
    launcher = load_launcher()
    for mode in ("maximise_export", "minimise_export", "export_generated"):
        processes = launcher.process_list({"controller": {"operation_mode": mode}})
        controller = dict(processes)["controller"]
        assert controller[-1] == "/app/runtime/controller/app.py"


def test_passive_controller_has_no_service_write_path():
    source = (ROOT / "components/controller/forecast_only.py").read_text()
    assert "/services/" not in source
    assert ".write(" not in source
    assert "async def write" not in source
