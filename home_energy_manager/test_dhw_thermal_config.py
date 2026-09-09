import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_config.py"

spec = importlib.util.spec_from_file_location("dhw_config", MODULE_PATH)
dhw_config = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(dhw_config)


def test_legacy_single_temperature_config_keeps_thermal_model_disabled():
    cfg = dhw_config.DHWThermalConfig.from_options({
        "dhw_tank_temperature_entity": "sensor.legacy_dhw_temp"
    })
    assert cfg.upper_temperature_entity == "sensor.legacy_dhw_temp"
    assert cfg.lower_temperature_entity is None
    assert cfg.enabled is False
    assert cfg.tank_volume_l == 250.0


def test_dual_sensor_config_enables_thermal_model():
    cfg = dhw_config.DHWThermalConfig.from_options({
        "dhw_tank_temperature_entity": "sensor.legacy_dhw_temp",
        "dhw_upper_temperature_entity": "sensor.tank_top",
        "dhw_lower_temperature_entity": "sensor.tank_bottom",
        "dhw_tank_volume_l": 250,
    })
    assert cfg.upper_temperature_entity == "sensor.tank_top"
    assert cfg.lower_temperature_entity == "sensor.tank_bottom"
    assert cfg.enabled is True


def test_optional_ambient_and_minimum_useful_temperature_are_supported():
    cfg = dhw_config.DHWThermalConfig.from_options({
        "dhw_lower_temperature_entity": "sensor.tank_bottom",
        "dhw_ambient_temperature_entity": "sensor.airing_cupboard_temperature",
        "dhw_minimum_useful_temperature_c": 42,
    })
    assert cfg.ambient_temperature_entity == "sensor.airing_cupboard_temperature"
    assert cfg.minimum_useful_temperature_c == 42.0


def test_invalid_tank_volume_is_rejected():
    try:
        dhw_config.DHWThermalConfig.from_options({"dhw_tank_volume_l": 20})
    except ValueError as exc:
        assert "dhw_tank_volume_l" in str(exc)
    else:
        raise AssertionError("invalid DHW tank volume was accepted")
