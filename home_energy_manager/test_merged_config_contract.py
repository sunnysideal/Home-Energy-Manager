import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("hem_launcher_contract", ROOT / "launcher.py")
launcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher
assert spec.loader is not None
spec.loader.exec_module(launcher)


def test_merged_config_keeps_home_assistant_api_permissions_and_canonical_shape():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert cfg["homeassistant_api"] is True
    assert cfg["hassio_api"] is True
    assert cfg["hassio_role"] == "default"

    expected_domains = {
        "battery", "grid", "inverter", "tariff", "solar", "heat_pump",
        "dhw", "ev", "energy_strategy", "axle", "advanced",
    }
    assert set(cfg["options"]) == expected_domains
    assert set(cfg["schema"]) == expected_domains

    # Package-owned cross-component plumbing is deliberately absent from the UI.
    text = (ROOT / "config.yaml").read_text()
    assert "home_energy_forecast_entity:" not in text
    assert "controller_refresh_request_entity:" not in text
    assert "status_entity_id:" not in text
    assert "forecast_48h:" not in text

    # Shared inputs have one public owner.
    assert "smart_charging_dispatch" in cfg["options"]["ev"]
    assert "smart_charging_active" in cfg["options"]["ev"]
    assert "smart_charging_dispatch_entity" not in text
    assert "ev_smart_charging_dispatch_entity" not in text

    assert cfg["options"]["heat_pump"]["ch_energy_total_kwh"] == "sensor.ashp_electrical_energy_ch"
    assert cfg["options"]["dhw"]["energy_total_kwh"] == "sensor.ashp_electrical_energy_dhw"
    assert cfg["options"]["advanced"]["mqtt"]["topic_prefix"] == "home_energy_manager"


def test_standalone_home_forecaster_shape_is_normalised_for_merged_runtime():
    hf = {
        "timezone": "Europe/London",
        "history_days": 21,
        "minimum_baseline_w": 225,
        "battery": {
            "charge_target_soc_1": "number.charge_target_1",
            "charge_target_soc_2": "number.charge_target_2",
            "discharge_target_soc_1": "number.discharge_target_1",
            "discharge_target_soc_2": "number.discharge_target_2",
        },
        "load": {"energy_total_kwh": "sensor.house_energy"},
        "pv": {
            "energy_total_kwh": "sensor.pv_energy",
            "solcast_today": "sensor.solcast_today",
            "solcast_tomorrow": "sensor.solcast_tomorrow",
            "solcast_day3": "sensor.solcast_day3",
        },
        "grid": {
            "import_energy_total_kwh": "sensor.grid_import",
            "export_energy_total_kwh": "sensor.grid_export",
            "inverter_import_energy_total_kwh": "sensor.inverter_import",
            "inverter_export_energy_total_kwh": "sensor.inverter_export",
        },
        "tariff": {"import_rate": "sensor.import_rate", "export_rate": "sensor.export_rate"},
        "ashp": {"forecast_entity": "sensor.ashp_forecast_next_48h"},
        "ev": {},
    }

    launcher._normalise_home_forecaster(hf)

    assert hf["settings"]["timezone"] == "Europe/London"
    assert hf["settings"]["history_days"] == 21
    assert hf["settings"]["minimum_baseline_w"] == 225
    assert hf["solar"]["energy_total_kwh"] == "sensor.pv_energy"
    assert hf["solar"]["solcast_day_3"] == "sensor.solcast_day3"
    assert hf["meter"]["import_energy_total_kwh"] == "sensor.grid_import"
    assert hf["meter"]["export_energy_total_kwh"] == "sensor.grid_export"
    assert hf["tariff"]["import_energy_total_kwh"] == "sensor.inverter_import"
    assert hf["tariff"]["export_energy_total_kwh"] == "sensor.inverter_export"
    assert hf["tariff"]["import_current_rate"] == "sensor.import_rate"
    assert hf["tariff"]["export_current_rate"] == "sensor.export_rate"
    assert hf["ashp"]["forecast_48h"] == "sensor.ashp_forecast_next_48h"
    assert hf["ashp"]["ch_energy_total_kwh"] == "sensor.ashp_electrical_energy_ch"
    assert hf["ashp"]["dhw_energy_total_kwh"] == "sensor.ashp_electrical_energy_dhw"
    assert hf["battery"]["charge_target_1"] == "number.charge_target_1"
    assert hf["battery"]["charge_target_2"] == "number.charge_target_2"
    assert hf["battery"]["discharge_target_1"] == "number.discharge_target_1"
    assert hf["battery"]["discharge_target_2"] == "number.discharge_target_2"
