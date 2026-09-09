from config_migration import migrate_runtime_options


def test_canonical_only_config_renders_private_component_contracts():
    saved = {
        "battery": {
            "soc": "sensor.soc",
            "capacity_kwh": "sensor.capacity",
            "reserve_soc": "number.reserve",
            "power_w": "sensor.battery_power",
            "charge_energy_total_kwh": "sensor.charge_total",
            "discharge_energy_total_kwh": "sensor.discharge_total",
        },
        "grid": {
            "import_energy_total_kwh": "sensor.true_import",
            "export_energy_total_kwh": "sensor.true_export",
        },
        "inverter": {
            "import_energy_total_kwh": "sensor.inverter_import",
            "export_energy_total_kwh": "sensor.inverter_export",
            "max_rate_w": "sensor.max_rate",
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
        },
        "tariff": {
            "import_current_rate": "sensor.import_rate",
            "export_current_rate": "sensor.export_rate",
            "import_current_day_rates": "event.import_today",
            "import_next_day_rates": "event.import_tomorrow",
            "export_current_day_rates": "event.export_today",
            "export_next_day_rates": "event.export_tomorrow",
        },
        "solar": {
            "energy_total_kwh": "sensor.pv_total",
            "solcast_today": "sensor.solcast_today",
            "solcast_tomorrow": "sensor.solcast_tomorrow",
            "solcast_day_3": "sensor.solcast_day3",
        },
        "heat_pump": {
            "ch_energy_total_kwh": "sensor.ch_total",
            "outdoor_temperature": "sensor.outdoor",
            "weather": "weather.home",
            "summer_mode_off": "number.summer_off",
            "summer_mode_on": "number.summer_on",
            "forecast_48h": "sensor.ashp_forecast_next_48h",
        },
        "dhw": {
            "energy_total_kwh": "sensor.dhw_total",
            "mode": "select.dhw_mode",
            "tank_temperature": "sensor.dhw_mid",
            "tank_upper_temperature": "sensor.dhw_top",
            "tank_lower_temperature": "sensor.dhw_bottom",
            "ambient_temperature": "sensor.cupboard",
            "target_temperature": "number.dhw_target",
            "hysteresis": "number.dhw_hysteresis",
            "schedule_prefix": "number.dhw_schedule_",
        },
        "ev": {
            "energy_total_kwh": "sensor.ev_total",
            "smart_charging_dispatch": "binary_sensor.ev_dispatch",
            "smart_charging_active": "binary_sensor.ev_active",
            "smart_charging_enabled": True,
        },
        "energy_strategy": {
            "operation_mode": "forecast_only",
            "safety_buffer_soc": 20,
            "export_start": "20:00",
        },
        "advanced": {
            "home_forecaster": {
                "history_days": 28,
                "forecast_interval_minutes": 5,
                "controller_refresh_request_entity": "sensor.home_energy_forecast_refresh_request",
            },
            "ashp_forecaster": {
                "base_temperature_c": 15.5,
                "forecast_hours": 48,
                "forecast_interval_minutes": 30,
                "update_minutes": 15,
            },
            "battery_learning": {
                "battery_idle_threshold_w": 75,
            },
            "mqtt": {
                "enabled": True,
                "topic_prefix": "home_energy_manager",
            },
        },
    }

    runtime, canonical, diagnostics = migrate_runtime_options(saved)

    assert diagnostics.source_layout == "canonical"
    assert canonical["grid"]["import_energy_total_kwh"] == "sensor.true_import"
    assert runtime["home_forecaster"]["meter"]["import_energy_total_kwh"] == "sensor.true_import"
    assert runtime["home_forecaster"]["tariff"]["import_energy_total_kwh"] == "sensor.inverter_import"
    assert runtime["home_forecaster"]["battery"]["soc"] == "sensor.soc"
    assert runtime["home_forecaster"]["solar"]["energy_total_kwh"] == "sensor.pv_total"
    assert runtime["home_forecaster"]["ev"]["energy_total_kwh"] == "sensor.ev_total"
    assert runtime["ashp_forecaster"]["ch_energy_entity"] == "sensor.ch_total"
    assert runtime["ashp_forecaster"]["dhw_tank_upper_temperature_entity"] == "sensor.dhw_top"
    assert runtime["controller"]["operation_mode"] == "forecast_only"
    assert runtime["controller"]["ev_smart_charging_active_entity"] == "binary_sensor.ev_active"
