# Home Energy Forecaster v0.2.10

Read-only Home Assistant forecaster for whole-home load, ASHP CH/DHW, PV, tariffs and GivEnergy battery behaviour.

## v0.2.4 battery pause support

The normal `forecast` now honours the configured GivTCP battery pause mode and pause window.

Supported modes:
- `Disabled`
- `PauseCharge`
- `PauseDischarge`
- `PauseBoth`

Within the pause window, charge and/or discharge are blocked in both forced-slot and Eco/self-consumption behaviour as appropriate. `battery_mode` can report `pause_charge`, `pause_discharge`, or `pause_both`.

The parallel `forecast_no_slots` deliberately ignores the pause slot as well as charge/discharge slots, preserving its purpose as a clean no-forced-schedule counterfactual.

The pause entity IDs are configurable under `battery`.

## Controller-facing forecast interface

`sensor.home_energy_forecast` now exposes:

- `forecast_generated_at`
- `offpeak.start`, `offpeak.end`, `offpeak.rate_p`
- `overnight_start_soc`
- `overnight_start_soc_no_slots`
- `forecast`
- `forecast_no_slots`
- `no_slots_totals.today`
- `no_slots_totals.tomorrow`

### `forecast`

The normal forecast. It includes the currently programmed battery charge/discharge slots.

### `forecast_no_slots`

A parallel counterfactual forecast from exactly the same starting state and non-battery inputs, but with all forced battery charge/discharge schedule slots ignored.

Eco/self-consumption remains active exactly as configured. Solar can still charge the battery and the battery can still supply the house. Battery capacity, reserve, inverter limit and charge/discharge efficiencies are unchanged.

Both arrays use the same slot schema:

- `start`
- `baseline_kwh`
- `ch_kwh`
- `dhw_kwh`
- `load_kwh`
- `pv_kwh`
- `battery_kwh` — positive discharge, negative charge
- `import_kwh`
- `export_kwh`
- `soc` — SOC at slot start
- `import_rate_p`
- `export_rate_p`
- `cost_p`
- `battery_mode`

`battery_mode` is one of:

- `eco`
- `forced_charge`
- `forced_discharge`
- `idle`

`forecast_no_slots` therefore normally reports only `eco` or `idle`.

### Off-peak selection

The controller-facing `offpeak` window is selected from the lowest import-rate morning segments. Adjacent equal-rate segments are merged; a block may start before or after midnight; qualifying blocks end by 12:00 local time; when several lowest-rate blocks qualify, the one ending latest is selected.

The existing state remains the integer forecast SOC at the selected next off-peak start. `overnight_start_soc_no_slots` is the equivalent SOC from the no-forced-slots trajectory.

### No-slots totals

`no_slots_totals.today` and `.tomorrow` provide:

- `load_kwh`
- `pv_kwh`
- `import_kwh`
- `export_kwh`
- `cost_p`

These are convenience summaries for comparing controlled behaviour with natural Eco/self-consumption behaviour.

## Existing forecasting

The add-on retains:

- 28-day recency-weighted winsorized-mean baseline model
- recent-load correction
- ASHP Energy Forecaster CH/DHW inputs
- Solcast PV
- GivEnergy battery simulation
- Octopus import/export tariffs
- EV Smart Charging dispatch tariff treatment
- frozen midnight forecast comparison entity
- 5-minute forecast cycle and startup forecast
- persistent model/history cache

## Installation

Copy the `home_energy_forecaster` directory into `/addons/`, reload the local add-on store, rebuild/update the add-on, review configuration, and start it.

All input entity IDs remain configurable.


## v0.2.8: active discharge at reserve

Forced discharge simulation already stops energy flow when the configured target/reserve is reached, even if the discharge schedule remains active. v0.2.8 makes that state explicit in the forecast.

`battery_mode` can report:

- `forced_discharge_idle_reserve` — discharge slot is still active, but the inverter/BMS has reached reserve and the battery is idle.
- `forced_discharge_idle_target` — same behaviour for a discharge target above reserve.
- `forced_charge_idle_target` — charge slot remains active after its target has been reached.

This means long discharge-to-reserve windows used by the controller and calibration are represented correctly: the forecast does not require the slot end time to coincide with the instant reserve is reached.


## Controller discovery interface (v0.2.8)

The main `sensor.home_energy_forecast` now exposes `controller_inputs`.

This is the authoritative interface between the forecaster and Home Energy Controller. It contains:

- the configured Home Assistant entity IDs for battery state and inverter controls;
- the configured timezone;
- useful current battery metadata such as capacity, reserve and maximum rate.

The controller uses the entity IDs from this block and then reads the live Home Assistant entities itself where current values are important. This avoids duplicating inverter entity configuration in both add-ons while preserving live control/readback.

Two additional forecaster configuration fields are now present under Battery:

- Battery power
- Grid power

They are published for controller discovery and are also useful for diagnostics.


## v0.2.8: energy-based controller interface

The forecaster no longer asks for live battery-power or grid-power telemetry solely for the controller.

`controller_inputs.version` is now 2. The entity map publishes cumulative energy counters instead:

- `pv_energy_total`
- `grid_import_energy_total`
- `grid_export_energy_total`

alongside SOC, capacity, reserve, inverter limits and writable schedule/control entities.

The forecaster itself continues to model power limits from configured inverter maximum and charge/discharge rate settings. It does not require instantaneous battery/grid power telemetry for forecasting.


## v0.2.8: controller-triggered immediate refresh

The forecaster/controller protocol is now `controller_inputs.version: 3`.

The forecaster publishes:
- `forecast_sequence`: monotonically increasing for every successful forecast publication.
- `refresh_request_id`: the latest controller refresh request incorporated into this forecast.
- `forecast_trigger`: `startup`, `scheduled`, `controller_refresh`, or retry equivalent.

`controller_inputs.refresh_request_entity` tells the controller where to publish refresh requests. The entity defaults to `sensor.home_energy_forecast_refresh_request` and is configurable in the forecaster.

Between normal wall-clock forecast runs the add-on checks the refresh-request entity every five seconds. A newer request wakes the forecaster immediately. The request ID is captured at the start of calculation, so a request that arrives while a forecast is being calculated cannot be accidentally acknowledged by that older calculation; it triggers another run afterwards.


## v0.2.9: fractional battery-control slots

Forced charge, forced discharge and battery-pause windows are now applied at
their actual minute boundaries rather than treating a 30-minute forecast slot
as wholly active whenever its start falls inside the control window.

Each forecast slot is split internally at any charge/discharge/pause start or
end that occurs inside it. Load and PV are apportioned by elapsed time, the
battery is simulated sequentially for each segment, and the results are
recombined into the normal 30-minute output row.

Example: a 6 kW discharge ending at 21:07 applies 6 kW for 7 minutes of the
21:00-21:30 slot (maximum 0.70 kWh forced discharge), then the remaining
23 minutes follow Eco/idle/pause behaviour.

Mixed output slots are labelled `forced_charge_partial`,
`forced_discharge_partial`, or `pause_partial` in `battery_mode`.
`forecast_no_slots` remains unchanged because it intentionally removes all
forced charge/discharge and pause controls.


## v0.2.10 — same day-of-week baseline weighting

Household baseline learning keeps the existing 28-day history, 7-day recency
half-life, 10th/90th percentile winsorisation, and recent-load correction, and
adds an extra multiplier for samples from the same day of week as the forecast
target.

Default settings:

```yaml
settings:
  day_of_week_weighting: true
  same_weekday_weight: 1.5
```

For a Tuesday forecast, historical Tuesdays receive 1.5 times their normal
recency weight. Every non-Tuesday keeps its normal recency weight. There is no
weekday/weekend grouping.


## v0.2.11 — true meter preference and EV forecast exclusion

Optional `meter.import_energy_total_kwh` and `meter.export_energy_total_kwh`
entities may now point at true utility-meter cumulative import/export sensors.
When each configured entity is live and numeric it is preferred; otherwise that
direction falls back to the existing inverter/battery-side entity under `tariff`.

`load.ev_included_in_battery_load` declares whether the configured battery
house-load cumulative sensor includes EV charging. Default is `false`.

EV charging is never forecast:
- with the default `false`, the battery house-load already excludes EV;
- with `true`, baseline learning, recent-load correction, and forecast comparison
  omit intervals where `load.ev_charging_entity` was active or cannot be ruled out.

The forecast sensor exposes a `metering` diagnostic attribute showing which
import/export entities and sources are currently in use.
