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

`sensor.ashp_weather_raw_mae` reports weather-forecast accuracy and shadow calibration diagnostics. Home Energy Manager learns a robust 30-day median temperature bias globally and for the 0–6h, 6–12h, 12–24h, 24–36h and 36–48h forecast horizons. Sparse horizon buckets fall back to the global correction, and learned corrections are limited to ±5°C. The sensor exposes raw MAE, shadow-corrected MAE, improvement percentage, sample counts and oldest/newest calibration samples. This is diagnostic only: `calibration_applied` remains `false`, so the learned weather correction does not yet alter the production ASHP forecast.

The app log also records a summary of the learned global bias and shadow improvement plus per-horizon sample counts, learned corrections, fallback source and raw/shadow MAE. This makes it possible to see immediately how much usable observation history has accumulated.

### Whole-home forecast

Combines normal household load, ASHP demand, PV forecast, battery state/schedules, tariff data and optional EV Smart Charging into a forward simulation of load, SOC, import, export and cost.

`sensor.home_energy_cost_today` exposes the current **net actual energy cost so far today** in GBP. It is calculated from the Home Forecaster's existing actual totals as import cost minus export income, so export income can reduce the value below zero. The sensor also exposes today's import cost and export income as attributes. If actual pricing is incomplete, the sensor reports `unknown` rather than £0.

### Battery model diagnostics

`sensor.home_energy_manager_battery_model` exposes the Home Forecaster's learned SOC-band charging model. The Controller uses this model for charge-duration planning when it is valid and fresh. If it is missing, stale, invalid or incompatible, the Controller automatically falls back to its existing local learned/generic calculation without changing inverter-write semantics.

`sensor.home_energy_manager_battery_model_parity` shows which source supplied the Controller calculation (`forecaster` or `fallback`) and compares the resulting charge duration with the currently published Forecaster model whenever a charge is planned. A state of `match` means the calculations agree within the diagnostic tolerance; `difference` exposes both durations, the delta and the SOC-band factors involved; `unavailable` includes the reason, such as no active charge plan or unusable model data.

`sensor.home_energy_manager_battery_model_seed` exposes the Controller model used to initialise the Home Forecaster's learned battery state on a fresh data store. It is an internal hand-off entity and normally needs no user action.

### Battery control

Available operating modes can minimise export, maximise export, export an amount related to generated solar, remain forecast-only, or operate only for qualifying Axle events. Controller safety rules prioritise avoiding unwanted peak-rate import and protecting battery reserve/safety constraints.

In `minimise_export`, the overnight cheap-rate objective is adaptive rather than a fixed low SOC. Home Energy Manager charges as high as practical while leaving enough forecast battery headroom to absorb avoidable solar surplus before the next regular cheap period. The target is never allowed below the configured minimise-export floor or the SOC required to avoid forecast peak-rate import and preserve the safety buffer. Low-solar/high-load days therefore naturally tend toward a 100% overnight target, while sunnier surplus days may deliberately leave more battery capacity empty. If the required no-slots forecast coverage or battery inputs are incomplete, the target fails safe to 100%.

The **Calibration enabled** setting controls automatic periodic battery calibration only. Turning it off does not disable explicit one-shot calibration requests. `button.home_energy_manager_request_low_calibration` requests the normal low/deep calibration cycle. `button.home_energy_manager_request_high_calibration` requests the normal full/top calibration at the next safe/practical charging opportunity. Both requests remain subject to the Controller's tariff, event and safety priorities, survive restart while pending, and do not re-enable automatic calibration when it is disabled. A high request is only completed when the battery actually reports 100% SOC; a 99% or interrupted attempt remains incomplete.

`button.home_energy_manager_clear_calibration_state` abandons persisted calibration intent such as a pending manual request or an incomplete deep-calibration recharge. It preserves historical calibration timestamps, SOC observations and battery-learning data. The button itself does not write inverter settings; the next normal Controller plan converges any stale calibration schedule. If automatic calibration remains enabled and is genuinely due from its historical timer, the normal scheduler may derive a new calibration again.

### Optional integrations

- **EV Smart Charging**: supplies confirmed/planned charging information and optional EV energy measurement. Disable Smart Charging handling if you do not use it.
- **Axle VPP**: reads event information from the Home Assistant Axle integration and can prepare/control the battery for qualifying export events. Set `axle.enabled` to `false` if you do not use Axle.
- **Power Down**: supplier event handling controlled by `energy_strategy.power_down_enabled`. Disable it when no matching event/baseline entities are configured.
- **MQTT**: used for Home Assistant discovery/presentation when a broker is available; explicit broker details are normally unnecessary when Home Assistant can provide the MQTT service.

## Important safety note

Only one controller should write the inverter's charge, discharge and pause settings. Disable other battery-control automations before enabling a Home Energy Manager control mode.

## Complete guide

See [DOCS.md](DOCS.md) for installation, configuration, operating modes, output entities, verification and troubleshooting.
