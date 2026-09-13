import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "components" / "ashp_forecaster" / "app" / "main.py"
RUNNER = ROOT / "components" / "ashp_forecaster" / "app" / "runner.py"

EXPECTED_SLOT_KEYS = {
    "start",
    "temperature_c",
    "heating_mode",
    "heating_enabled",
    "dhw_active",
    "degree_days",
    "ch_kwh",
    "dhw_kwh",
    "energy_kwh",
}


def _forecast_slot_dict_keys() -> set[str]:
    tree = ast.parse(MAIN.read_text())
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "append":
            continue
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Dict):
            continue
        keys = {
            key.value
            for key in node.args[0].keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if {"ch_kwh", "dhw_kwh", "energy_kwh"}.issubset(keys):
            candidates.append(keys)
    assert len(candidates) == 1, "expected one ASHP forecast-slot dictionary"
    return candidates[0]


def test_public_ashp_forecast_slot_contract_is_unchanged():
    assert _forecast_slot_dict_keys() == EXPECTED_SLOT_KEYS


def test_core_forecaster_does_not_import_thermal_model_modules():
    source = MAIN.read_text()
    forbidden = (
        "dhw_shadow_forecast",
        "dhw_validation",
        "dhw_cycle_learner",
        "dhw_demand_learner",
        "dhw_passive_learner",
        "dhw_simulator",
    )
    for module in forbidden:
        assert module not in source


def test_runner_filters_thermal_only_options_before_starting_core_forecaster():
    source = RUNNER.read_text()
    for option in (
        "dhw_tank_upper_temperature_entity",
        "dhw_tank_lower_temperature_entity",
        "dhw_tank_volume_l",
        "dhw_ambient_temperature_entity",
        "dhw_min_usable_temperature_c",
        "dhw_thermal_sample_minutes",
    ):
        assert option in source
    assert 'forecast_env["OPTIONS_PATH"] = str(core_options_path)' in source


def test_thermal_production_is_owned_by_forecast_runner_not_main_module():
    source = MAIN.read_text()
    assert "dhw_production_selector" not in source
    assert "sensor.ashp_dhw_thermal_forecast_next_48h" not in source
