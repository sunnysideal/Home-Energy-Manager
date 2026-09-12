from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / "components" / "controller"


def test_controller_discovery_is_composed_outside_legacy_core():
    app = (CONTROLLER / "app.py").read_text(encoding="utf-8")
    discovery = (CONTROLLER / "controller_discovery.py").read_text(encoding="utf-8")
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "from controller_discovery import discover_from_forecast as _discover_from_forecast" in app
    assert "return _discover_from_forecast(self, state)" in app
    assert "def discover_from_forecast(controller, state):" in discovery
    assert "controller_inputs" in discovery
    assert "metering" in discovery
    assert "ZoneInfo" in discovery
    assert len(app.splitlines()) < 80

    assert "COPY components/controller/controller_legacy_core.py /app/runtime/controller/controller_legacy_core.py" in docker
    assert "COPY components/controller/controller_discovery.py /app/runtime/controller/controller_discovery.py" in docker
