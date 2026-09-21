#!/usr/bin/env python3
"""User-requested calibration controls and low-calibration strategy.

The package owns the Home Assistant calibration buttons. Calibration policy remains
in Controller and all actual planning/writes continue through the existing planner
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
# offpeak_rollover_runtime forwards reads via __getattr__, but assignments to
# that wrapper do not update the actual minimise_export_core planner globals.
_calibration_policy_runtime = _legacy_runtime.runtime

_PENDING_KEY = 'manual_low_calibration_pending'
_REQUESTED_AT_KEY = 'manual_low_calibration_requested_at'
_COMPLETED_AT_KEY = 'manual_low_calibration_completed_at'
_LAST_RESULT_KEY = 'manual_low_calibration_last_result'
_BUTTON_ENTITY = 'button.home_energy_manager_request_low_calibration'
_CLEAR_BUTTON_ENTITY = 'button.home_energy_manager_clear_calibration_state'
_CLEAR_AT_KEY = 'calibration_state_cleared_at'
_CLEAR_RESULT_KEY = 'calibration_state_clear_result'
_CLEAR_KEYS_KEY = 'calibration_state_cleared_keys'
_NATURAL_MARGIN_SOC = 0.5

# These are transient intent markers only. Historical calibration observations,
# completion timestamps, SOC crossings and learned battery data are deliberately
# excluded. The high-calibration names reserve the persistence contract for #59
# so the same clear action works once that request path is added.
_CLEARABLE_CALIBRATION_STATE = (
    (_PENDING_KEY, False),
    ('manual_high_calibration_pending', False),
    ('high_calibration_requested', False),
    ('manual_high_calibration_requested', False),
    ('calibration_low_reached_at', None),
)


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


def _clear_calibration_state(controller):
    """Clear transient calibration intent without altering calibration history."""
    if not controller.db.ok:
        return
    cleared = []
    low_was_pending = bool(controller.db.get(_PENDING_KEY))
    high_was_pending = any(bool(controller.db.get(key)) for key in (
        'manual_high_calibration_pending', 'high_calibration_requested', 'manual_high_calibration_requested',
    ))
    for key, cleared_value in _CLEARABLE_CALIBRATION_STATE:
        current = controller.db.get(key)
        active = current is not None if key == 'calibration_low_reached_at' else bool(current)
        if not active:
            continue
        controller.db.set(key, cleared_value)
        cleared.append(key)

    if low_was_pending:
        controller.db.set(_LAST_RESULT_KEY, 'cleared')
    if high_was_pending:
        controller.db.set('manual_high_calibration_last_result', 'cleared')

    now_iso = core.iso(controller.now())
    result = 'cleared' if cleared else 'nothing_to_clear'
    controller.db.set(_CLEAR_AT_KEY, now_iso)
    controller.db.set(_CLEAR_RESULT_KEY, result)
    controller.db.set(_CLEAR_KEYS_KEY, cleared)
    core.LOG.info(
        'Calibration state clear requested: result=%s cleared=%s; historical calibration timestamps and learning preserved',
        result, ','.join(cleared) if cleared else 'none',
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


def _install_clear_button(controller):
    event = threading.Event()
    controller._clear_calibration_state_press = event
    available = publish_command_button(
        controller.mqtt,
        _CLEAR_BUTTON_ENTITY,
        'Clear Calibration State',
        'mdi:battery-off-outline',
        event.set,
    )
    controller._clear_calibration_state_button_available = available
    if not available:
        core.LOG.warning('Clear calibration state button unavailable because Controller MQTT is unavailable')


def _init_with_calibration_buttons(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    _install_request_button(self)
    _install_clear_button(self)


def _manual_calibration_state(self):
    state = _original_calibration_state(self)
    if state == 'deep_recharge':
        return state
    # ``calibration_enabled`` governs automatic scheduling only. A low endpoint
    # already reached by a requested cycle still requires its normal recharge,
    # even if the automatic scheduler is disabled.
    if self.db.ok and self.db.get('calibration_low_reached_at'):
        return 'deep_recharge'
    if _pending(self):
        # Reuse the exact automatic deep-calibration planning state rather than
        # introducing a manual discharge path. This deliberately overrides an
        # automatic ``disabled`` state for the one requested cycle only.
        return 'awaiting_deep_low'
    return state


def _request_status(controller):
    if _pending(controller):
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
        'calibration_clear_button': _CLEAR_BUTTON_ENTITY,
        'calibration_clear_button_available': bool(getattr(self, '_clear_calibration_state_button_available', False)),
        'calibration_state_cleared_at': self.db.get(_CLEAR_AT_KEY) if self.db.ok else None,
        'calibration_state_clear_result': self.db.get(_CLEAR_RESULT_KEY) if self.db.ok else None,
        'calibration_state_cleared_keys': self.db.get(_CLEAR_KEYS_KEY, []) if self.db.ok else [],
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
    # MQTT callbacks run on paho's network thread. They only set thread-safe
    # Events; all DB/policy work happens here on the Controller thread.
    request_event = getattr(self, '_manual_low_calibration_press', None)
    if request_event is not None and request_event.is_set():
        request_event.clear()
        _latch_request(self)

    # Process clear after request so an accidental/simultaneous pair of presses
    # resolves to the user's explicit clear action. This changes persistence only;
    # the next normal planner/apply pass converges any stale inverter schedule.
    clear_event = getattr(self, '_clear_calibration_state_press', None)
    if clear_event is not None and clear_event.is_set():
        clear_event.clear()
        _clear_calibration_state(self)

    was_pending = _pending(self)
    before_deep = self.db.get('last_deep_calibration_at') if self.db.ok else None
    await _original_sample(self)
    if not self.db.ok:
        return

    entity = self.c.get('battery_soc_entity', '')
    st = await self.ha.state(entity) if entity else None
    soc = core.as_float(st.get('state')) if st else None
    if soc is None:
        return

    now_iso = core.iso(self.now())
    floor = float(self.c.get('deep_cycle_floor_soc', 4))
    low_reached = self.db.get('calibration_low_reached_at')

    # The ordinary sampler intentionally ignores calibration bookkeeping when
    # automatic calibration is disabled. For an explicit requested cycle we
    # still need the same low-end marker so the normal deep-recharge planner can
    # finish the cycle safely after the request itself is cleared.
    if was_pending and soc <= floor:
        if not low_reached:
            low_reached = now_iso
            self.db.set('calibration_low_reached_at', low_reached)
            core.LOG.info(
                'Manual low calibration endpoint observed with automatic calibration disabled: soc=%.1f%% floor=%.1f%%; preserving normal deep-recharge completion',
                soc, floor,
            )

        completed_at = low_reached
        self.db.set(_PENDING_KEY, False)
        self.db.set(_COMPLETED_AT_KEY, completed_at)
        self.db.set(_LAST_RESULT_KEY, 'completed')
        core.LOG.info(
            'Manual low calibration completed: soc=%.1f%% floor=%.1f%% last_deep_before=%s low_reached_at=%s; request cleared and recharge state preserved',
            soc, floor, before_deep or 'unknown', completed_at,
        )
        return

    # If automatic calibration was disabled after (or throughout) a requested
    # low cycle, the legacy sampler will not clear the low marker at 100%. Mirror
    # only that existing completion bookkeeping here so deep_recharge can finish
    # without re-enabling automatic scheduling.
    if not self.c.get('calibration_enabled', True) and low_reached and soc >= 100:
        self.db.set('last_full_soc_at', now_iso)
        self.db.set('last_deep_calibration_at', now_iso)
        self.db.set('calibration_low_reached_at', None)
        core.LOG.info(
            'Manual low calibration recharge completed with automatic calibration disabled: soc=%.1f%% last_deep_calibration_at=%s; automatic calibration remains disabled',
            soc, now_iso,
        )


# Patch the legacy policy function used by the active overlay pipeline. This is
# policy only: forecast physics still comes from the Home Forecaster entity/API.
_calibration_policy_runtime._minimise_calibration_discharge = _natural_first_calibration_discharge
core.Controller.__init__ = _init_with_calibration_buttons
core.Controller.calibration_state = _manual_calibration_state
core.Controller.calibration_attrs = _calibration_attrs_with_manual_request
core.Controller.sample = _sample_with_manual_low_calibration

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
