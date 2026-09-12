from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = (ROOT / "app.py").read_text(encoding="utf-8") + "\n" + (ROOT / "controller_legacy_core.py").read_text(encoding="utf-8")
MINIMISE_RUNTIME = (ROOT / "minimise_export_runtime.py").read_text(encoding="utf-8")
ACTIVE_RUNTIME = (ROOT / "active_runtime.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

def require(cond, message):
    if not cond:
        raise AssertionError(message)

# The behavioural contract itself.
for required in (
    "Configured export start is a hard lower bound",
    "No overnight pre-export of forecast solar",
    "Car-charging apparent export is not genuine grid export",
    "Discharge target is invariant",
    "Planned Intelligent Go slots are advisory only",
    "Confirmed Intelligent Go overlays the normal plan",
    "Axle overlays all active optimisation modes",
    "Axle is a temporary plan overlay, not a second controller",
):
    require(required in AGENTS, f"Missing AGENTS.md invariant: {required}")

# Core implementation guards.
require("Never pre-export today's forecast solar generation" in APP,
        "Overnight normal export_generated suppression missing")
require("if car_on or prev_car:" in APP,
        "EV charging export exclusion missing")
require("intelligent_charge_reason='export_generated_topup'" in APP,
        "Confirmed Intelligent export-top-up path missing")
require("intelligent_pause_mode='PauseDischarge'" in APP,
        "Confirmed Intelligent PauseDischarge overlay missing")
require("charge_slot_target=intelligent_charge_target" in APP,
        "Temporary Intelligent charge target application missing")
require("es=self.export_start(w)" in APP,
        "Normal export-generated path no longer derives start from configured export_start")

# The obsolete overnight catch-up branch must not return.
for forbidden in (
    "Cheap-rate catch-up uses live SOC",
    "phase':'cheap_rate_catchup",
):
    require(forbidden not in APP,
            f"Forbidden normal overnight export-generated behaviour reintroduced: {forbidden}")

# Source must direct maintainers to the authoritative rules.
require(APP.startswith("# IMPORTANT PROJECT INSTRUCTIONS"),
        "app.py is missing the AGENTS.md instruction header")

require("def export_generated_guardrail_end" in APP,
        "SOC-based export-generated guardrail solver missing")
require("target_end,guardrail_end" in APP or "de=min(target_end,guardrail_end)" in APP,
        "Evening export is not capped by SOC guardrail end")
require("required_depletion_pct=self.planned_discharge_soc_adjustment" in APP,
        "Intelligent export top-up is not based on the same SOC-depletion model")

# Regression: Rule-1 Intelligent charging must use the FORECAST ARRIVAL SOC.
require("offpeak_arrival_soc=float(start_soc)" in APP,
        "Explicit forecaster arrival-SOC semantic missing")
require("offpeak_arrival_soc < buffer_soc-0.5" in APP,
        "Intelligent Rule-1 decision is not based on forecast off-peak arrival SOC")
require("buffer_soc-offpeak_arrival_soc" in APP,
        "Intelligent Rule-1 top-up is not the forecast arrival shortfall")
require("protected_soc_to_offpeak" not in APP,
        "Controller-side duplicate load/PV reverse simulation reintroduced")

# Minimise Export must not rely on arrival SOC alone. If the no-slots battery
# reaches reserve hours before cheap rate, SOC remains clamped while grid import
# accumulates. The runtime policy must size tonight's target from the complete
# cheap-end -> following-cheap load/PV bridge using the established reverse
# energy model. Missing forecast coverage is deliberately fail-safe (100%).
require("self.operation_mode() == 'minimise_export'" in MINIMISE_RUNTIME,
        "Minimise Export peak-protection policy is not mode-scoped")
require("self.power_down_protected_soc(" in MINIMISE_RUNTIME,
        "Minimise Export does not use the load/PV reverse energy model")
require("bridge_start = window['end']" in MINIMISE_RUNTIME,
        "Minimise Export bridge does not start at regular cheap-rate end")
require("next_offpeak_start = _shift_local_day(self, window['start'], 1)" in MINIMISE_RUNTIME,
        "Minimise Export bridge does not extend to the following regular cheap start")
require("max(configured_floor, protected_soc)" in MINIMISE_RUNTIME,
        "Configured minimise-export SOC is not retained as a floor")
require("forecast_coverage_complete" in MINIMISE_RUNTIME,
        "Minimise Export peak-protection coverage is not exposed diagnostically")
require("If forecast coverage is incomplete, the target must fail safe to 100%" in AGENTS,
        "Minimise Export incomplete-forecast fail-safe is not contractual")

# Axle is a higher-priority plan overlay in all active optimisation modes. It
# must not become a second concurrent controller and forecast_only stays write-free.
require("self.operation_mode() in ('minimise_export', 'maximise_export', 'export_generated')" in MINIMISE_RUNTIME,
        "Axle overlay is not scoped to all three active optimisation modes")
require("axle_entity" in MINIMISE_RUNTIME and "sensor.home_energy_manager_axle" in MINIMISE_RUNTIME,
        "Normal-mode Axle overlay does not consume the package-owned Axle sensor")
require("event_type != 'export'" in MINIMISE_RUNTIME,
        "Non-export Axle events are not explicitly rejected")
require("required_kwh = (float(max_discharge) / 1000.0) * duration_h / discharge_eff" in MINIMISE_RUNTIME,
        "Axle event SOC requirement is not based on full-rate event discharge")
require("'kind': 'axle_export'" in MINIMISE_RUNTIME,
        "Active Axle event does not create a distinct forced-discharge plan")
require("'target_soc': int(round(reserve))" in MINIMISE_RUNTIME,
        "Axle active discharge does not preserve configured reserve")
require("_disabled_discharge(controller, plan, now)" in MINIMISE_RUNTIME,
        "Upcoming Axle event does not suppress lower-priority forced discharge")
require("cheap_before_event" in MINIMISE_RUNTIME and "prepare_in_regular_offpeak" in MINIMISE_RUNTIME,
        "Axle preparation does not prefer the regular cheap window")
require("peak_prepare_last_resort" in MINIMISE_RUNTIME and "latest_charge_start" in MINIMISE_RUNTIME,
        "Axle peak charging is not constrained to last-resort preparation")
require("suspended_for_confirmed_ev_smart_charging" in MINIMISE_RUNTIME,
        "Confirmed EV Smart Charging does not pre-empt Axle forced export")
require("restore_snapshot" not in MINIMISE_RUNTIME,
        "Normal-mode Axle overlay must replan, not restore a stale inverter snapshot")
require("`forecast_only` remains write-free" in AGENTS,
        "Forecast-only Axle exclusion is not contractual")

# The next off-peak window itself is supplied by the forecaster/tariff contract.
require("off=s.get('attributes',{}).get('offpeak')" in APP,
        "Controller no longer reads the forecaster tariff-derived off-peak window")
require("w['start']" in APP,
        "Controller no longer plans against the forecast off-peak start")

arrival_soc=4.0
buffer_soc=20.0
required_delta_pct=max(0.0,buffer_soc-arrival_soc)
require(abs(required_delta_pct-16.0)<1e-9,
        "18:35 regression: a 4% forecast arrival should request a 16pp Intelligent top-up")

require("grid_export_meter_source" in APP,
        "Controller does not consume selected meter-source metadata")
require("if meter_source=='true_meter':" in APP,
        "True utility-meter export is not used directly")
require("if str(self.c.get('grid_export_meter_source') or 'battery') == 'true_meter':" in APP,
        "EV-adjusted fallback accounting is not bypassed for true meter export")

require("controller_refresh_timeout_seconds" in APP,
        "Controller refresh timeout config missing")
require("Controller refresh wait timed out" in APP,
        "Controller refresh barrier timeout recovery missing")
require("waiting_for_controller_refresh_since" in APP,
        "Controller refresh wait timestamp missing")

require("MQTTPublisher" in APP,
        "Controller MQTT presentation publisher missing")
require("if not self.mqtt.publish_sensor(status_entity,self.health,a):" in APP,
        "Controller status does not retain REST fallback when MQTT is unavailable")

# Export Generated direct-solar PauseCharge regression guards.
require("replace(second=0,microsecond=0).strftime('%H:%M:%S')" in APP,
        "Inverter schedule times are not normalised to GivTCP minute resolution")
require("def export_generated_solar_window" in APP,
        "Forecast-defined Export Generated solar window missing")
require("export_generated_solar_threshold_w" in APP,
        "Export Generated solar threshold is not slot-resolution independent")
require("self.intervals(s,'forecast_no_slots')" in APP,
        "Export Generated solar pause is not derived from forecast_no_slots")
require("solar_pause_source':'forecast_no_slots'" in APP,
        "Export Generated status does not expose forecast-only solar pause source")
require("if solar_pause_active:" in APP and "intelligent_pause_mode='PauseCharge'" in APP,
        "Confirmed Intelligent no-top-up path does not preserve solar PauseCharge")
require("elif mode=='PauseCharge':" in APP and "pause=p.get('pause') or pause" in APP,
        "Apply path does not preserve the planned solar PauseCharge window")
require("Exception: in `export_generated`" in AGENTS,
        "Intelligent Go solar PauseCharge exception is not documented in AGENTS.md")

# Calibration instrumentation is observation-only. 40% and 20% must be genuine
# downward crossings; the configured floor remains a visit/latch because a
# controller restart while physically at reserve still counts as calibration.
require("previous_soc > threshold and soc <= threshold" in ACTIVE_RUNTIME,
        "40/20 calibration instrumentation is not downward-crossing based")
require("last_below_40_soc_at" in ACTIVE_RUNTIME and "last_below_20_soc_at" in ACTIVE_RUNTIME,
        "40/20 calibration crossing timestamps are not persisted")
require("soc_crossing_40_step_delta_pp" in ACTIVE_RUNTIME or "f'{key_prefix}_step_delta_pp'" in ACTIVE_RUNTIME,
        "SOC crossing step size is not recorded")
require("calibration_previous_soc" in ACTIVE_RUNTIME,
        "Previous SOC observation is not persisted across controller restarts")
require("last_deep_calibration_at', now_iso" in ACTIVE_RUNTIME,
        "Configured low-SOC visit no longer resets the deep calibration interval")
require("low_soc_visit_active" in ACTIVE_RUNTIME,
        "Low-SOC visit latch/hysteresis missing")

print("All controller invariant checks passed.")

# Meter provenance must survive export_generated_inputs -> published plan/log.
require("'export_meter_source':export_info.get('export_meter_source')" in APP,
        "Export Generated plan drops selected export meter source")
require("'export_meter_entity':export_info.get('export_meter_entity')" in APP,
        "Export Generated plan drops selected export meter entity")
