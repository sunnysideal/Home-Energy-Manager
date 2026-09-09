from copy import deepcopy

from config_migration import migrate_runtime_options


def legacy_options():
    return {
        "mqtt": {"enabled": True},
        "ashp_forecaster": {
            "ch_energy_entity": "sensor.hp_ch",
            "outdoor_temperature_entity": "sensor.outdoor",
            "weather_entity": "weather.home",
            "summer_mode_off_entity": "number.summer_off",
            "summer_mode_on_entity": "number.summer_on",
            "dhw_energy_entity": "sensor.hp_dhw",
            "dhw_mode_entity": "select.dhw_mode",
            "dhw_tank_temperature_entity": "sensor.dhw_mid",
            "dhw_tank_upper_temperature_entity": "sensor.dhw_top",
            "dhw_tank_lower_temperature_entity": "sensor.dhw_bottom",
            "dhw_ambient_temperature_entity": "sensor.airing_cupboard",
            "dhw_target_temperature_entity": "number.dhw_target",
            "dhw_hysteresis_entity": "number.dhw_hysteresis",
            "dhw_schedule_prefix": "number.dhw_schedule_",
            "base_temperature_c": 15.5,
        },
        "home_forecaster": {
            "settings": {"timezone": "Europe/London", "history_days": 28},
            "battery": {
                "soc": "sensor.battery_soc",
                "capacity_kwh": "sensor.battery_capacity",
                "reserve_soc": "number.reserve",
                "inverter_max_rate_w": "sensor.max_rate",
                "charge_rate_w": "number.charge_rate",
                "discharge_rate_w": "number.discharge_rate",
                "eco_mode": "switch.eco",
                "charge_schedule_enabled": "switch.charge_schedule",
                "discharge_schedule_enabled": "switch.discharge_schedule",
                "pause_mode": "select.pause_mode",
                "pause_start": "time.pause_start",
                "pause_end": "time.pause_end",
                "charge_start_1": "time.charge_start_1",
                "charge_end_1": "time.charge_end_1",
                "charge_target_1": "number.charge_target_1",
                "charge_start_2": "time.charge_start_2",
                "charge_end_2": "time.charge_end_2",
                "charge_target_2": "number.charge_target_2",
                "discharge_start_1": "time.discharge_start_1",
                "discharge_end_1": "time.discharge_end_1",
                "discharge_target_1": "number.discharge_target_1",
                "discharge_start_2": "time.discharge_start_2",
                "discharge_end_2": "time.discharge_end_2",
                "discharge_target_2": "number.discharge_target_2",
                "battery_power_w": "sensor.battery_power",
                "battery_charge_energy_total_kwh": "sensor.battery_charge_total",
                "battery_discharge_energy_total_kwh": "sensor.battery_discharge_total",
            },
            "load": {"energy_total_kwh": "sensor.house_energy"},
            "solar": {
                "energy_total_kwh": "sensor.pv_energy",
                "solcast_today": "sensor.solcast_today",
                "solcast_tomorrow": "sensor.solcast_tomorrow",
                "solcast_day_3": "sensor.solcast_day3",
            },
            "tariff": {
                "import_energy_total_kwh": "sensor.inverter_import",
                "export_energy_total_kwh": "sensor.inverter_export",
                "import_current_rate": "sensor.import_rate",
                "export_current_rate": "sensor.export_rate",
                "import_current_day_rates": "event.import_today",
                "import_next_day_rates": "event.import_tomorrow",
                "export_current_day_rates": "event.export_today",
                "export_next_day_rates": "event.export_tomorrow",
            },
            "ashp": {
                "forecast_48h": "sensor.ashp_forecast_next_48h",
                "ch_energy_total_kwh": "sensor.hp_ch",
                "dhw_energy_total_kwh": "sensor.hp_dhw",
            },
            "meter": {
                "import_energy_total_kwh": "sensor.smart_meter_import",
                "export_energy_total_kwh": "sensor.smart_meter_export",
            },
            "ev": {
                "energy_total_kwh": "sensor.ev_energy",
                "smart_charging_dispatch_entity": "binary_sensor.ev_dispatch",
                "smart_charging_active_entity": "binary_sensor.ev_active",
            },
        },
        "controller": {
            "operation_mode": "forecast_only",
            "safety_buffer_soc": 20,
            "export_start": "20:00",
            "ev_smart_charging_enabled": True,
            "ev_smart_charging_dispatch_entity": "binary_sensor.ev_dispatch",
            "ev_smart_charging_active_entity": "binary_sensor.ev_active",
        },
    }


def test_legacy_layout_is_preserved_for_component_runtime():
    saved = legacy_options()
    before = deepcopy(saved)
    runtime, canonical, diagnostics = migrate_runtime_options(saved)

    assert saved == before, "migration must never mutate the saved options object"
    assert diagnostics.source_layout == "legacy"

    assert canonical["battery"]["soc"] == "sensor.battery_soc"
    assert canonical["grid"]["import_energy_total_kwh"] == "sensor.smart_meter_import"
    assert canonical["inverter"]["import_energy_total_kwh"] == "sensor.inverter_import"
    assert canonical["solar"]["energy_total_kwh"] == "sensor.pv_energy"
    assert canonical["heat_pump"]["ch_energy_total_kwh"] == "sensor.hp_ch"
    assert canonical["dhw"]["tank_upper_temperature"] == "sensor.dhw_top"
    assert canonical["ev"]["energy_total_kwh"] == "sensor.ev_energy"
    assert canonical["energy_strategy"]["operation_mode"] == "forecast_only"

    # Component contracts remain unchanged after the migration layer renders them.
    assert runtime["home_forecaster"]["battery"]["soc"] == "sensor.battery_soc"
    assert runtime["home_forecaster"]["meter"]["import_energy_total_kwh"] == "sensor.smart_meter_import"
    assert runtime["ashp_forecaster"]["dhw_tank_upper_temperature_entity"] == "sensor.dhw_top"
    assert runtime["controller"]["operation_mode"] == "forecast_only"


def test_canonical_domain_value_overrides_legacy_duplicate_without_rewriting_saved_options():
    saved = legacy_options()
    saved["battery"] = {"soc": "sensor.new_soc"}
    saved["grid"] = {"import_energy_total_kwh": "sensor.new_grid_import"}
    saved["ev"] = {"smart_charging_active": "binary_sensor.new_ev_active"}
    before = deepcopy(saved)

    runtime, canonical, diagnostics = migrate_runtime_options(saved)

    assert saved == before
    assert diagnostics.source_layout == "canonical"
    assert canonical["battery"]["soc"] == "sensor.new_soc"
    assert canonical["grid"]["import_energy_total_kwh"] == "sensor.new_grid_import"
    assert canonical["ev"]["smart_charging_active"] == "binary_sensor.new_ev_active"
    assert runtime["home_forecaster"]["battery"]["soc"] == "sensor.new_soc"
    assert runtime["home_forecaster"]["meter"]["import_energy_total_kwh"] == "sensor.new_grid_import"
    assert runtime["controller"]["ev_smart_charging_active_entity"] == "binary_sensor.new_ev_active"
    assert diagnostics.conflicts


def test_accidental_0131_home_forecaster_layout_is_still_accepted():
    saved = legacy_options()
    hf = saved["home_forecaster"]
    hf["timezone"] = "Europe/London"
    hf.pop("settings")
    hf["pv"] = {
        "energy_total_kwh": "sensor.old_pv",
        "solcast_today": "sensor.old_today",
        "solcast_tomorrow": "sensor.old_tomorrow",
        "solcast_day3": "sensor.old_day3",
    }
    hf.pop("solar")
    hf["grid"] = {
        "import_energy_total_kwh": "sensor.old_true_import",
        "export_energy_total_kwh": "sensor.old_true_export",
        "inverter_import_energy_total_kwh": "sensor.old_inv_import",
        "inverter_export_energy_total_kwh": "sensor.old_inv_export",
    }
    hf.pop("meter")
    hf["tariff"] = {"import_rate": "sensor.old_import_rate", "export_rate": "sensor.old_export_rate"}
    hf["ashp"] = {"forecast_entity": "sensor.old_ashp_forecast"}
    hf["battery"]["charge_target_soc_1"] = hf["battery"].pop("charge_target_1")

    runtime, canonical, _ = migrate_runtime_options(saved)

    assert runtime["home_forecaster"]["settings"]["timezone"] == "Europe/London"
    assert runtime["home_forecaster"]["solar"]["energy_total_kwh"] == "sensor.old_pv"
    assert runtime["home_forecaster"]["meter"]["import_energy_total_kwh"] == "sensor.old_true_import"
    assert runtime["home_forecaster"]["tariff"]["import_energy_total_kwh"] == "sensor.old_inv_import"
    assert runtime["home_forecaster"]["tariff"]["import_current_rate"] == "sensor.old_import_rate"
    assert runtime["home_forecaster"]["ashp"]["forecast_48h"] == "sensor.old_ashp_forecast"
    assert runtime["home_forecaster"]["battery"]["charge_target_1"] == "number.charge_target_1"
    assert canonical["solar"]["energy_total_kwh"] == "sensor.old_pv"
