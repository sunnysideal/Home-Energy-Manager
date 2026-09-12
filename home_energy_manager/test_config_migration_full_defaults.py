from copy import deepcopy
from pathlib import Path

import yaml

from config_migration import migrate_runtime_options

ROOT = Path(__file__).resolve().parent


def test_shipped_canonical_options_render_private_component_contracts():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    saved = deepcopy(config["options"])
    before = deepcopy(saved)

    runtime, canonical, diagnostics = migrate_runtime_options(saved)

    assert saved == before, "in-memory migration must not rewrite saved options"
    assert diagnostics.source_layout == "canonical"
    assert set(canonical) == {
        "battery", "grid", "inverter", "tariff", "solar", "heat_pump",
        "dhw", "ev", "energy_strategy", "advanced",
    }

    # The public canonical domains are rendered back into the unchanged private
    # component contracts consumed by the four separate processes.
    assert runtime["home_forecaster"]["battery"]["soc"] == before["battery"]["soc"]
    assert runtime["home_forecaster"]["load"]["energy_total_kwh"] == before["inverter"]["house_load_energy_total_kwh"]
    assert runtime["home_forecaster"]["meter"]["import_energy_total_kwh"] == before["grid"]["import_energy_total_kwh"]
    assert runtime["home_forecaster"]["solar"]["energy_total_kwh"] == before["solar"]["energy_total_kwh"]
    assert runtime["ashp_forecaster"]["ch_energy_entity"] == before["heat_pump"]["ch_energy_total_kwh"]
    assert runtime["ashp_forecaster"]["dhw_energy_entity"] == before["dhw"]["energy_total_kwh"]
    assert runtime["controller"]["operation_mode"] == before["energy_strategy"]["operation_mode"]
    assert runtime["controller"]["home_energy_forecast_entity"] == "sensor.home_energy_forecast"
    assert runtime["controller"]["status_entity_id"] == "sensor.home_energy_controller"
    assert runtime["home_forecaster"]["settings"]["controller_refresh_request_entity"] == "sensor.home_energy_forecast_refresh_request"
    assert runtime["home_forecaster"]["ashp"]["forecast_48h"] == "sensor.ashp_forecast_next_48h"

    # EV Smart Charging is configured once and handed to both consumers.
    assert runtime["home_forecaster"]["ev"]["smart_charging_dispatch_entity"] == before["ev"]["smart_charging_dispatch"]
    assert runtime["controller"]["ev_smart_charging_dispatch_entity"] == before["ev"]["smart_charging_dispatch"]

    # Component-specific adapter configuration remains owned by Axle.
    assert runtime["axle"] == before["axle"]
