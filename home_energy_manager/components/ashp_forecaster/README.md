# ASHP Energy Forecaster v0.2.0

Forecasts both space-heating (CH) and domestic hot water (DHW) electricity. DHW has priority: any 30-minute slot with forecast DHW activity has CH set to zero, and missed CH is not carried forward. The detailed forecast exposes `ch_kwh`, `dhw_kwh`, `energy_kwh`, and `dhw_active`.

# ASHP Energy Forecaster

A small Home Assistant add-on that forecasts space-heating electrical energy from an hourly weather forecast using a heating-degree-day model.

## Default inputs

- CH cumulative energy: `sensor.ashp_electrical_energy_ch`
- Outdoor temperature: `sensor.ecomax360i_outdoor_temperature`
- Weather forecast: `weather.forecast_home`

DHW is deliberately excluded from the model.

## Model

For each forecast slot:

`degree_day_fraction = max(base_temperature - outdoor_temperature, 0) * slot_hours / 24`

`forecast_kwh = degree_day_fraction * kwh_per_degree_day`

The initial coefficient is 1.55 kWh/DD. The add-on retrieves Home Assistant history and replaces it with a measured coefficient when qualifying complete heating days are available.

A training day is included by default when:

- degree days >= 2.0 DD at the configured 15.5 C base; and
- CH electrical energy >= 2.0 kWh.

Training degree days are integrated from the outdoor-temperature history using `max(base - temperature, 0)` through each complete day. This avoids shoulder-season distortion from using only a whole-day mean temperature. The learned coefficient is calculated from aggregate energy divided by aggregate degree days, rather than averaging individual daily ratios.

## Output states

The add-on publishes these state entities through Home Assistant's REST API:

- `sensor.ashp_forecast_next_30m`
- `sensor.ashp_forecast_remaining_today`
- `sensor.ashp_forecast_next_24h`
- `sensor.ashp_forecast_next_48h`
- `sensor.ashp_forecast_kwh_per_degree_day`

The `sensor.ashp_forecast_next_24h` attributes include the half-hour forecast series.

These are REST-created states rather than entities supplied by a custom integration. They are republished by the add-on after Home Assistant/add-on restarts.

## Local installation for development

This directory is structured as a Home Assistant add-on repository. Put the repository in GitHub (or another Git repository reachable by Home Assistant), update the placeholder URLs in `repository.yaml` and `ashp_forecast/config.yaml`, then add that repository URL in **Settings -> Apps -> App store -> Repositories**.

For local add-on development you can also place the `ashp_forecast` directory beneath `/addons/ashp_forecast` using the Samba/SSH development workflow, then reload the app store.

## Notes

- Home Assistant Recorder must retain enough raw history to calculate the requested training period. If less history is available, the model uses the available complete days.
- The weather entity must support an hourly forecast through `weather.get_forecasts`.
- SQLite under `/data/ashp_forecast.db` caches calculated daily training rows; Home Assistant remains the source of raw measurements.
- Version 0.1 intentionally has no ASHP control logic.


## v0.1.2 training model

Training is performed from 30-minute observations. Each slot independently measures CH energy and mean outdoor temperature, converts that temperature to a degree-day fraction, and the learned kWh/DD coefficient is `sum(slot CH kWh) / sum(slot DD)` across qualifying heating slots. This avoids using a daily mean temperature and keeps training aligned with the 30-minute forecast model.


## Heating-mode hysteresis

The forecaster models the ASHP controller separately from the 15.5°C heating-demand base. It reads the live summer-mode hysteresis thresholds from `number.ecomax360i_summer_mode_off` and `number.ecomax360i_summer_mode_on`. It enters winter/heating mode below the current OFF threshold, retains the previous mode inside the hysteresis band, and returns to summer mode above the current ON threshold. Degree-day demand is only accumulated while the simulated controller is in winter mode. The numeric threshold options remain as fallbacks if the HA entities cannot be read.


## v0.1.6 historical hysteresis

Historical training now reads the Home Assistant Recorder history for `number.ecomax360i_summer_mode_off` and `number.ecomax360i_summer_mode_on`. Every 30-minute training slot uses the threshold values that were actually configured at that timestamp. Current threshold values are used for future forecasts only (and as a fallback if historical threshold data is unavailable). Changing the controller thresholds therefore does not reinterpret older training data.


## v0.1.6

`sensor.ashp_forecast_next_48h` now includes the full 30-minute `forecast` attribute, matching the detailed forecast data exposed by the 24-hour sensor.
