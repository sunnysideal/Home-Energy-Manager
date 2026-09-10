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

7. **Regular overnight target**
   - In `export_generated` and `maximise_export`, the regular overnight objective is 100% SOC by the end of the regular overnight cheap window unless an explicitly documented calibration rule applies.
   - Current regular window is normally 23:30–05:30, but timing comes from configured/forecast off-peak data.

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

### Safety and priorities

12. **Safety buffer is protected**
    - Normal optimisation must preserve the configured safety buffer at the next regular off-peak start unless an explicitly documented higher-priority rule applies.

13. **Avoid peak import before maximising export**
    - Priority order:
      1. avoid peak-rate grid import;
      2. preserve required safety/reserve constraints;
      3. maximise genuine surplus export.

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
    - The Axle component normalises the installed HACS integration into `sensor.home_energy_manager_axle`; the controller consumes only that package-owned interface.

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
