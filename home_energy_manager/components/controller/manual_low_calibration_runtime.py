#!/usr/bin/env python3
"""One-shot user-requested low-calibration overlay and low-calibration strategy.

The package owns the Home Assistant request button. Calibration policy remains in
Controller and all actual planning/writes continue through the existing planner
and plan-applier path. Automatic and requested low calibrations deliberately use
the same natural-depletion-first strategy.
"""
import asyncio
from datetime import timedelta
import threading

import active_runtime as runtime
from common.mqtt_button import publish_command_button

core = runtime.core
_original_init = core.Controller.__init__
_original_sample = core.Controller.sample
_original_calibration_state = core.Controller.calibration_state
_original_calibration_attrs = core.Controller.calibration_attrs
_legacy_runtime = runtime.runtime.legacy_runtime

_PENDING_KEY = 'manual_low_calibration_pending'
_REQUESTED_AT_KEY = 'manual_low_calibration_requested_at'
_COMPLETED_AT_KEY = 'manual_low_calibration_completed_at'
_LAST_RESULT_KEY = 'manual_low_calibration_last_result'
_BUTTON_ENTITY = 'button.home_energy_manager_request_low_calibration'
_NATURAL_MARGIN_SOC = 0.5


def _pending(controller):
    return bool(controller.db.ok and controller.db.get(_PENDING_KEY))


def _latch_request(controller):
    """Consume one MQTT press in the Controller thread and persist it."""
    if not controller.db.ok:
        return
    if _pending(controller):
        core.LOG.info('Manual low calibration button press ignored because one is already pending')
        return
    now_iso = core.iso(controller.now())
    controller.db.set(_PENDING_KEY, True)
    controller.db.set(_REQUESTED_AT_KEY, now_iso)
    controller.db.set(_LAST_RESULT_KEY, 'pending')
    controller.db.set(_COMPLETED_AT_KEY, None)
    core.LOG.info(
        'Manual low calibration requested: button=%s target=%.1f%%; request latched until actual low-SOC completion',
        _BUTTON_ENTITY, float(controller.c.get('deep_cycle_floor_soc', 4)),
    )


def _install_request_button(controller):
    event = threading.Event()
    controller._manual_low_calibration_press = event
    available = publish_command_button(
        controller.mqtt,
        _BUTTON_ENTITY,
        'Request Low Calibration',
        'mdi:battery-arrow-down',
        event.set,
    )
    controller._manual_low_calibration_button_available = available
    if not available:
        core.LOG.warning('Manual low calibration request button unavailable because Controller MQTT is unavailable')


def _init_with_manual_low_button(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    _install_request_button(self)


def _manual_calibration_state(self):
    state = _original_calibration_state(self)
    if state == 'disabled':
        return state
    if state == 'deep_recharge':
        return state
    if _pending(self):
        # Reuse the exact automatic deep-calibration planning state rather than
        # introducing a manual discharge path.
        return 'awaiting_deep_low'
    return state


def _request_status(controller):
    if _pending(controller):
        if not controller.c.get('calibration_enabled', True):
            return 'blocked', 'calibration_disabled'
        state = controller.calibration_state()
        if state == 'awaiting_deep_low':
            return 'planned', 'user_requested'
        return 'waiting', f'calibration_state_{state}'
    completed = controller.db.get(_COMPLETED_AT_KEY) if controller.db.ok else None
    return ('completed', 'target_observed') if completed else ('not_requested', 'none')


def _calibration_attrs_with_manual_request(self):
    attrs = _original_calibration_attrs(self)
    status, reason = _request_status(self)
    attrs.update({
        'low_calibration_requested': _pending(self),
        'low_calibration_request_button': _BUTTON_ENTITY,
        'low_calibration_request_button_available': bool(getattr(self, '_manual_low_calibration_button_available', False)),
        'low_calibration_requested_at': self.db.get(_REQUESTED_AT_KEY) if self.db.ok else None,
        'low_calibration_request_state': status,
        'low_calibration_request_reason': reason,
        'low_calibration_request_target_soc': float(self.c.get('deep_cycle_floor_soc', 4)),
        'low_calibration_request_completed_at': self.db.get(_COMPLETED_AT_KEY) if self.db.ok else None,
        'low_calibration_request_last_result': self.db.get(_LAST_RESULT_KEY) if self.db.ok else None,
    })
    return attrs


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


async def _sample_with_manual_low_calibration(self):
    # MQTT callbacks run on paho's network thread. They only set a thread-safe
    # Event; all DB/policy work happens here on the Controller thread.
    event = getattr(self, '_manual_low_calibration_press', None)
    if event is not None and event.is_set():
        event.clear()
        _latch_request(self)

    was_pending = _pending(self)
    before_deep = self.db.get('last_deep_calibration_at') if self.db.ok else None
    await _original_sample(self)
    if not was_pending or not self.db.ok:
        return

    entity = self.c.get('battery_soc_entity', '')
    st = await self.ha.state(entity) if entity else None
    soc = core.as_float(st.get('state')) if st else None
    floor = float(self.c.get('deep_cycle_floor_soc', 4))
    after_deep = self.db.get('last_deep_calibration_at')
    if soc is None or soc > floor:
        return

    # The existing sampler records the same low-end completion timestamp used
    # by automatic calibration. Clear only after that real observation; merely
    # requesting or starting discharge never resets/clears the request.
    completed_at = after_deep or core.iso(self.now())
    self.db.set(_PENDING_KEY, False)
    self.db.set(_COMPLETED_AT_KEY, completed_at)
    self.db.set(_LAST_RESULT_KEY, 'completed')
    core.LOG.info(
        'Manual low calibration completed: soc=%.1f%% floor=%.1f%% last_deep_before=%s last_deep_after=%s; request cleared and normal deep-cycle timer reset',
        soc, floor, before_deep or 'unknown', after_deep or completed_at,
    )


# Patch the legacy policy function used by the active overlay pipeline. This is
# policy only: forecast physics still comes from the Home Forecaster entity/API.
_legacy_runtime._minimise_calibration_discharge = _natural_first_calibration_discharge
core.Controller.__init__ = _init_with_manual_low_button
core.Controller.calibration_state = _manual_calibration_state
core.Controller.calibration_attrs = _calibration_attrs_with_manual_request
core.Controller.sample = _sample_with_manual_low_calibration

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
