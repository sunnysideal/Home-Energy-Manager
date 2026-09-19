# Home Energy Manager

Home Energy Manager is a Home Assistant app that forecasts household energy use and can control a compatible home battery around tariffs, solar generation, heat-pump demand and optional EV/grid-service events.

This guide is written for a **fresh installation**. Start with `forecast_only`, confirm the data and forecasts are correct, and only then enable battery control.

## What the app does

Home Energy Manager provides four user-visible functions:

- **ASHP forecasting** — central-heating (CH) and domestic-hot-water (DHW) electricity demand.
- **Whole-home forecasting** — household load, PV, battery SOC, import/export and cost.
- **Battery control** — charge, discharge and pause planning using the selected operating mode.
- **Optional event support** — EV Smart Charging and Axle VPP events.

## Requirements

You need:

- Home Assistant with **Apps** support;
- an `aarch64` or `amd64` system;
- Home Assistant entities exposing the energy system measurements/settings described below;
- Recorder history for useful learning from previous household/ASHP behaviour;
- a weather entity with forecast temperatures;
- a compatible inverter integration if you want Home Energy Manager to control the battery.

An MQTT broker is recommended for normal entity discovery. When Home Assistant exposes its MQTT service to the app, no manual broker address is normally required.

## Install the app

1. Open **Settings → Apps → App store**.
2. Open **Repositories**.
3. Add:

   `https://github.com/sunnysideal/Home-Energy-Manager`

4. Refresh the App store.
5. Install **Home Energy Manager**.
6. Open the app's **Configuration** tab.
7. Configure the entities for your installation.
8. Leave **Operation mode** set to `forecast_only` for the first start.
9. Start the app.

Updates are delivered through the Home Assistant App store in the same way as other repository apps.

## Before configuring entities

An entity name is not enough to determine whether it is suitable. Check the entity under **Developer Tools → States** and confirm its state, unit and meaning.

Important distinctions:

- **Cumulative energy** means a total that increases over time, normally measured in `kWh`.
- **Power** is an instantaneous value, normally measured in `W` or `kW`.
- **SOC** is a battery percentage from `0` to `100`.
- **Configured charge/discharge rate** is an inverter setting/limit, not live battery power.

The default heat-pump and solar entity IDs in the app are examples from the development installation. Replace them unless your entities really use those IDs.

# Configuration

## 1. Battery

These inputs describe the physical battery. They are needed for the whole-home battery simulation and for controller operation.

| Setting | What to select |
|---|---|
| **State of charge** (`battery.soc`) | Current battery SOC percentage. |
| **Capacity** (`battery.capacity_kwh`) | Battery usable/configured capacity in kWh. |
| **Reserve SOC** (`battery.reserve_soc`) | Inverter minimum reserve percentage. |
| **Battery power** (`battery.power_w`) | Live battery power in watts. Use the integration's signed battery-power convention. |
| **Charge energy total** (`battery.charge_energy_total_kwh`) | Cumulative battery charging energy in kWh. Used for battery-efficiency learning. |
| **Discharge energy total** (`battery.discharge_energy_total_kwh`) | Cumulative battery discharge energy in kWh. Used for battery-efficiency learning. |

For basic forecasting, SOC and capacity are the most important battery inputs. For accurate learning, provide the cumulative charge/discharge totals as well.

## 2. Grid meters

| Setting | What to select |
|---|---|
| **Grid import energy total** (`grid.import_energy_total_kwh`) | True utility/smart-meter cumulative import in kWh. |
| **Grid export energy total** (`grid.export_energy_total_kwh`) | True utility/smart-meter cumulative export in kWh. |

When available, true grid-meter totals are preferred because they represent what the supplier sees. If these are not available, Home Energy Manager can use the configured inverter import/export totals as fallback inputs.

## 3. Inverter

These settings let the forecast reproduce the battery's current configuration and allow the controller to write future plans.

| Setting | What it represents |
|---|---|
| `inverter.house_load_energy_total_kwh` | Cumulative house/load energy in kWh measured on the battery side of the installation. |
| `inverter.import_energy_total_kwh` | Inverter cumulative grid import in kWh. |
| `inverter.export_energy_total_kwh` | Inverter cumulative grid export in kWh. |
| `inverter.max_rate_w` | Hardware maximum battery charge/discharge rate in watts. |
| `inverter.charge_rate_w` | Configured scheduled charge rate in watts. |
| `inverter.discharge_rate_w` | Configured scheduled discharge rate in watts. |
| `inverter.eco_mode` | Entity reporting the inverter's normal self-consumption/eco setting. |
| `inverter.charge_schedule_enabled` | Whether scheduled charging is enabled. |
| `inverter.discharge_schedule_enabled` | Whether scheduled discharging is enabled. |
| `inverter.pause_mode` | Current pause mode, such as Disabled/PauseCharge/PauseDischarge/PauseBoth. |
| `inverter.pause_start`, `inverter.pause_end` | Pause window start/end settings. |
| `inverter.charge_start_1`, `charge_end_1`, `charge_target_1` | First charge slot settings. |
| `inverter.charge_start_2`, `charge_end_2`, `charge_target_2` | Second charge slot settings. |
| `inverter.discharge_start_1`, `discharge_end_1`, `discharge_target_1` | First discharge slot settings. |
| `inverter.discharge_start_2`, `discharge_end_2`, `discharge_target_2` | Second discharge slot settings. |

For controller modes, configure all inverter settings that Home Energy Manager is expected to read or write. Do not point a schedule field at a live power sensor.

## 4. Tariff

Home Energy Manager can use current rates plus richer current-day/next-day rate entities.

| Setting | What to select |
|---|---|
| `tariff.import_current_rate` | Current import rate. |
| `tariff.export_current_rate` | Current export rate. |
| `tariff.import_current_day_rates` | Entity whose attributes contain today's import rate periods. |
| `tariff.import_next_day_rates` | Entity whose attributes contain tomorrow's import rate periods. |
| `tariff.export_current_day_rates` | Entity whose attributes contain today's export rate periods. |
| `tariff.export_next_day_rates` | Entity whose attributes contain tomorrow's export rate periods. |

The detailed day-rate entities give the forecaster enough information to identify cheap/off-peak periods and model costs accurately. If they are unavailable, current-rate data is only a degraded fallback.

## 5. Solar

| Setting | What to select |
|---|---|
| `solar.energy_total_kwh` | Cumulative PV generation in kWh. |
| `solar.solcast_today` | Solcast-style forecast entity for today. |
| `solar.solcast_tomorrow` | Solcast-style forecast entity for tomorrow. |
| `solar.solcast_day_3` | Optional third-day forecast used to fill horizon gaps. |

The supplied Solcast entity IDs are examples. Replace them if your Solcast integration uses different names.

## 6. Heat pump / central heating

These inputs drive CH learning and forecasting.

| Setting | Required meaning |
|---|---|
| `heat_pump.ch_energy_total_kwh` | Cumulative electrical energy used for central/space heating, in kWh. |
| `heat_pump.outdoor_temperature` | Outdoor temperature in °C, ideally the heat pump's own shaded external sensor. |
| `heat_pump.weather` | Home Assistant weather entity that supplies forecast temperatures. |
| `heat_pump.summer_mode_off` | Controller threshold below which heating/winter mode becomes active. |
| `heat_pump.summer_mode_on` | Controller threshold above which summer mode suppresses normal space heating. |

All five are required by the current configuration schema.

## 7. Domestic hot water

| Setting | Required meaning |
|---|---|
| `dhw.energy_total_kwh` | Cumulative heat-pump DHW electrical energy in kWh. |
| `dhw.mode` | DHW mode entity, for example Off/On/Schedule. |
| `dhw.tank_temperature` | Main/current cylinder temperature in °C. |
| `dhw.target_temperature` | Configured DHW target temperature in °C. |
| `dhw.hysteresis` | DHW reheating hysteresis in °C. |
| `dhw.schedule_prefix` | Common entity-ID prefix used for the DHW weekday AM/PM schedule entities. |

Optional thermal inputs improve the tank model when available:

- `dhw.tank_upper_temperature` — upper cylinder sensor;
- `dhw.tank_lower_temperature` — lower cylinder sensor;
- `dhw.ambient_temperature` — temperature around the cylinder, useful for standing-loss modelling.

The DHW forecast estimates the electrical energy needed to reach the configured target from the simulated tank state. When the authoritative DHW forecast is temporarily unavailable, CH can continue updating while DHW is reported unavailable rather than being silently treated as zero.

## 8. EV Smart Charging (optional)

| Setting | Purpose |
|---|---|
| `ev.energy_total_kwh` | Cumulative energy measured at the EV charger, if available. |
| `ev.smart_charging_dispatch` | Entity containing planned/confirmed smart-charging dispatches. |
| `ev.smart_charging_active` | Entity indicating that smart charging is currently active/confirmed. |
| `ev.smart_charging_enabled` | Enables EV Smart Charging handling. |

EV energy is modelled separately from ordinary household baseline load when configured.

## 9. Energy strategy / controller

The default mode is `forecast_only`.

Important settings:

| Setting | Default | Meaning |
|---|---:|---|
| `operation_mode` | `forecast_only` | Selects controller behaviour. |
| `safety_buffer_soc` | 20% | Battery SOC buffer the controller aims to protect at the next regular off-peak period. |
| `minimise_export_min_soc` | 25% | Minimum SOC floor used by minimise-export planning; the controller may target higher when required to avoid peak import. |
| `calibration_enabled` | true | Enables periodic battery calibration planning. |
| `top_full_every_days` | 14 | Maximum interval between top-end/full calibration opportunities. |
| `deep_cycle_every_days` | 60 | Interval used for low-end calibration planning. |
| `deep_cycle_floor_soc` | 4% | Low-SOC calibration target/floor. |
| `reserve_dwell_minutes` | 15 | Time to remain at the low endpoint during a requested deep calibration. |
| `export_start` | 20:00 | Earliest normal forced export start in `export_generated`. |
| `preferred_charge_c_rate` | 0.25 | Preferred battery charge rate as a fraction of battery capacity per hour. |
| `max_charge_c_rate` | 0.4 | Normal maximum charge C-rate used by planning. |
| `charge_safety_margin_minutes` | 10 | Margin kept when sizing timed charging. |
| `forecast_stale_minutes` | 15 | Forecast age after which controller data is treated as stale. |
| `write_retry_attempts` | 4 | Number of attempts for inverter writes. |
| `write_retry_delay_seconds` | 10 | Delay between write retries. |
| `controller_refresh_timeout_seconds` | 90 | Maximum wait for a refreshed forecast after controller changes. |

Most timing/deadband/rate-match settings under this section are advanced tuning values. Leave the defaults unless logs show a specific reason to adjust them.

## 10. Axle (optional)

Home Energy Manager reads Axle event data from Home Assistant entities supplied by the Axle integration; it does not log in to Axle itself.

Configure:

- event start time;
- event end time;
- Import/Export direction;
- event-window state;
- updated-at timestamp;
- package output entity (default `sensor.home_energy_manager_axle`).

Only qualifying **Export** events cause battery action. Import events may be reported but do not trigger battery charging/discharging.

## 11. Advanced settings

### Home forecaster

The defaults are suitable for most installations:

- history: 28 days;
- same-weekday weighting enabled;
- 5-minute whole-home simulation interval;
- fallback charge/discharge efficiency: 95%;
- minimum baseline load: 200 W;
- log level: INFO.

Change these only when tuning forecast behaviour or diagnosing a problem.

### ASHP forecaster

Defaults include:

- degree-day base: 15.5 °C;
- fallback winter/summer thresholds: 11/12 °C;
- initial coefficient: 1.55 kWh/DD;
- 30-day CH training window;
- 48-hour forecast horizon;
- 30-minute ASHP output slots;
- 15-minute refresh interval;
- 250 L DHW tank volume;
- 5-minute DHW thermal sampling.

The threshold values in this advanced section are fallbacks; the live summer-mode threshold entities are preferred when available.

### Battery learning

Battery learning settings control how charge/discharge sessions and idle periods are accepted. The defaults are deliberately conservative. Configure the cumulative battery charge/discharge energy entities before expecting useful efficiency learning.

### MQTT

Leave MQTT discovery enabled for normal Home Assistant presentation. With **Auto-discover broker** enabled, the app asks Home Assistant for the configured MQTT service. Only enter host/port/credentials when automatic discovery is not suitable.

# First start and verification

Keep `energy_strategy.operation_mode` set to `forecast_only`.

After starting the app, verify these areas in order.

## 1. Check the app log

Open the app's **Log** tab. Look for:

- unavailable/non-numeric required entities;
- missing weather forecast data;
- tariff data that could not be parsed;
- stale forecast warnings;
- DHW thermal model readiness/alignment messages.

Fix input problems before enabling control.

## 2. Check ASHP outputs

Useful entities include:

- `sensor.ashp_forecast_next_30m`
- `sensor.ashp_forecast_remaining_today`
- `sensor.ashp_forecast_next_24h`
- `sensor.ashp_forecast_next_48h`
- `sensor.ashp_forecast_ch_next_48h`
- `sensor.ashp_forecast_dhw_next_48h`
- `sensor.ashp_forecast_kwh_per_degree_day`
- `sensor.ashp_forecast_health`
- `sensor.ashp_dhw_production_source`

The detailed 48-hour forecast is exposed in the `forecast` attribute and contains half-hour rows including `ch_kwh`, `dhw_kwh`, temperature and heating state.

A healthy forecast should update regularly. If DHW becomes unavailable, `sensor.ashp_forecast_health` reports degraded DHW status while CH may remain fresh.

## 3. Check whole-home outputs

Useful entities include:

- `sensor.home_energy_forecast`
- `sensor.home_energy_forecast_health`
- `sensor.home_energy_forecast_comparison`

The detailed forecast should show plausible load, CH, DHW, PV, battery, SOC, import/export and cost values. Compare current SOC and household load with the source entities before trusting future planning.

## 4. Check controller output

`sensor.home_energy_controller` reports controller state/reasoning. In `forecast_only`, it should not make inverter changes.

Battery-learning diagnostics may also be available, including charge-curve and learning/efficiency entities.

# Operating modes

## `forecast_only` — recommended first-run mode

Runs the forecasters and controller diagnostics without writing inverter/battery settings. Use this for installation, validation and troubleshooting.

## `minimise_export`

Designed for situations where exporting energy has little or no value. The controller tries to retain/use energy locally while still prioritising avoidance of peak-rate grid import and protection of the configured safety buffer. Overnight charging is sized from the forecast rather than treating the configured minimum SOC as a fixed target.

## `maximise_export`

Runs the normal optimisation strategy with an export-oriented objective while preserving controller safety rules. Use only when exported energy has sufficient value and your tariff/meter inputs are correctly configured.

## `export_generated`

Targets export related to genuine solar generation. Normal forced export does not begin before the configured `export_start`, must stop by the regular off-peak period, and confirmed EV Smart Charging periods do not permit normal forced export. True grid-export metering is preferred for measuring credited export.

## `axle_only`

Leaves normal battery behaviour alone except when Home Energy Manager needs to prepare for or execute a qualifying Axle **Export** event. Temporary settings are restored/replanned after the event. This is useful when Axle support is wanted without the normal daily optimiser.

## Axle overlay in normal modes

Qualifying Axle Export events can overlay `minimise_export`, `maximise_export` and `export_generated`. `forecast_only` remains completely write-free.

# Controller safety behaviour

Home Energy Manager's controller is designed around these priorities:

1. avoid avoidable peak-rate grid import;
2. preserve battery reserve/safety constraints;
3. optimise/export surplus energy.

Normal battery operation remains self-consumption/eco mode outside explicitly planned charge, discharge or pause periods.

The controller never uses the inverter charge-target SOC setting as a normal planning control. Charge quantity is controlled by schedule duration/rate. Forced discharge quantity is likewise controlled by duration while preserving the configured reserve target.

# Calibration

When calibration is enabled, Home Energy Manager can periodically plan top-end and low-end battery calibration opportunities.

For low calibration in `minimise_export`, the controller prefers natural household consumption to reduce SOC. Forced discharge/export is used only for the remaining shortfall when required, and the controller plans the recharge within the same cheap period when practical. If there is not enough cheap-rate time for the required low dwell and recharge, calibration is postponed rather than deliberately extending the recharge into peak-rate time.

# Troubleshooting

## The app starts but forecast entities are missing

- Check the app log for an unavailable required entity.
- Confirm MQTT is working if you expect MQTT-discovered entities.
- Check **Developer Tools → States** for the entity IDs.
- Restart the app after correcting configuration.

## ASHP CH forecast is zero or implausible

- Confirm `heat_pump.ch_energy_total_kwh` is cumulative CH-only electrical energy.
- Confirm outdoor temperature is in °C and represents outdoor conditions.
- Confirm the weather entity provides hourly forecast temperatures.
- Check summer-mode thresholds and `sensor.ashp_forecast_kwh_per_degree_day`.
- Remember that several valid heating days are needed before learned data becomes representative.

## DHW forecast is unavailable

Check:

- DHW energy total;
- mode, target and hysteresis entities;
- tank temperature sensors;
- schedule prefix/entities;
- `sensor.ashp_dhw_production_source`;
- `sensor.ashp_forecast_health`;
- app log messages mentioning DHW thermal readiness/alignment.

CH should remain able to update during a DHW-specific failure.

## Whole-home forecast looks wrong

Check whether you accidentally selected:

- power (`W`) instead of cumulative energy (`kWh`);
- inverter import/export instead of true grid meters when true meters are available;
- a load meter that includes/excludes the EV differently from your configured EV measurement;
- stale or incomplete tariff/Solcast entities.

Compare the source entities with the forecast's current slot before adjusting advanced tuning values.

## Controller is not writing settings

- Confirm the mode is not `forecast_only`.
- Check `sensor.home_energy_controller` and the app log for the reason.
- Confirm the forecast is fresh.
- Confirm all configured inverter entities are available and writable through the underlying integration.
- Confirm another automation is not immediately overwriting the same settings.

## Unexpected charging/discharging

Return to `forecast_only` first. Then check:

- tariff periods;
- confirmed EV Smart Charging periods;
- Axle events;
- calibration state;
- safety-buffer and reserve values;
- the controller reason/status entity and logs.

Do not troubleshoot control behaviour by repeatedly changing advanced deadbands or timing values before the input data has been verified.

# Getting help

When reporting a problem, include:

- the Home Energy Manager version;
- selected operation mode;
- the relevant entity states/attributes;
- the app log covering the event;
- what you expected to happen and what actually happened.

Do not publish credentials, API keys or other secrets.


## Free electricity periods

When your supplier announces a free electricity period, open **Settings → Apps → Home Energy Manager → Open Web UI**. In **Free electricity periods**, select the start and end date/time and choose **Add period**. Repeat for separate periods. Saved periods appear below the form, where you can remove one at any time, including while it is active.

Periods are one-off, not recurring. They are stored by the app and remain available after restarting Home Assistant or the app. You do not need to create a template sensor, edit YAML, or restart the app when adding or removing a period. The displayed times use your Home Assistant timezone; ensure your device is set to the same timezone when entering a period.

The forecaster uses 0 p/kWh for import within a saved period and the usual supplier import price outside it. It checks for saved-period changes approximately every five seconds and updates its forecast. The regular overnight off-peak period is still identified from the supplier tariff, not these manual periods. Adding a period does not directly command the battery, hot water or calibration. Supplier billing and any later account credit are not changed by the app.

If **Open Web UI** is missing, confirm you are running an app version that includes the free-electricity editor. If the forecast does not reflect a newly saved period, check the app log and `sensor.home_energy_forecast_health`.
