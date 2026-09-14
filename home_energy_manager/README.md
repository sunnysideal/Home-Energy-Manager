# Home Energy Manager

Home Energy Manager is a Home Assistant app that forecasts household energy use and can control a compatible battery around electricity tariffs, solar generation, heat-pump demand and optional grid-service events.

## First start

For a new installation:

1. Complete the required entity settings in the app **Configuration** tab.
2. Keep **Operation mode** set to `forecast_only`.
3. Disable optional event features you do not use, such as Axle or Power Down handling.
4. Start the app.
5. Check the app log for missing/unavailable entities.
6. Confirm the ASHP and whole-home forecast entities are updating.
7. Confirm the forecast values and battery state match what you expect from Home Assistant.
8. Only then select a battery-control mode if you want Home Energy Manager to write inverter settings.

`forecast_only` does not write battery/inverter settings and is the recommended setup and troubleshooting mode.

## Main features

### ASHP forecast

Learns space-heating demand from historical central-heating energy and outdoor temperature. It also models domestic-hot-water demand from the configured DHW state, tank temperatures, target and schedule.

### Whole-home forecast

Combines normal household load, ASHP demand, PV forecast, battery state/schedules, tariff data and optional EV Smart Charging into a forward simulation of load, SOC, import, export and cost.

### Battery control

Available operating modes can minimise export, maximise export, export an amount related to generated solar, remain forecast-only, or operate only for qualifying Axle events. Controller safety rules prioritise avoiding unwanted peak-rate import and protecting battery reserve/safety constraints.

### Optional integrations

- **EV Smart Charging**: supplies confirmed/planned charging information and optional EV energy measurement. Disable Smart Charging handling if you do not use it.
- **Axle VPP**: reads event information from the Home Assistant Axle integration and can prepare/control the battery for qualifying export events. Set `axle.enabled` to `false` if you do not use Axle.
- **Power Down**: supplier event handling controlled by `energy_strategy.power_down_enabled`. Disable it when no matching event/baseline entities are configured.
- **MQTT**: used for Home Assistant discovery/presentation when a broker is available; explicit broker details are normally unnecessary when Home Assistant can provide the MQTT service.

## Important safety note

Only one controller should write the inverter's charge, discharge and pause settings. Disable other battery-control automations before enabling a Home Energy Manager control mode.

## Complete guide

See [DOCS.md](DOCS.md) for installation, configuration, operating modes, output entities, verification and troubleshooting.
