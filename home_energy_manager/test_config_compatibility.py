from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _text(path):
    return (ROOT / path).read_text()


def test_export_generated_threshold_is_upgrade_optional_with_code_default():
    controller_cfg = _text("components/controller/standalone-config.yaml")
    controller = _text("components/controller/app.py")

    controller_options = controller_cfg.split("schema:", 1)[0]
    assert "export_generated_solar_threshold_w:" not in controller_options
    assert "export_generated_solar_threshold_w: int(0,2000)?" in controller_cfg
    assert "get('export_generated_solar_threshold_w',100)" in controller


def test_forecast_entity_has_registry_and_runtime_fallbacks():
    mqtt = _text("common/mqtt.py")
    controller = _text("components/controller/app.py")
    assert '"default_entity_id": entity_id' in mqtt
    assert "async def resolve_forecast_entity" in controller
    assert "auto-discovered %s" in controller
