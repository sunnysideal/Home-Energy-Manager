# Home Energy Controller — Authoritative Instructions

This file is the authoritative behavioural contract for this project.

Any person or AI modifying this project MUST read this file before changing controller behaviour.

## Change-control rules

- Do not violate, reinterpret, weaken, bypass, or silently override any hard rule in this file.
- Optimisation logic must operate within these constraints.
- If a requested change conflicts with a hard rule, stop and explicitly identify the conflict.
- A hard rule may only be changed when the user explicitly asks to change that rule.
- Do not infer permission to change a hard rule from a higher-level objective.
- Before releasing a new version, run tests that exercise the hard rules.
- If a new feature needs an exception, document that exception here explicitly before changing code.

## Hard invariants

### Export generated

1. **Configured export start is a hard lower bound**
   - Normal `export_generated` forced export may not begin before the configured export start time.
   - Default export start: `20:00`.
   - No optimisation, catch-up mechanism, cheap-rate period, Intelligent Go slot, or forecast condition may move normal export earlier.

2. **Normal export must stop by regular off-peak start**
   - Normal `export_generated` forced export must end no later than the next regular overnight off-peak start.

3. **Confirmed Intelligent Go never permits forced export**
   - During a confirmed Intelligent Go settlement half-hour, normal forced export is prohibited.
   - If a normal forced export would overlap that half-hour, it must be suspended or truncated.

4. **Car-charging apparent export is not genuine grid export**
   - The EV charger is upstream of the house-side metering point used by the controller.
   - Apparent house export while the car is charging may simply be energy flowing into the car.
   - Such energy must not count toward the `export_generated` target.
   - Transition intervals touching an active car-charging state must be treated conservatively and excluded.

5. **Discharge target is invariant**
   - The controller must never change the discharge target away from the configured reserve.
   - Export/discharge quantity must be controlled by duration only.

6. **Export target uses genuine credited export**
   - `export_generated` is matched against genuine credited grid export, not battery discharge energy and not house-side apparent export during EV charging.
   - When a true utility-meter export entity is configured and available, its measured export is authoritative and must be used directly.
   - EV-adjusted house-side export accounting is a fallback only when true utility-meter export is unavailable.

### Overnight charging

6a. **Charge target is invariant**
   - The controller must never write or alter the inverter charge-target SOC entity.
   - The inverter charge target is user-owned configuration and remains whatever value the user has set outside Home Energy Manager.
   - Charge quantity must be controlled by charge-slot duration and charge rate only.
   - Logical SOC objectives such as minimise-export protection, 100% regular charging, calibration recharge, EV Smart Charging top-up, Power Down preparation, and Axle preparation may still be calculated by the planner, but they must be translated into duration/rate rather than written to the inverter as a target SOC.
   - This invariant applies to all controller modes that can write inverter settings, including `axle_only`.

7. **Regular overnight target**
   - In `export_generated` and `maximise_export`, the regular overnight objective is 100% SOC by the end of the regular overnight cheap window unless an explicitly documented calibration rule applies.
   - Current regular window is normally 23:30–05:30, but timing comes from configured/forecast off-peak data.
   - In `minimise_export`, the configured `minimise_export_min_soc` is only a floor. The regular overnight target is the highest cheap-rate SOC that still leaves enough forecast battery headroom to absorb avoidable PV surplus before the following regular off-peak start, subject first to avoiding peak-rate import and preserving the safety buffer.
   - The final `minimise_export` objective is therefore the maximum of: the configured minimum floor; the SOC required to supply the forecast peak-rate bridge and still arrive with the safety buffer; and the solar-headroom target derived from the Home Forecaster no-slots load/PV curve.
   - Solar headroom must be evaluated from the end of the regular cheap window to the following regular off-peak start. Forecast load before later solar generation is allowed to create headroom naturally, so low-solar/high-load days should tend toward a 100% cheap-rate target while sunnier surplus days may deliberately target less than 100%.
   - Export that cannot be absorbed because forecast PV exceeds the battery's hardware charge-power limit is not avoidable by lowering overnight SOC and must not create extra headroom demand.
   - The solar-headroom calculation may conservatively assume unity battery efficiency; this can leave slightly more empty capacity than strictly necessary but must never justify a higher target than the forecast can safely absorb.
   - The peak-import calculation must use the forecaster's no-slots load/PV curve and must account for energy demand hidden after forecast SOC reaches inverter reserve; arrival SOC alone is insufficient because it is clamped at reserve.
   - If the no-slots forecast does not fully cover the required bridge, or required battery inputs are unavailable, the regular target must fail safe to 100% rather than risk deliberate peak-rate import or unsupported solar-headroom assumptions.
   - In `minimise_export`, do not schedule a regular overnight preservation pause: allow Eco/self-consumption to supply household load during the cheap window until an actual charging slot begins. This does not override higher-priority Axle, calibration, or confirmed EV Smart Charging controls.
   - Size and time the regular charge from the forecast SOC at its proposed start, including household load/PV before charging. When already in the cheap window, anchor that projection to live SOC; update on subsequent planning cycles. The charge must still finish before the regular cheap window ends, accounting for the configured charging safety margin.
   - When projected SOC at cheap-window end already meets the overnight target without forced charging, schedule no regular charge. If forecast coverage is missing, prefer charging promptly over assuming the battery will retain SOC without a pause.
   - Once an ordinary `minimise_export` charge slot has actually started in the regular cheap window, routine replanning must leave its observed start, end and charge rate unchanged through its scheduled end; it may continue updating the target and diagnostic forecast without writing these three active inverter settings. Forecast household consumption is included before the slot but must not be deducted again as if the battery were discharging while forced charging is active. Existing calibration, Axle and confirmed EV Smart Charging interventions and explicit safety actions may override the normal slot lock; other operating modes are unchanged.

8. **No overnight pre-export of forecast solar**
   - The regular overnight cheap period must not be used to pre-export the current day's forecast solar generation.
   - Overnight cheap energy is for charging/preservation, not manufacturing the day's export target.

9. **Planned Intelligent Go slots are advisory only**
   - Planned Intelligent dispatches must never be assumed in advance when deciding whether the regular overnight charge target is achievable.
   - Only a currently confirmed Intelligent settlement half-hour may be acted upon as cheap.

10. **Confirmed Intelligent Go overlays the normal plan**
    - A confirmed Intelligent slot must not silently erase or invalidate the normal future overnight charge plan.
    - If no extra battery charge is useful, preserve the normal charge schedule and normally use `PauseDischarge` for the confirmed half-hour.
    - Exception: in `export_generated`, while the confirmed half-hour overlaps the forecast-defined solar PauseCharge window, preserve `PauseCharge` so forecast PV is preferentially exported directly rather than stored and re-exported with battery losses.
    - If a genuine cheap-charge need exists, a confirmed Intelligent slot may temporarily use charge slot 1 and disable `PauseCharge`; the normal plan must be restored/replanned afterwards.

11. **Confirmed Intelligent charging may be used when genuinely beneficial**
    - A confirmed Intelligent slot may charge the battery when needed to:
      - avoid later peak-rate import;
      - protect arrival SOC at the next regular off-peak period;
      - prepare for a Power Down event;
      - make the day's genuine `export_generated` target achievable while preserving the safety buffer;
      - satisfy an otherwise unreachable overnight target;
      - satisfy an explicitly documented calibration requirement.
    - The controller should charge only the useful amount, subject to battery headroom and available slot duration.
    - For ordinary Rule-1 protection, a confirmed Intelligent slot outside the regular cheap window should use the forecaster's `overnight_start_soc_no_slots` at the tariff-derived next off-peak start. If that forecast arrival SOC is below the safety buffer, charge by the SOC shortfall; do not duplicate the forecaster's load/PV simulation in the controller.

### Calibration

11a. **Minimise Export deep calibration reaches reserve as late as practical while preserving same-window recharge**
    - `calibration_enabled` controls automatic periodic calibration scheduling only. An explicit persisted user-requested low/deep calibration must still enter the normal `awaiting_deep_low` path when automatic calibration is disabled, and any `deep_recharge` already required by that requested cycle must be allowed to complete. Completing the manual cycle must not re-enable automatic calibration.
    - Any genuine observed battery SOC at or below the configured `deep_cycle_floor_soc` satisfies the low-end calibration requirement and resets the deep-cycle interval, regardless of whether the low SOC was reached naturally, through another operating mode, or through a controller-requested calibration.
    - A spontaneous/natural low-SOC observation must not by itself enter the `deep_recharge` state or force a calibration recharge. The same-window recharge sequence applies only when the controller is already performing an `awaiting_deep_low` calibration cycle.
    - In `minimise_export`, the reserve objective must be scheduled as late as practical within the regular off-peak window while still leaving enough time for the configured reserve dwell, recharge from reserve to 100% at the available hardware charge rate, and the configured charge safety margin before off-peak ends.
    - The configured default `reserve_dwell_minutes` is 15 minutes.
    - The controller must not deliberately reach reserve materially earlier than that latest safe reserve point merely because an earlier forced discharge is possible.
    - When a deep calibration will become due before the following regular off-peak window, the preceding cheap window is a calibration-preparation window. `PauseDischarge` must be disabled so Eco/self-consumption can use battery energy for genuine house load instead of preserving energy that would later need to be exported unpaid.
    - Calibration preparation must not weaken Rule 7 or Rule 13: the normal minimise-export overnight charge target remains active and must still be raised when required to avoid forecast peak-rate import and preserve the safety buffer. A required scheduled charge may therefore occur during the same cheap window while Eco discharge is otherwise allowed.
    - For an active `awaiting_deep_low` calibration, the planner must evaluate the Home Forecaster no-slots SOC at the latest safe reserve point with preservation removed. Natural house consumption and PV effects up to that point are the preferred depletion path.
    - If natural depletion is forecast to reach the configured reserve by the latest safe reserve point, no forced export is scheduled; `PauseBoth`/`PauseDischarge` must not prevent the natural depletion required for calibration.
    - If natural depletion is forecast to get only partway to reserve, forced discharge/export must be limited to the residual SOC/energy shortfall after that forecast natural depletion. Full forced discharge is only the fallback when natural depletion contributes nothing useful or forecast coverage is unavailable.
    - The controller must calculate any residual forced-discharge start backwards from the latest safe reserve objective using the available discharge rate and expected SOC. Battery output first supplies house load and only the excess is exported.
    - While still `awaiting_deep_low`, the controller must also pre-program the expected future calibration recharge slot from `latest_safe_reserve_at + reserve_dwell_minutes` to the calculated completion time, at a rate up to hardware maximum sufficient to reach the logical 100% objective within the cheap window.
    - That expected recharge slot is a forecast-based controller plan and does not wait for an actual low-SOC observation before being written. Before the slot starts, each normal controller replan may move or resize the future recharge slot when live SOC or the no-slots forecast changes.
    - Replanning must reduce or remove future forced discharge when updated SOC/forecast shows more natural depletion than previously expected, and must correspondingly move the future recharge slot. Existing active-slot start immutability still applies once a discharge or charge slot has actually started.
    - While an `awaiting_deep_low` calibration is using natural depletion or a residual forced-discharge slot, preservation pauses must not block that depletion; Eco/self-consumption remains available outside any forced-discharge slot.
    - Actual low-SOC observation remains the calibration-completion evidence and resets the low-end interval, but it is not a prerequisite for programming or beginning the already forecast recharge slot. If forecast depletion is wrong, subsequent replans must correct the future slot while it remains movable.
    - The calibration recharge logical objective is 100% and may use any charge rate up to the hardware maximum required to complete within the same regular off-peak window; the normal preferred/max C-rate planning limits must not prevent completion of a calibration recharge.
    - If there is insufficient regular off-peak time for the reserve dwell, recharge to 100%, and configured charge safety margin, postpone the deep calibration rather than extend the recharge into peak-rate time.

### Safety and priorities

12. **Safety buffer is protected**
   - Normal optimisation must preserve the configured safety buffer at the next regular off-peak start unless an explicitly documented higher-priority rule applies.

13. **Avoid peak import before maximising export**
   - Priority order for normal optimisation:
      1. avoid peak-rate grid import;
      2. preserve required safety/reserve constraints;
      3. maximise genuine surplus export.
   - A qualifying paid Axle Export event is an explicitly documented exception under the Axle rules below. Peak-rate battery charging is permitted only when it is the last available way to meet the Axle event energy requirement; cheap-rate opportunities must be used first and only the remaining shortfall may be charged at peak rate.

14. **Eco mode**
   - Eco/self-consumption remains the normal battery mode outside explicitly controlled charge/discharge/pause periods.

### Power Down

15. **Power Down is a separate higher-priority paid export event**
   - Power Down may override normal export timing where specifically implemented.
   - Power Down behaviour must still preserve its own documented battery-protection rules.
   - Planned Intelligent dispatches do not block Power Down; a confirmed Intelligent slot may pre-empt forced export while active.

### Axle

16. **Axle event data comes from the HACS Axle integration**
   - Home Energy Manager must not authenticate with or call Axle directly.
   - The Axle component normalises the installed HACS Axle integration into `sensor.home_energy_manager_axle`; the controller consumes only that package-owned interface.

17. **`axle_only` is passive outside necessary Axle intervention**
   - The normal optimisation planner is not loaded in `axle_only` mode.
   - Outside preparation for a qualifying Export event and the event itself, Home Energy Manager must not modify inverter/battery settings.
   - Any settings temporarily changed for Axle must be restored after the event, cancellation, or controller shutdown.

18. **Only Axle Export events cause battery action**
   - Import events may be reported but do not cause charging, discharging, or other inverter writes in the initial implementation.
   - Missing/invalid HACS-derived event data must never invent an Axle event.

19. **Axle preparation targets full-rate battery discharge for the whole event**
   - Required event-start stored energy is based on configured reserve, event duration, hardware maximum discharge rate, battery capacity and discharge efficiency.
   - House load must not be added on top of hardware maximum battery discharge: it consumes part of that battery output and therefore reduces grid export rather than increasing possible inverter output.
   - If a full event at maximum discharge is physically impossible, target 100% SOC and report that limitation.

20. **Axle active-event discharge preserves reserve**
   - During a qualifying Axle Export event, forced discharge uses the hardware maximum discharge rate and the configured battery reserve as the discharge target.
   - The controller must never lower the reserve target to increase Axle export.

21. **Axle overlays all active optimisation modes**
   - Qualifying Axle Export events overlay `minimise_export`, `maximise_export`, and `export_generated`.
   - `forecast_only` remains write-free and is never given an Axle battery-control overlay.
   - `axle_only` remains the dedicated passive-outside-Axle mode described above.

22. **Normal-mode Axle preparation uses the forecast and preserves the underlying mode**
   - Preparation must use forecasted battery state/load/PV rather than current SOC alone when deciding whether additional stored energy is required at event start.
   - A regular off-peak opportunity before the event must be used before any peak-rate top-up.
   - Normal forced discharge that would jeopardise the Axle event requirement must be suppressed while the event is being protected.
   - If forecast coverage required to size preparation is incomplete, preparation must fail safe toward additional stored energy rather than assume the event can be met.

23. **Axle is a temporary plan overlay, not a second controller**
   - Normal modes must not launch or run `axle_only.py` concurrently.
   - Axle preparation and active-event controls are applied by the normal planner as a temporary higher-priority overlay.
   - When an event ends or is cancelled, the controller must immediately return to a fresh plan from the selected underlying normal mode; it must not restore a stale snapshot of inverter settings.

24. **Confirmed EV Smart Charging pre-empts Axle export**
   - A currently confirmed EV Smart Charging settlement half-hour remains a no-forced-export period, including during an Axle event.
   - Axle forced discharge must resume on the next fresh plan while the Axle event remains active after the confirmed charging half-hour ends.

## Release checklist

Before producing a release ZIP:

- Read this file.
- Verify every changed planner branch against these hard rules.
- Run `test_invariants.py`.
- Run Python syntax validation.
- Parse `config.yaml`.
- Check that version strings match.
- Summarise any behaviour change that affects a hard rule.
- Do not release if any invariant test fails.

## Rule changes

If the user explicitly changes one of these rules:

1. update this file first;
2. update or add invariant tests;
3. then change implementation code;
4. document the rule change in the release notes.
