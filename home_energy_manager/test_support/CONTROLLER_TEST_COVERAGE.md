# Controller behavioural baseline and hard-rule coverage

This is **developer-only** test documentation for #115. It is not an operating guide
and does not change runtime policy. The baseline is the controller code in
`0.1.115` (the parent commit of PR #118).

## How to run

From `home_energy_manager/` with the GitHub Actions Python 3.13 environment:

```sh
python -m compileall -q .
pytest -q test_packaged_controller_integration.py
pytest -q
```

`test_packaged_controller_integration.py` copies the production Dockerfile's
**ordered** controller COPY targets to a temporary image directory, copies the
real shared transport infrastructure and launches a fresh Python interpreter
for each scenario. The interpreter imports the final `app.py`, i.e. the file
created by the last Controller COPY in the production Dockerfile. It uses the
real composed Controller, planner, event overlays, calibration samplers,
persistence DB and plan applier. No code is imported from the source controller
directory in the scenario interpreter.

Faked external boundaries: Home Assistant state reads, tariff/forecast and
Home Forecaster model entity payloads, MQTT transport (disabled deliberately),
a fixed Europe/London clock, and the inverter HA write/readback endpoints. The
slow, real retrying `ensure()` write boundary is replaced *under* the
production charge-target suppression layer; desired writes flow through
`Controller.apply()` and are captured by FakeHA. This is **not** a test of
network calls, real GivTCP hardware responses, MQTT broker delivery, real
battery-model accuracy, or physical charge completion. Existing retry/readback
tests remain necessary.

The representative synthetic baseline is 21 September 2026, with tariff
23:00–06:00 at 6.99p/kWh, day rate 31.12p/kWh, 4% reserve, 20% safety buffer,
13.79kWh battery capacity, 6kW charge/discharge limits and 250W house demand.
The no-slots trajectories represent either a natural arrival at 4% or a
shortfall arrival near 18%. Axle is modelled as an 18:30–19:30 export event.
The tests assert logical planning and *applied* HH:MM inverter fields separately:
the current applier writes minute-resolution times even when a plan timestamp
contains seconds. These are deterministic **modelled** fixtures, not evidence
that the actual home followed those exact trajectories.

The effective-planner negative regression deliberately registers the working
function on the wrong forwarding module while disabling it on the module
executed by the real planner. In that case the executable scenario has no
calibration-forecast recharge. A positive scenario verifies that the real
production entrypoint does register it on the effective module. Both catch the
class of #92/#114 regression without depending on a string match.

## Hard-rule traceability

Numbers refer to `components/controller/AGENTS.md`. "Source guard" means a
historical source-contract test, **not** equivalent to an executed behaviour
assertion. Subsequent refactors should replace those guards with additional
packaged behavioural scenarios before deleting the legacy implementation.

| Rules | Coverage in this baseline | Remaining boundary |
| --- | --- | --- |
| 1–2: export_generated timing | `components/controller/test_invariants.py` (source guard); `components/controller/test_export_generated_solar_pause.py` (forecast solar pause checks) | Add packaged `export_generated` timing/output cases in the export-mode refactor. |
| 3–4: no export during confirmed EV charge; exclude apparent EV export | `test_packaged_controller_integration.py::test_confirmed_ev_smart_charging_uses_no_forced_export`; `test_active_axle_overlays_low_calibration_and_confirmed_ev_blocks_export`; `components/controller/test_invariants.py` (source guard on export accounting) | EV export-meter provenance is not reproduced with a physical upstream CT in this fixture. |
| 5: invariant discharge target | `test_packaged_controller_integration.py::assert_no_unauthorised_target_write` used across active modes, calibration and Axle; `test_axle_invariants.py::test_active_event_uses_max_discharge_and_reserve_target` | Inverter firmware enforcement is outside pytest. |
| 6: genuine credited export | `test_export_meter_payload_provenance.py::test_export_generated_uses_current_forecast_metering`; `components/controller/test_invariants.py` (source accounting guards) | Real utility-meter provenance remains an external integration. |
| 6a: user-owned charge-target entity | `test_packaged_controller_integration.py::assert_no_unauthorised_target_write`; `components/controller/test_duration_only_charging.py`; `test_controller_plan_apply_boundary.py` | Real Home Assistant readback tested separately. |
| 7: normal overnight protection and solar headroom | `test_packaged_controller_integration.py::test_normal_minimise_export_preservation_and_charge_have_no_overlap`; `test_incomplete_bridge_forecast_fails_safe_to_full_solar_headroom`; `components/controller/test_transfer_pause_invariants.py`; `components/controller/test_minimise_export_headroom.py` | Real future PV/load uncertainty cannot be captured by a deterministic fixture. |
| 8: no overnight pre-export of forecast solar | `components/controller/test_invariants.py` (source guard); `test_packaged_controller_integration.py::test_normal_minimise_export_preservation_and_charge_have_no_overlap` (minimise-export case) | An explicit packaged export-generated overnight case remains desirable. |
| 9–11: planned EV advisory, confirmed EV overlay and useful charge | `components/controller/test_invariants.py` (source guards); `test_packaged_controller_integration.py::test_confirmed_ev_smart_charging_uses_no_forced_export`; `test_active_axle_overlays_low_calibration_and_confirmed_ev_blocks_export` | No simulated real-time EV metering/supplier settlement API. |
| 11a: deep/low calibration | `test_packaged_controller_integration.py::test_manual_low_calibration_preprograms_actual_inverter_schedule`; `test_low_request_survives_fresh_controller_instance_and_db`; `test_low_observation_transitions_to_full_recharge_without_reenabling_automatic`; `test_infeasible_calibration_does_not_schedule_forced_export_or_late_recharge`; `test_no_request_and_spontaneous_low_do_not_force_calibration_recharge`; `test_package_dispatch_ignores_shadow_patch_on_forwarding_module`; `test_effective_planner_dispatch_is_required_for_recharge`; `components/controller/test_minimise_export_calibration.py` | Baseline diagnostic `forced_discharge` can describe residual forced energy after previous natural consumption when `plan.initial_soc` is absent; preserve observed baseline, revisit policy only in its own issue. |
| 12–13: safety buffer and avoid peak-rate import | `test_packaged_controller_integration.py::test_normal_minimise_export_preservation_and_charge_have_no_overlap`; `test_incomplete_bridge_forecast_fails_safe_to_full_solar_headroom`; `components/controller/test_invariants.py` (Rule-1 and Axle-last-resort source guards) | Real peak-rate avoidance depends on forecast accuracy and physical SoC. |
| 14: Eco by default | `test_packaged_controller_integration.py::test_normal_minimise_export_preservation_and_charge_have_no_overlap` checks final Eco write. | Inverter may apply its own local modes. |
| 15: Power Down priority and protection | `test_packaged_controller_integration.py::test_power_down_uses_existing_controller_overlay_without_target_write`; `test_controller_overlay_pipeline.py`; `components/controller/test_invariants.py` | Baseline fixture does not reproduce reward settlement/actual exported kWh. |
| 16–18: Axle adapter boundary, passive axle_only, export-only | `test_axle_invariants.py`; `test_packaged_controller_integration.py::test_forecast_only_loads_no_active_controller_and_performs_no_writes`; `test_future_axle_suppresses_forced_calibration_discharge_but_keeps_recharge` | Axle-only temporary settings are covered by existing tests, not the packaged normal-mode harness. |
| 19–20: maximum feasible Axle export, reserve target | `test_axle_invariants.py::test_event_energy_uses_max_discharge_duration_efficiency_and_reserve`; `test_active_event_uses_max_discharge_and_reserve_target`; `test_packaged_controller_integration.py::test_active_axle_overlays_low_calibration_and_confirmed_ev_blocks_export` | Physical maximum is provided as a fixture input. |
| 21–23: Axle mode coverage, forecast preparation, temporary overlay | `test_packaged_controller_integration.py::test_future_axle_suppresses_forced_calibration_discharge_but_keeps_recharge`; `test_axle_lifecycle_replans_same_controller_without_losing_calibration`; `test_expired_or_absent_axle_restores_underlying_calibration_planner`; `test_controller_overlay_pipeline.py`; `test_axle_invariants.py` | The existing snapshot/diff compatibility oracle is removed under #40, not #115. |
| 24: confirmed EV interrupts active Axle export | `test_packaged_controller_integration.py::test_active_axle_overlays_low_calibration_and_confirmed_ev_blocks_export`; `components/controller/test_invariants.py` (source guard) | The explicit replan after an EV half-hour is a follow-on scenario to add when #40 changes overlay logic. |

The `test_packaged_controller_integration.py::test_active_axle_discharge_preserves_existing_inverter_slot_start`
scenario and `test_active_slot_start_immutable.py` cover the separate
active-slot immutability requirement. Existing source guards remain in CI;
their limitations are intentional and must not be mistaken for proof of
end-to-end behaviour.
