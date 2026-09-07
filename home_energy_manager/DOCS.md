# Home Energy Manager

Home Energy Manager combines three separate engines in one Home Assistant app:

- **ASHP Energy Forecaster** — learns and forecasts CH/DHW electrical demand.
- **Home Energy Forecaster** — forecasts house load, ASHP, PV, EV, grid, battery SOC and cost.
- **Home Energy Controller** — plans and applies battery charge/discharge/pause settings.

> Stop any old standalone copies before running this app. Do not run two controller instances against the same inverter.

## Configuration guidance

The Configuration tab contains inline names and descriptions for every option. This document contains the same settings as a complete reference.

**Entity IDs:** enter the complete Home Assistant entity ID, such as `sensor.example`. Fields described as cumulative energy should normally use monotonically increasing kWh sensors rather than instantaneous power sensors.

**Optional fields:** fields shown as optional may be left blank/omitted. If you explicitly configure a true utility meter, Home Energy Manager treats it as authoritative rather than silently changing accounting source when it is unavailable.

**EV Smart Charging:** this is supplier-independent terminology. The dispatch entity should expose planned smart-charging slots (including Kraken-style `planned_dispatches` where available); the EV energy entity is the measured cumulative kWh source used to learn charging power.

## ASHP forecaster

Learns space-heating and hot-water demand from historical Home Assistant data and publishes a forward ASHP energy forecast.

| Setting | Type | Default | Description |
|---|---|---|---|
| **CH energy total** (`ch_energy_entity`) | `str` | `sensor.ashp_electrical_energy_ch` | Cumulative kWh sensor for ASHP space-heating electrical energy. Use a monotonically increasing energy sensor. |
| **Outdoor temperature** (`outdoor_temperature_entity`) | `str` | `sensor.ecomax360i_outdoor_temperature` | Outdoor-air temperature sensor used for degree-day learning and heating-mode decisions. |
| **Weather forecast** (`weather_entity`) | `str` | `weather.forecast_home` | Home Assistant weather entity used to obtain forecast outdoor temperatures. |
| **Degree-day base temperature** (`base_temperature_c`) | `float` | `15.5` | Base temperature in °C used when calculating heating degree days. |
| **Summer-mode off threshold entity** (`summer_mode_off_entity`) | `str` | `number.ecomax360i_summer_mode_off` | Entity containing the controller threshold below which heating/winter mode is selected. Equal off/on thresholds are allowed. |
| **Summer-mode on threshold entity** (`summer_mode_on_entity`) | `str` | `number.ecomax360i_summer_mode_on` | Entity containing the controller threshold above which summer mode is selected. Equal off/on thresholds are allowed. |
| **Fallback winter threshold** (`winter_mode_below_c`) | `float(-30,30)` | `11.0` | Fallback °C threshold used when the configured summer-mode threshold entities cannot be read. |
| **Fallback summer threshold** (`summer_mode_above_c`) | `float(-30,30)` | `12.0` | Fallback °C threshold used when the configured summer-mode threshold entities cannot be read. |
| **Initial kWh per degree day** (`initial_kwh_per_degree_day`) | `float` | `1.55` | Starting CH energy coefficient used until sufficient history has been learned. |
| **CH training days** (`training_days`) | `int(1,180)` | `30` | Maximum number of historical days used to learn space-heating demand. |
| **Minimum daily degree days** (`minimum_daily_degree_days`) | `float(0,20)` | `2.0` | Days below this heating-degree-day total are excluded from CH coefficient training. |
| **Minimum daily CH energy** (`minimum_daily_ch_kwh`) | `float(0,100)` | `2.0` | Days with less CH electrical energy than this are excluded from CH coefficient training. |
| **Forecast horizon** (`forecast_hours`) | `int(1,168)` | `48` | Number of hours of ASHP demand to forecast. |
| **ASHP forecast interval** (`forecast_interval_minutes`) | `int(5,60)` | `30` | Length in minutes of each ASHP forecast slot. |
| **ASHP update interval** (`update_minutes`) | `int(5,60)` | `15` | How often, in minutes, the ASHP forecast is recalculated. |
| **DHW energy total** (`dhw_energy_entity`) | `str` | `sensor.ashp_electrical_energy_dhw` | Cumulative kWh sensor for ASHP domestic-hot-water electrical energy. |
| **DHW mode** (`dhw_mode_entity`) | `str` | `select.dhw_mode` | Entity reporting the heat-pump DHW operating mode, used to understand scheduled/disabled hot-water operation. |
| **DHW tank temperature** (`dhw_tank_temperature_entity`) | `str` | `sensor.dhw_temperature` | Current hot-water cylinder temperature sensor. |
| **DHW target temperature** (`dhw_target_temperature_entity`) | `str` | `number.dhw_target_temperature` | Entity containing the configured hot-water target temperature. |
| **DHW hysteresis** (`dhw_hysteresis_entity`) | `str` | `number.dhw_hysteresis` | Entity containing the DHW reheating hysteresis used to estimate when a heating cycle is required. |
| **DHW schedule entity prefix** (`dhw_schedule_prefix`) | `str` | `number.dhw_dhw_schedule_` | Common entity-ID prefix for the DHW schedule entities. The forecaster appends the weekday/period suffixes it expects. |
| **DHW history days** (`dhw_history_days`) | `int(7,90)` | `28` | Number of historical days used when learning domestic-hot-water consumption. |
| **DHW activity threshold** (`dhw_activity_threshold_kwh`) | `float(0,10)` | `0.2` | Minimum interval energy in kWh treated as a genuine DHW heating event rather than meter noise. |

## Home energy forecaster

Builds the whole-home forecast from household demand, ASHP, solar, battery, tariff, meter and EV inputs.

### General forecast settings

Core model timing, history and efficiency settings.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Timezone** (`timezone`) | `str` | `Europe/London` | IANA timezone used for tariff periods, schedules and forecast timestamps, for example Europe/London. |
| **Home-load history days** (`history_days`) | `int(7,90)` | `28` | Number of historical days used to learn normal household load. |
| **Weight matching weekdays** (`day_of_week_weighting`) | `bool` | `true` | Give additional weight to historical days matching the weekday being forecast. |
| **Same-weekday weight** (`same_weekday_weight`) | `float(1.0,10.0)` | `1.5` | Multiplier applied to matching weekdays when weekday weighting is enabled. |
| **Home forecast interval** (`forecast_interval_minutes`) | `int(1,60)` | `5` | Length in minutes of each whole-home forecast slot. |
| **Controller refresh request entity** (`controller_refresh_request_entity`) | `str` | `sensor.home_energy_forecast_refresh_request` | Internal Home Assistant entity used by the controller to request an immediate post-plan forecast refresh. Normally leave at the default. |
| **Fallback charge efficiency** (`charge_efficiency`) | `float(0.5,1.0)` | `0.95` | Battery charge efficiency used when learned charge-curve information is unavailable. Enter as a fraction, e.g. 0.95. |
| **Fallback discharge efficiency** (`discharge_efficiency`) | `float(0.5,1.0)` | `0.95` | Battery discharge efficiency used when learned information is unavailable. Enter as a fraction, e.g. 0.95. |
| **Minimum baseline load** (`minimum_baseline_w`) | `int(0,5000)` | `200` | Minimum non-EV/non-ASHP household load in watts enforced by the forecast model. |
| **Log level** (`log_level`) | `list(INFO|DEBUG)` | `INFO` | INFO for normal operation or DEBUG for detailed diagnostic logging. |

### Battery entities

Entities describing battery state, limits, operating modes and inverter charge/discharge schedules.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Battery state of charge** (`soc`) | `str` | blank | Current battery SOC sensor, in percent. |
| **Battery capacity** (`capacity_kwh`) | `str` | blank | Entity containing usable/nominal battery capacity in kWh. |
| **Battery reserve SOC** (`reserve_soc`) | `str` | blank | Entity containing the configured minimum battery reserve percentage. |
| **Inverter maximum rate** (`inverter_max_rate_w`) | `str` | blank | Entity containing the inverter maximum battery charge/discharge power in watts. |
| **Battery charge rate** (`charge_rate_w`) | `str` | blank | Entity containing the currently configured battery charge power/rate in watts. |
| **Battery discharge rate** (`discharge_rate_w`) | `str` | blank | Entity containing the currently configured battery discharge power/rate in watts. |
| **Eco mode** (`eco_mode`) | `str` | blank | Entity reporting whether the inverter/battery eco operating mode is enabled. |
| **Charge schedule enabled** (`charge_schedule_enabled`) | `str` | blank | Entity reporting whether scheduled battery charging is enabled. |
| **Discharge schedule enabled** (`discharge_schedule_enabled`) | `str` | blank | Entity reporting whether scheduled battery discharge is enabled. |
| **Pause mode** (`pause_mode`) | `str` | blank | Entity reporting the inverter pause mode, such as PauseCharge. |
| **Pause start** (`pause_start`) | `str` | blank | Entity containing the active inverter pause-slot start time. |
| **Pause end** (`pause_end`) | `str` | blank | Entity containing the active inverter pause-slot end time. |
| **Charge slot 1 start** (`charge_start_1`) | `str` | blank | Entity containing battery charge slot 1 start time. |
| **Charge slot 1 end** (`charge_end_1`) | `str` | blank | Entity containing battery charge slot 1 end time. |
| **Charge slot 1 target SOC** (`charge_target_1`) | `str` | blank | Entity containing the target SOC percentage for charge slot 1. |
| **Charge slot 2 start** (`charge_start_2`) | `str` | blank | Entity containing battery charge slot 2 start time. |
| **Charge slot 2 end** (`charge_end_2`) | `str` | blank | Entity containing battery charge slot 2 end time. |
| **Charge slot 2 target SOC** (`charge_target_2`) | `str` | blank | Entity containing the target SOC percentage for charge slot 2. |
| **Discharge slot 1 start** (`discharge_start_1`) | `str` | blank | Entity containing battery discharge slot 1 start time. |
| **Discharge slot 1 end** (`discharge_end_1`) | `str` | blank | Entity containing battery discharge slot 1 end time. |
| **Discharge slot 1 target SOC** (`discharge_target_1`) | `str` | blank | Entity containing the target SOC percentage for discharge slot 1. |
| **Discharge slot 2 start** (`discharge_start_2`) | `str` | blank | Entity containing battery discharge slot 2 start time. |
| **Discharge slot 2 end** (`discharge_end_2`) | `str` | blank | Entity containing battery discharge slot 2 end time. |
| **Discharge slot 2 target SOC** (`discharge_target_2`) | `str` | blank | Entity containing the target SOC percentage for discharge slot 2. |

### House load

Input used to learn normal household consumption.

| Setting | Type | Default | Description |
|---|---|---|---|
| **House-load energy total** (`energy_total_kwh`) | `str` | blank | Cumulative kWh sensor for the load measured behind the battery/inverter. EV should not be included when the charger is outside that measurement point. |

### Solar PV

PV generation history and Solcast forecast entities.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Solar generation energy total** (`energy_total_kwh`) | `str` | blank | Cumulative kWh sensor for measured PV generation. |
| **Solcast today** (`solcast_today`) | `str` | `sensor.solcast_pv_forecast_forecast_today` | Solcast entity providing today’s PV forecast and detailed forecast attributes. |
| **Solcast tomorrow** (`solcast_tomorrow`) | `str` | `sensor.solcast_pv_forecast_forecast_tomorrow` | Solcast entity providing tomorrow’s PV forecast and detailed forecast attributes. |
| **Solcast day 3** (`solcast_day_3`) | `str?` | `sensor.solcast_pv_forecast_forecast_day_3` | Optional Solcast entity for the third forecast day, used when the forecast horizon reaches it. |

### Tariff and inverter meter inputs

Import/export tariff entities plus cumulative grid energy seen by the battery/inverter. True whole-property meters can be configured separately below.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Inverter-side import energy** (`import_energy_total_kwh`) | `str` | blank | Cumulative grid-import kWh seen by the battery/inverter meter. Used as a fallback when no true whole-property import meter is configured. |
| **Inverter-side export energy** (`export_energy_total_kwh`) | `str` | blank | Cumulative grid-export kWh seen by the battery/inverter meter. Used as a fallback when no true whole-property export meter is configured. |
| **Current import rate** (`import_current_rate`) | `str` | blank | Entity containing the current electricity import price. |
| **Current export rate** (`export_current_rate`) | `str` | blank | Entity containing the current electricity export price. |
| **Import rates today** (`import_current_day_rates`) | `str` | blank | Entity whose attributes contain today’s time-of-use import-rate periods. |
| **Import rates tomorrow** (`import_next_day_rates`) | `str` | blank | Entity whose attributes contain tomorrow’s time-of-use import-rate periods. |
| **Export rates today** (`export_current_day_rates`) | `str` | blank | Entity whose attributes contain today’s export-rate periods. |
| **Export rates tomorrow** (`export_next_day_rates`) | `str` | blank | Entity whose attributes contain tomorrow’s export-rate periods. |

### ASHP forecast inputs

ASHP forecast and measured CH/DHW totals consumed by the whole-home forecaster.

| Setting | Type | Default | Description |
|---|---|---|---|
| **ASHP 48-hour forecast** (`forecast_48h`) | `str` | `sensor.ashp_forecast_next_48h` | ASHP Forecaster entity containing the detailed forward CH/DHW forecast. |
| **ASHP CH energy total** (`ch_energy_total_kwh`) | `str` | `sensor.ashp_electrical_energy_ch` | Cumulative kWh space-heating electrical energy used to separate CH from base house load. |
| **ASHP DHW energy total** (`dhw_energy_total_kwh`) | `str` | `sensor.ashp_electrical_energy_dhw` | Cumulative kWh domestic-hot-water electrical energy used to separate DHW from base house load. |

### True whole-property grid meters

Optional authoritative utility/smart-meter cumulative energy sensors. Use these when loads such as an EV charger sit outside the battery/inverter grid meter.

| Setting | Type | Default | Description |
|---|---|---|---|
| **True grid import energy** (`import_energy_total_kwh`) | `str?` | blank | Optional cumulative whole-property import kWh sensor. When configured it is authoritative; temporary unavailability causes a visible forecast error rather than silent fallback. |
| **True grid export energy** (`export_energy_total_kwh`) | `str?` | blank | Optional cumulative whole-property export kWh sensor. When configured it is authoritative; temporary unavailability causes a visible forecast error rather than silent fallback. |

### EV charging

EV metering and supplier-independent smart-charging inputs. Compatible with Kraken-style planned dispatches used by providers such as Octopus and EDF.

| Setting | Type | Default | Description |
|---|---|---|---|
| **EV energy total** (`energy_total_kwh`) | `str` | blank | Cumulative kWh sensor measuring EV charger consumption, for example an energy sensor derived from a CT clamp. Used to learn typical EV charging power. |
| **EV Smart Charging dispatches** (`smart_charging_dispatch_entity`) | `str` | blank | Entity whose attributes provide planned smart-charging dispatches/slots. Forecast EV timing comes from these slots. |
| **EV Smart Charging active** (`smart_charging_active_entity`) | `str` | blank | Optional entity indicating that a smart EV charging slot is currently active; used to confirm the present interval. |

## Controller

Plans and applies battery charge/discharge/pause settings from the latest forecast.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Controller status entity** (`status_entity_id`) | `str` | `sensor.home_energy_controller` | Entity ID requested for the controller’s published Home Assistant status sensor. |
| **Home energy forecast entity** (`home_energy_forecast_entity`) | `str` | `sensor.home_energy_forecast` | Whole-home forecast sensor consumed by the controller. Auto-discovery can recover the MQTT entity if its registry ID differs. |
| **Safety buffer SOC** (`safety_buffer_soc`) | `int(1,99)` | `20` | Battery SOC percentage the controller aims to preserve as a guardrail against forecast error and unexpected load. |
| **Operation mode** (`operation_mode`) | `list(maximise_export|minimise_export|export_generated)` | `maximise_export` | Battery strategy: maximise_export, minimise_export, or export_generated. |
| **Power-down handling** (`power_down_enabled`) | `bool` | `true` | Enable special handling for configured power-down events. |
| **Power-down events entity** (`power_down_events_entity`) | `str` | blank | Optional entity describing upcoming power-down/outage events used by the controller. |
| **Power-down import baseline** (`power_down_import_baseline_entity`) | `str` | blank | Optional baseline entity used by power-down planning for grid import. |
| **Power-down export baseline** (`power_down_export_baseline_entity`) | `str` | blank | Optional baseline entity used by power-down planning for grid export. |
| **Minimise-export minimum SOC** (`minimise_export_min_soc`) | `int(1,99)` | `25` | Minimum SOC percentage maintained while operating in minimise_export mode. |
| **Battery calibration** (`calibration_enabled`) | `bool` | `true` | Allow periodic full-charge and deep-cycle calibration behavior. |
| **Full-charge interval** (`top_full_every_days`) | `int(1,365)` | `14` | Maximum number of days between calibration charges to 100%. |
| **Deep-cycle interval** (`deep_cycle_every_days`) | `int(1,730)` | `60` | Maximum number of days between calibration discharges toward the configured floor. |
| **Deep-cycle floor SOC** (`deep_cycle_floor_soc`) | `int(1,20)` | `4` | SOC percentage used as the target floor for a calibration deep cycle. |
| **Reserve dwell time** (`reserve_dwell_minutes`) | `int(0,180)` | `30` | Minutes the battery should remain at reserve during applicable calibration/deep-cycle behavior. |
| **Preferred export start** (`export_start`) | `str` | `20:00` | Earliest preferred clock time for scheduled export in modes that deliberately export stored energy. |
| **Export-generated solar threshold** (`export_generated_solar_threshold_w`) | `int(0,2000)?` | — | PV power threshold used when identifying meaningful generation for export-generated pause behavior. If omitted, the controller uses its internal default. |
| **Preferred charge C-rate** (`preferred_charge_c_rate`) | `float` | `0.25` | Preferred battery charge rate expressed as a fraction of battery capacity per hour. |
| **Maximum charge C-rate** (`max_charge_c_rate`) | `float` | `0.4` | Maximum allowed battery charge C-rate used by planning. |
| **Controller discharge rate** (`discharge_rate_w`) | `int(0,20000)` | `0` | Requested scheduled discharge power in watts. Zero allows the controller/inverter logic to use its normal configured maximum where supported. |
| **Charge safety margin** (`charge_safety_margin_minutes`) | `int(0,120)` | `10` | Extra minutes added to planned charging so the target SOC is reached despite modelling error. |
| **Generic dwell time** (`generic_dwell_minutes`) | `int(0,120)` | `15` | Minimum duration used to avoid unnecessary rapid changes between controller actions. |
| **Forecast stale threshold** (`forecast_stale_minutes`) | `int(1,120)` | `15` | Maximum acceptable forecast age in minutes before the controller treats it as stale. |
| **Future timestamp tolerance** (`future_tolerance_minutes`) | `int(0,30)` | `2` | Allowed number of minutes that a forecast timestamp may appear to be in the future because of timing differences. |
| **Schedule time deadband** (`time_deadband_minutes`) | `int(0,30)` | `3` | Do not rewrite inverter schedule times when the proposed change is within this many minutes of the existing setting. |
| **Power-rate deadband** (`rate_deadband_w`) | `int(0,2000)` | `100` | Do not rewrite charge/discharge rates when the proposed change differs by less than this many watts. |
| **Energy deadband** (`energy_deadband_kwh`) | `float` | `0.1` | Ignore very small forecast energy differences below this kWh amount when deciding whether a plan materially changed. |
| **Charge variance threshold** (`charge_variance_threshold_kwh`) | `float` | `0.5` | Forecast charge-energy difference in kWh required before charge-plan variance is considered significant. |
| **Export variance threshold** (`export_variance_threshold_kwh`) | `float` | `0.2` | Forecast export-energy difference in kWh required before export-plan variance is considered significant. |
| **Write retry attempts** (`write_retry_attempts`) | `int(1,10)` | `4` | Maximum attempts for an inverter setting write that does not confirm successfully. |
| **Write retry delay** (`write_retry_delay_seconds`) | `int(1,600)` | `10` | Seconds to wait between failed inverter-setting write attempts. |
| **Learning sample interval** (`sample_interval_seconds`) | `int(5,300)` | `30` | Seconds between controller observations used for battery charge-curve/session learning. |
| **Rate match tolerance (%)** (`rate_match_tolerance_pct`) | `float` | `5` | Percentage tolerance when deciding whether observed battery power matches the commanded charge/discharge rate. |
| **Rate match tolerance (W)** (`rate_match_tolerance_w`) | `int` | `100` | Absolute watt tolerance used alongside the percentage rate-match tolerance. |
| **Forecast refresh timeout** (`controller_refresh_timeout_seconds`) | `int(30,600)` | `90` | Maximum seconds the controller waits for its requested post-write forecast refresh before allowing normal scheduled control to resume. |
| **EV Smart Charging awareness** (`ev_smart_charging_enabled`) | `bool` | `true` | Allow EV smart-charging slots to influence controller planning, for example by treating confirmed smart-charge slots as cheap charging opportunities. |
| **EV Smart Charging dispatches** (`ev_smart_charging_dispatch_entity`) | `str` | blank | Supplier-independent entity containing planned EV smart-charging slots/dispatches. |
| **EV Smart Charging active** (`ev_smart_charging_active_entity`) | `str` | `binary_sensor.octopus_slot_actually_charging` | Entity indicating that a smart EV charging slot is currently active. |

## MQTT publishing

Home Assistant MQTT Discovery and state publishing. Supervisor broker discovery is recommended.

| Setting | Type | Default | Description |
|---|---|---|---|
| **Enable MQTT** (`enabled`) | `bool` | `true` | Publish Home Energy Manager entities through MQTT Discovery. Components fall back to REST state publishing for a run if MQTT is unavailable. |
| **Discover MQTT broker** (`auto_discover_broker`) | `bool` | `true` | Ask Home Assistant Supervisor for the configured MQTT service connection. Recommended when using the official Mosquitto broker/app. |
| **MQTT host** (`host`) | `str?` | blank | Optional explicit broker hostname. Leave blank when automatic broker discovery is enabled and working. |
| **MQTT port** (`port`) | `int(1,65535)` | `1883` | Broker TCP port, normally 1883 without TLS. |
| **MQTT username** (`username`) | `str?` | blank | Optional explicit MQTT username. Normally unnecessary with Supervisor broker discovery. |
| **MQTT password** (`password`) | `password?` | blank | Optional explicit MQTT password. Normally unnecessary with Supervisor broker discovery. |
| **MQTT TLS/SSL** (`ssl`) | `bool` | `false` | Use TLS for an explicitly configured MQTT broker connection. |
| **Discovery prefix** (`discovery_prefix`) | `str` | `homeassistant` | Home Assistant MQTT Discovery prefix. Normally leave as homeassistant. |
| **Topic prefix** (`topic_prefix`) | `str` | `home_energy_manager` | Root MQTT topic used for Home Energy Manager state and availability messages. |
| **Migrate legacy REST entities** (`migrate_legacy_states`) | `bool` | `true` | Before MQTT discovery, remove legacy REST-created states so Home Assistant can register the MQTT entities without creating suffixed duplicates. |

## Published/persistent data

Persistent learned state is stored in the app `/data` directory:

- `/data/ashp_forecast.db`
- `/data/home_energy_forecaster.db`
- `/data/last_forecast.json`
- `/data/controller.db`

MQTT Discovery groups user-facing entities under the Home Energy Manager devices. If MQTT is unavailable during startup, a component can fall back to its REST publishing path for that run.

## Development

Read `AGENTS.md` before changing package architecture, and `components/controller/AGENTS.md` before changing controller behaviour.

## Licence

Home Energy Manager is released under the MIT License. See the repository root `LICENSE` and `THIRD_PARTY_NOTICES.md` files.
