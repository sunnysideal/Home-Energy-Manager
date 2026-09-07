# Home Energy Controller v0.1.23

Requires Home Energy Forecaster v0.2.4 or newer.

## Operating modes

`operation_mode` has two supported values.

### maximise_export

This is the original controller strategy:

- overnight charging targets 100%
- `PauseCharge` runs from off-peak end until the next off-peak start
- solar is therefore exported during the day rather than stored
- forced evening export is calculated from the forecaster's no-slots SOC while preserving the configured safety buffer

### minimise_export

Designed to retain solar locally:

- no forced evening export
- normal daytime Eco/self-consumption is left available so solar can charge the battery
- during the cheap overnight window the controller uses `PauseDischarge`, preserving stored battery energy while the house uses cheap grid electricity
- if `overnight_start_soc_no_slots` is at or above `minimise_export_min_soc`, no forced overnight charge is scheduled
- if it is below that threshold, the controller charges only to the threshold (default 25%), not to 100%

Default:
`minimise_export_min_soc: 25`

## SOC calibration maintenance

Calibration is enabled by default and is independent of the daily operating strategy.

Defaults:

- `top_full_every_days: 14`
- `deep_cycle_every_days: 60`
- `deep_cycle_floor_soc: 4`

Natural battery endpoints count. If the battery genuinely reaches 100% from solar or scheduled charging, the top-calibration timer resets.

### Top calibration

If 100% has not been observed for the configured number of days, the next suitable overnight charge is temporarily changed to a full 100% charge. The normal learned taper/top-balance allowance is used.

### Deep calibration

When a deep calibration becomes due:

- in `minimise_export`, the normal 25% overnight minimum top-up is temporarily suppressed, allowing ordinary household use to take the battery down toward the configured floor; overnight `PauseDischarge` remains active so cheap-rate energy is still preferred overnight
- in `maximise_export`, the evening forced-export reserve is temporarily lowered to the configured deep-cycle floor, allowing the scheduled export to reach the low endpoint

Once the actual battery SOC reaches the configured floor, the controller records the low endpoint. The next charging opportunity is then forced to 100%. Reaching 100% completes the deep calibration and resets both calibration state and full-charge timer.

The calibration state is exposed on `sensor.home_energy_controller`:

- `normal`
- `top_due`
- `awaiting_deep_low`
- `deep_recharge`
- `disabled`

with timestamps for the last observed full SOC, completed deep calibration, and any pending low endpoint.

Calibration timestamps are persisted in the existing SQLite database.

## Pause slot

The controller continues to own:

- battery pause mode/start/end
- charge slot 1
- discharge slot 1
- Eco ON
- charge/discharge schedule enables ON

Slot 2 remains untouched.

All Home Assistant entity IDs remain configurable.


## Configuration UI

v0.1.4 adds user-facing names and descriptions for every Home Assistant add-on option.

Entity fields are visually prefixed by purpose:

- Forecast
- Battery state
- Battery limits
- Battery control
- Charge slot 1
- Discharge slot 1
- Pause slot

Controller behaviour settings are similarly labelled under Operation, Calibration, Charging, Forecast safety, Write control, Reporting and Learning.

The internal option keys have not changed, so existing saved add-on options remain compatible.


## v0.1.5: Match solar generation + reserve-aware full discharge

A third operation mode is available: `export_generated` (user-facing intent: **Match solar generation**).

It uses the forecaster's existing `today.total.pv_kwh` as the expected final solar generation for the local day and `today.actual.export_kwh` as actual grid export so far.

Before the cheap period, the controller attempts to make up the remaining export target while retaining the normal safety buffer. If some target remains when the cheap period begins, the controller uses the cheap period for catch-up.

When the remaining target is at least all energy available above inverter reserve, the discharge slot is intentionally left active for the cheap window with its target set to the inverter reserve. The controller does **not** predict an exact reserve-reached time: the inverter/BMS stops battery discharge and idles at reserve.

Deep calibration uses the same principle. A deep-calibration discharge is a long discharge-to-reserve window. Once actual SOC at reserve is observed, the controller records the low endpoint and waits `reserve_dwell_minutes` (default 30) before allowing the full recharge to 100%.

In `export_generated` mode, `PauseCharge` is scheduled across one continuous forecast-defined solar window, from the first to the last `forecast_no_slots` interval whose forecast PV exceeds the configured equivalent-power threshold (default 100 W). Instantaneous PV is deliberately ignored so clouds do not make the pause state chatter. Small forecast gaps inside the window remain paused, encouraging direct PV export rather than battery round-tripping. A confirmed Intelligent Go half-hour preserves this solar `PauseCharge` when no battery top-up is required; a genuinely required cheap charge may temporarily override it.


## v0.1.6: Forecaster-driven entity discovery

The controller no longer asks the user to configure the inverter/battery entity IDs a second time.

The only forecast connection setting is:

`home_energy_forecast_entity: sensor.home_energy_forecast`

Home Energy Forecaster v0.2.8+ publishes a `controller_inputs` attribute containing the authoritative entity map. On startup and every subsequent forecast update, the controller refreshes its runtime entity map from that attribute.

The controller still reads live Home Assistant states directly for SOC, battery power, grid power, reserve, capacity, rate limits and write verification. The forecast sensor supplies the IDs, not stale copies of safety-critical values.

If the forecaster is unavailable at controller startup, or an older forecaster does not expose `controller_inputs`, the controller remains degraded and waits rather than attempting writes with incomplete configuration.

The controller status exposes:

- `forecast_entity`
- `controller_inputs_ready`

This makes discovery state visible without duplicating the complete entity map into the controller configuration UI.


## v0.1.7: no live power dependency

The controller no longer requires instantaneous battery power or grid power.

Charge-curve learning now uses:

- commanded charge rate;
- sampled battery SOC;
- elapsed time;
- discovered battery capacity.

This directly learns effective charge performance per SOC band without a live power sensor.

Accounting now uses energy/state data:

- actual scheduled charge is estimated as positive stored-energy change from SOC × battery capacity while the charge slot is active;
- actual forced export is measured from the cumulative grid-export energy sensor while the discharge slot is active;
- calibration continues to use live SOC.

The forecaster discovery interface v2 supplies the cumulative PV, import and export energy entity IDs. `battery_power_charge_negative` has therefore been removed from controller configuration.


## v0.1.8: convergent forecast refresh

The controller now tracks `forecast_sequence` and only processes newer forecast publications.

When an inverter setting is actually changed and the new value is confirmed by readback, the controller publishes a new monotonic request ID to the forecaster's discovered `refresh_request_entity`.

The forecaster then recalculates promptly and republishes a forecast containing the newly programmed inverter slots. That refreshed forecast acknowledges the request using `refresh_request_id`.

The controller status exposes:
- `source_forecast_sequence`
- `forecast_refresh.awaiting`
- `forecast_refresh.pending_request_id`
- `forecast_refresh.last_ack_sequence`

A refreshed forecast does not create an update loop: if the desired plan already matches the inverter, no confirmed write occurs, so no new refresh request is issued.


## v0.1.9: startup forecast race fix

Startup is now sequence-safe and does not wait for a forecast generated after the controller starts.

- The Home Assistant forecast state-change subscription is established before the controller reads the current forecast state.
- Forecast events arriving while startup is in progress are queued.
- The current `sensor.home_energy_forecast` is processed immediately if present, including after a restart.
- A current valid forecast is not rejected merely because its sequence was already processed by a previous controller process; controller writes converge against live inverter readback.
- Persisted `pending_writes` from the previous process are discarded at startup because the current forecast/plan supersedes them.
- Startup now emits progress logs before potentially slow work, so the add-on log no longer appears idle while initialization is happening.

After startup, normal duplicate/old sequence suppression remains in place.


## v0.1.10: count forecast natural export in export-generated mode

`export_generated` now subtracts the forecaster's
`no_slots_totals.today.export_kwh` from the remaining forced-export target.

This means solar expected to export naturally later in the day — especially
after the battery reaches 100% — is counted before deciding how much battery
energy to force-export in the evening.

The planning equation is now:

`forced export remaining = expected total PV - actual export so far - forecast future natural export`

Controller status adds `export_generated.forecast_natural_export_kwh` for
visibility. If the no-slots export total is unavailable, it safely falls back
to zero rather than refusing to plan.


## v0.1.11: charge from predicted SOC at the actual start time

Overnight charging no longer sizes the charge window from the raw SOC at the
start of the cheap period.

For a planned evening forced discharge the controller first estimates the
additional SOC depletion relative to `forecast_no_slots`. It then searches for
the latest charge start that will still reach the target by the end of the
off-peak window. At every candidate start it reads the no-slots SOC for that
actual time, so expected house consumption between (for example) 23:30 and
02:00 is included.

The planner:
1. accounts for the planned forced discharge before cheap rate;
2. predicts SOC at each candidate charge start;
3. tries the preferred charge rate first;
4. increases the rate only when the preferred rate cannot fit;
5. uses the latest start that still reaches the target plus the configured
   safety margin;
6. reports an error and uses the maximum rate from the earliest permitted start
   if even the maximum rate cannot reach the target.

Controller status now includes:
- `forecast.planned_discharge_soc_adjustment_pct`
- `forecast.estimated_charge_start_soc`
- `forecast.charge_target_reachable`

v0.1.11 also includes the v0.1.10 export-generated fix which subtracts forecast
natural export (`no_slots_totals.today.export_kwh`) from the forced-export
requirement.


## v0.1.12: Octopus Power Down

Adds support for BottlecapDave's Octopus Energy Power Down entities.

Configure:
- `power_down_events_entity`: the `event.*_octoplus_power_down_events` entity.
- `power_down_baseline_entity`: optional `sensor.*_octoplus_power_down_baseline`.
- `power_down_enabled`: enable/disable Power Down planning.

Only joined events are acted on. The controller does not auto-join sessions.

For a joined Power Down before the next cheap period, discharge slot 1 is
reserved for the session and runs at the configured/hardware maximum discharge
rate. The baseline is reported for visibility but does **not** cap export.

The discharge target is calculated dynamically. Working backwards from the next
off-peak start, the controller preserves enough battery energy to:
1. supply forecast house load after the session;
2. account for forecast PV after the session;
3. arrive at the next off-peak period with the normal `safety_buffer_soc`
   remaining;
4. never plan below the inverter reserve.

This implements "maximum economically safe export": use as much battery as
possible during the paid Power Down without deliberately causing peak-price
import afterwards.

If the joined session is after the next off-peak window, the overnight charge
target is raised to 100%. Detailed Power Down discharge protection is then
recalculated after the controller's next-offpeak window advances beyond the
session.

Power Down export naturally counts toward `export_generated` because that mode
uses the actual cumulative grid-export total on later plans.

New status attributes:
- `power_down.start` / `end` / `active`
- `power_down.reward_octopoints_per_kwh`
- `power_down.baseline_total_kwh`
- `power_down.forecast_session_start_soc`
- `power_down.protected_soc`
- `power_down.post_session_required_kwh`
- `power_down.post_session_peak_import_avoidable`
- `power_down.max_safe_battery_output_kwh`
- `power_down.strategy = max_safe_export`


## v0.1.13: discharge target is invariant

The controller never changes the inverter discharge target away from the
configured battery reserve.

For Power Down, `protected_soc` remains an internal planning value only. The
controller converts the safely exportable energy into a discharge duration:

`duration = safe_export_kwh / discharge_rate_kw`

and caps that duration at the Power Down session end.

The inverter discharge target remains at reserve for Power Down, normal
export-generated discharge, maximise-export discharge and calibration. The
quantity discharged is controlled by the slot start/end times.


## v0.1.14: confirmed Intelligent Go half-hours

Adds current-slot Intelligent Go control using:

`binary_sensor.octopus_slot_actually_charging`

(default; configurable as `intelligent_car_charging_entity`).

Rules:
- Future/planned Intelligent dispatches are never assumed to be cheap.
- The first observed `on` confirms the entire enclosing 30-minute settlement
  period as cheap.
- Confirmation remains valid until the half-hour boundary even if the sensor
  subsequently turns off, and is persisted across controller restarts.
- A new half-hour needs fresh confirmation; if the sensor remains on across the
  boundary the new half-hour is confirmed immediately.
- Confirmed Intelligent slots always suspend forced export. An export already in
  progress is clipped at the start of the confirmed half-hour and is replanned
  from fresh state at the boundary.
- Power Down export is also pre-empted by a confirmed Intelligent slot.
- During a confirmed slot the controller may charge toward the normal mode
  target using only the physically remaining time in that half-hour. The charge
  slot/rate override ends at the boundary.
- `PauseCharge`/`PauseDischarge` is disabled for the confirmed half-hour so it
  cannot block the intended Intelligent behaviour.
- The inverter discharge target remains invariant at the configured reserve.

The controller reacts immediately to state changes of the confirmation sensor
and also watches half-hour boundaries, so export can resume/replan promptly when
a confirmed slot ends.


## v0.1.15: planned Intelligent dispatches defer new normal export

Adds `intelligent_dispatch_entity`, pointing at BottlecapDave's Intelligent
dispatch entity containing `planned_dispatches`.

Planned dispatches remain advisory only:
- they do not count as guaranteed cheap energy;
- they do not change charge planning;
- they do not stop an export that is already active;
- they do not block Power Down export.

They do, however, block the **start of a new normal export** if the proposed
export overlaps the planned dispatch. The controller defers that export and
replans later from fresh SOC/export totals.

Confirmed Intelligent slots still have the stronger rule from v0.1.14:
they immediately pre-empt active export and remain confirmed for the whole
enclosing half-hour.

The controller also reacts immediately when the BottlecapDave dispatch entity
changes, so a newly appearing or disappearing planned dispatch can change the
start decision without waiting for the next normal forecast cycle.


## v0.1.16: separate Power Down baselines and clean Supervisor shutdown

Power Down now has two optional BottlecapDave baseline inputs:

- `power_down_import_baseline_entity`
- `power_down_export_baseline_entity`

Status exposes import, export and net baseline totals:

`net_baseline = import_baseline - export_baseline`

These baselines are informational/reward-estimation inputs. They do not cap the
Power Down export strategy, which remains maximum economically safe export.

The add-on also now handles SIGTERM/SIGINT explicitly. A normal Home Assistant
Supervisor Stop/Restart/Update logs:

- `Shutdown requested by Supervisor (...)`
- `Controller shutdown starting`
- `Controller shutdown complete`

The asyncio event loop exits cleanly, background tasks are cancelled in the
existing `finally` path, the database is committed/closed, and the process exits
normally rather than looking like an application crash.


## v0.1.17: Intelligent Go uses PauseDischarge when no top-up is needed

A confirmed Intelligent Go half-hour no longer charges the house battery merely
because the normal overnight mode target is above current SOC.

If the no-slots forecast already carries the battery to the normal off-peak
start at or above `safety_buffer_soc`, and there is no other genuine energy
requirement, the controller sets:

- pause mode: `PauseDischarge`
- pause start: start of the confirmed settlement half-hour
- pause end: end of the confirmed settlement half-hour
- forced charge slot: inert
- forced export: suspended

This deliberately lets the house/car use cheap grid energy while preventing the
battery from discharging. Because charge is not paused, surplus PV remains free
to charge the battery naturally.

A confirmed Intelligent slot is used for forced battery charging only when one
of these applies:
- forecast SOC at the normal off-peak start is below `safety_buffer_soc`;
- an upcoming joined Power Down requires additional stored energy;
- the guaranteed overnight window cannot reach its required target;
- deep-recharge calibration requires charging.

Status/logging now exposes:
- `intelligent_go.pause_mode`
- `intelligent_go.charge_reason`
- `intelligent_go.intelligent_charge_target_soc`
- `intelligent_go.offpeak_arrival_safety_soc`

The existing rules remain unchanged: confirmed Intelligent slots never export,
planned dispatches only defer the start of new normal export, and discharge
target remains fixed at the inverter reserve.


## v0.1.18: export_generated active-export replanning fix

Fixes a confirmed defect where an export already in progress was replanned using
the remaining target but the new duration was still anchored to the original
slot start. This caused elapsed export to be counted twice and progressively
pulled the discharge end earlier.

Changes:
- active timed export is solved from the current time;
- the written discharge start may remain the original slot start, while the
  recalculated end represents the remaining work from now;
- duration targets forecast incremental GRID export, not raw battery output;
- forecast house load and PV are included in the AC balance;
- no-slots natural export is subtracted from forced export so it is not counted
  twice (it was already subtracted by export_generated target calculation);
- calculated end times round UP to the next inverter-supported whole minute;
- planned discharge SOC adjustment ignores elapsed discharge and only projects
  future forced output from the current forecast state;
- additional export_generated diagnostics/logging expose target, actual export,
  natural future export, remaining target, solve-from time, forecast incremental
  export and whether the target fits in the available window.

The discharge target invariant remains unchanged: discharge target is always the
configured battery reserve; export quantity is controlled only by slot duration.


## v0.1.19: latest-forecast control flow

Replaces the FIFO forecast-snapshot backlog with a latest-state control model.

Behaviour:
- `forecast_trigger=scheduled` (and startup/legacy non-controller forecasts) may
  wake normal control.
- `forecast_trigger=controller_refresh` and
  `controller_refresh_retry` are display/update forecasts only and are ignored
  as normal control inputs.
- after controller writes, the controller still requests one immediate forecast
  so Home Assistant's published forecast reflects the new inverter settings;
  that immediate forecast does not feed back into another control pass.
- the next scheduled forecast is the next normal control opportunity.
- forecast events are wake-up signals only; immediately before control the
  controller reads the current `sensor.home_energy_forecast` state from HA.
- pending wake-ups are coalesced to one latest wake-up rather than storing every
  historical forecast snapshot.
- if a scheduled wake-up is superseded by a controller-refresh publication
  before it is handled, it is discarded and control waits for the next scheduled
  forecast.
- Intelligent Go confirmation/dispatch changes remain force wake-ups and are
  coalesced without losing their force semantics.
- logs now include the forecast sequence, trigger and generation timestamp used
  for every control pass.

The refresh-request entity still receives a monotonically increasing value
because the forecaster needs a state change to trigger its immediate refresh;
the controller no longer uses request IDs as an acknowledgement/correlation
handshake.


## v0.1.20: post-write refresh barrier + timing diagnostics

Control transaction barrier:
- whenever a control pass confirms one or more inverter writes, the controller
  marks itself as waiting for the controller-triggered forecast before publishing
  the refresh request.
- scheduled forecasts that arrive while this barrier is active are ignored,
  because they may have been generated before the final inverter write completed.
- publication of `controller_refresh` or `controller_refresh_retry` clears the
  barrier; that forecast remains display-only and is never a normal control input.
- the next scheduled forecast after the barrier clears is the next normal
  controller input.
- forced Intelligent Go/dispatch wake-ups remain able to re-evaluate immediately.

Timing diagnostics:
- every process logs elapsed time for learning, planning, change calculation,
  applying settings, refresh request, publishing, and the total.
- every inverter field that actually needs changing logs the old/new value,
  HA write duration, confirmation-read duration, retry sleeps, total field time,
  and success/failure.
- apply logs total elapsed time and number of confirmed writes.

These logs are intended to identify whether long control passes are caused by HA
service calls, inverter readback latency, or the configured retry wait (currently
60 seconds between failed confirmations).


## v0.1.21: rate quantisation diagnostics + approximate confirmation

For charge/discharge rate fields only:
- readback confirmation is now approximate rather than exact.
- default tolerance is the larger of 100 W or 5% of the requested rate.
- SOC targets, times, switches and pause modes retain their previous matching rules.
- two new optional settings are available:
  - `rate_match_tolerance_pct` (default 5)
  - `rate_match_tolerance_w` (default 100)

Additional rate diagnostics log:
- configured battery capacity
- requested watts
- observed watts
- requested implied %C
- observed implied %C
- theoretical watts represented by 1 percentage point of C-rate

This is intended to verify whether GivEnergy/GivTCP is quantising the requested
watt rate onto discrete battery C-rate steps while avoiding minute-long retries
for a readback that is acceptably close to the requested rate.


## v0.1.22: canonical disabled discharge slot

Fixes the off-peak boundary bug where an export-generated plan with no discharge
could become a wrapped slot such as `23:30-20:00`.

Changes:
- `kind=none` is always canonicalised to a zero-duration slot (`start == end`).
- the disabled slot uses the normal evening export anchor, not the temporary
  active-cheap `control_w`, so a valid disabled `20:00-20:00` slot remains stable
  when 23:30 off-peak begins.
- apply-time validation repairs any future invalid `kind=none` slot before it can
  be written to the inverter and emits a warning.
- no-discharge normalisation logs `Discharge disabled: ...` when it has to correct
  a planner-generated slot.
- the Plan log now distinguishes `plan_changed` from the number of actual
  `inverter_writes`.

The discharge target invariant remains unchanged: target SOC is always the
configured reserve and discharge quantity is controlled only by slot duration.


## v0.1.23: Intelligent Go overlay + EV-adjusted export

- House-side export is no longer assumed to be utility-grid export. A persistent
  30-second accounting stream credits export only when the car-charging signal
  is OFF; transition intervals touching ON are conservatively excluded.
- On the first day after enabling this accounting model, export that happened
  before tracking started is not credited.
- `export_generated` uses credited adjusted export. Raw house export and excluded
  car-charging export remain visible as diagnostics.
- The regular overnight cheap period is no longer used to pre-export the current
  day's forecast solar generation.
- Confirmed Intelligent Go now overlays the normal plan. If no extra charge is
  useful, the future overnight charge schedule is preserved and the confirmed
  half-hour uses PauseDischarge.
- A confirmed daytime Intelligent slot may force-charge only the extra stored
  energy needed to make today's export-generated target achievable while still
  preserving the safety buffer. Planned slots remain advisory until confirmed.
- When such a special Intelligent charge is needed, charge slot 1 is temporarily
  used for the confirmed half-hour with the calculated charge target. The next
  control pass restores/replans the normal overnight charge.
- Forced export remains prohibited during confirmed Intelligent half-hours.
- Discharge target remains invariant at the configured reserve; quantity is
  controlled only by duration.

## v0.1.24: authoritative controller rules

- Added `AGENTS.md` as the authoritative behavioural contract for future changes.
- Added explicit hard invariants for export timing, Intelligent Go, EV-adjusted export accounting,
  discharge target, overnight charging, safety buffer, and Power Down.
- Added `test_invariants.py` to mechanically check key invariants before release.
- Added an `app.py` source header requiring future maintainers/AI agents to read `AGENTS.md`
  before changing behaviour.

## v0.1.25: SOC-based export guardrail + Intelligent export preparation

- `export_generated` now limits the evening forced-export **end time** using the
  predicted incremental SOC depletion of the forced discharge. The 20% safety
  buffer is therefore protected even when part of the 6 kW battery output serves
  house load or discharge losses rather than becoming grid export.
- The controller still aims for the full generation-matched export first. If the
  full target would cross the guardrail, the evening export is shortened.
- A confirmed daytime Intelligent Go slot evaluates the same full-target SOC
  requirement. If extra stored energy is needed, it charges only enough to make
  the full generation-matched export reachable while preserving the safety
  buffer at the regular off-peak boundary.
- Planned Intelligent dispatches remain advisory only; energy is not assumed
  until a settlement half-hour is actually confirmed cheap.


## v0.1.27: Forecaster-driven Intelligent arrival protection

- Clarifies that Intelligent Rule-1 charging uses `overnight_start_soc_no_slots`, the forecaster's predicted SOC at the tariff-derived next regular off-peak start.
- If that arrival forecast is below the configured safety buffer, a currently confirmed Intelligent cheap half-hour charges by exactly the forecast SOC shortfall, subject to available time, charge-rate and battery-headroom limits.
- The controller does not re-model intervening ASHP/load/PV demand; that remains the forecaster's responsibility.
- Confirmed Intelligent slots overlapping the regular cheap window do not create a separate charging requirement, and forced export remains suspended during any confirmed Intelligent charging half-hour.


## v0.1.28: true utility-meter export

When Home Energy Forecaster reports that the selected export cumulative entity is
a true utility-meter sensor, `export_generated` uses the actual meter export
directly. The EV-adjusted house-side export counter remains only as the fallback
for inverter/battery-side export metering.


## v0.1.31: Export Generated direct-solar PauseCharge

- Export Generated derives a continuous daytime PauseCharge window from `forecast_no_slots` only.
- The default meaningful-PV threshold is 100 W equivalent (`export_generated_solar_threshold_w`) and is scaled to the forecast slot duration.
- Instantaneous PV power is not used, avoiding cloud-driven pause toggling.
- Confirmed Intelligent Go preserves the solar PauseCharge window when no top-up is needed, but required cheap charging still overrides it.
- Actual/credited export accounting and the generation-matching target are unchanged.
