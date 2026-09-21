#!/usr/bin/env python3
"""Existing natural-first low-calibration scheduling policy (migrates in #116 PR 2).

State, user requests and completion are now owned by calibration_coordinator;
this module does not replace Controller init/sample/state/attrs. The production
overlay planner still imports its strategy through the existing runtime chain
until the next independently releasable refactor.
"""
import asyncio
from datetime import timedelta

import active_runtime as runtime

core = runtime.core
_legacy_runtime = runtime.runtime.legacy_runtime
# The planner executes this underlying module, not the forwarding wrapper.
_calibration_policy_runtime = _legacy_runtime.runtime
_NATURAL_MARGIN_SOC = 0.5


def _natural_first_calibration_discharge(controller, plan, window, capacity, reserve, max_discharge, forecast):
    """Reach reserve as late as practical, forcing only forecast residual depletion."""
    if controller.calibration_state() != 'awaiting_deep_low':
        return None

    dwell_minutes = max(0, int(controller.c.get('reserve_dwell_minutes', 30)))
    safety_minutes = max(0, int(controller.c.get('charge_safety_margin_minutes', 10)))
    max_charge = max(0.001, float(getattr(controller, '_calibration_max_charge_w', 0.0)) / 1000.0)
    recharge_kwh = float(capacity) * max(0.0, 100.0 - float(reserve)) / 100.0
    recharge_hours = recharge_kwh / max_charge

    # The low point is deliberately as late as possible: only dwell, a full-rate
    # recharge and the configured safety margin are reserved before cheap rate ends.
    latest_safe_reserve = window['end'] - timedelta(
        hours=recharge_hours, minutes=dwell_minutes + safety_minutes,
    )
    now = controller.now()
    feasible = latest_safe_reserve >= max(now, window['start'])

    projected_without_pause = controller.soc_at(forecast, latest_safe_reserve, 'forecast_no_slots')
    coverage_complete = projected_without_pause is not None
    if projected_without_pause is None:
        projected_without_pause = core.as_float(forecast.get('attributes', {}).get('overnight_start_soc_no_slots'))
    if projected_without_pause is None:
        projected_without_pause = core.as_float(plan.get('initial_soc'))
    if projected_without_pause is None:
        projected_without_pause = 100.0
    projected_without_pause = core.clamp(float(projected_without_pause), float(reserve), 100.0)

    projected_with_pause = core.as_float(plan.get('initial_soc'))
    if projected_with_pause is None:
        projected_with_pause = projected_without_pause
    projected_with_pause = core.clamp(float(projected_with_pause), float(reserve), 100.0)

    natural_drop_soc = max(0.0, projected_with_pause - projected_without_pause)
    residual_soc = max(0.0, projected_without_pause - float(reserve))
    if residual_soc <= _NATURAL_MARGIN_SOC and coverage_complete:
        residual_soc = 0.0
    residual_kwh = float(capacity) * residual_soc / 100.0
    discharge_hours = residual_kwh / max(0.001, float(max_discharge) / 1000.0)
    discharge_start = (latest_safe_reserve - timedelta(hours=discharge_hours)).replace(second=0, microsecond=0)
    expected_floor = latest_safe_reserve.replace(second=0, microsecond=0)
    expected_complete = latest_safe_reserve + timedelta(
        hours=recharge_hours, minutes=dwell_minutes + safety_minutes,
    )

    if residual_kwh <= 0.0:
        strategy = 'natural_discharge'
        reason = 'forecast_natural_depletion_reaches_reserve'
    elif natural_drop_soc > _NATURAL_MARGIN_SOC and coverage_complete:
        strategy = 'natural_plus_forced'
        reason = 'force_only_forecast_shortfall_after_natural_depletion'
    else:
        strategy = 'forced_discharge'
        reason = 'natural_depletion_unavailable_or_insufficient'

    diagnostics = {
        'state': 'awaiting_deep_low',
        'strategy': strategy,
        'reason': reason,
        'target_soc': round(float(reserve), 1),
        'latest_safe_reserve_at': core.iso(latest_safe_reserve),
        'target_reserve_at': core.iso(latest_safe_reserve),
        'expected_reserve_at': core.iso(expected_floor),
        'projected_soc_with_pause': round(projected_with_pause, 1),
        'projected_soc_without_pause': round(projected_without_pause, 1),
        'predicted_natural_depletion_soc': round(natural_drop_soc, 1),
        'forecast_coverage_complete': bool(coverage_complete),
        'natural_depletion_sufficient': residual_kwh <= 0.0,
        'forced_export_required': residual_kwh > 0.0,
        'residual_forced_discharge_kwh': round(residual_kwh, 3),
        'reserve_dwell_minutes': dwell_minutes,
        'expected_complete_by': core.iso(expected_complete),
        'offpeak_end': core.iso(window['end']),
        'feasible': bool(feasible),
    }

    if not feasible:
        _legacy_runtime._disabled_discharge(controller, plan, window['start'])
        plan['discharge']['kind'] = 'calibration_postponed'
        diagnostics['action'] = 'postponed'
        diagnostics['reason'] = 'insufficient_offpeak_time_for_dwell_recharge_and_margin'
        return diagnostics

    # Preservation must not defeat the natural-depletion-first calibration plan.
    plan['pause'] = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}
    if residual_kwh <= 0.0:
        _legacy_runtime._disabled_discharge(controller, plan, latest_safe_reserve)
        plan['discharge']['kind'] = 'calibration_natural_depletion'
        diagnostics['action'] = 'allow_natural_discharge'
    else:
        plan['discharge'] = {
            'start': core.iso(discharge_start),
            'end': core.iso(expected_floor),
            'rate_w': int(round(max_discharge)),
            'target_soc': int(round(reserve)),
            'planned_kwh': round(residual_kwh, 3),
            'kind': 'calibration_to_reserve',
        }
        diagnostics['action'] = 'discharge_to_reserve'

    core.LOG.info(
        'Low calibration strategy: strategy=%s target=%.1f%% latest_safe=%s with_pause=%.1f%% without_pause=%.1f%% natural_drop=%.1f%% residual_forced=%.3fkWh coverage=%s',
        strategy, float(reserve), latest_safe_reserve.strftime('%Y-%m-%d %H:%M'),
        projected_with_pause, projected_without_pause, natural_drop_soc, residual_kwh,
        'complete' if coverage_complete else 'fallback',
    )
    return diagnostics


# Keep the existing planning registration for PR 1; PR 2 replaces the
# separate discharge/recharge registrations with a single explicit call.
_calibration_policy_runtime._minimise_calibration_discharge = _natural_first_calibration_discharge

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
