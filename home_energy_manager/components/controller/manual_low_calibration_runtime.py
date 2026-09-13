#!/usr/bin/env python3
"""One-shot user-requested low-calibration overlay.

The package owns the Home Assistant request button. Calibration policy remains in
Controller and all actual planning/writes continue through the existing planner
and plan-applier path.
"""
import asyncio
import threading

import active_runtime as runtime
from common.mqtt_button import publish_command_button

core = runtime.core
_original_init = core.Controller.__init__
_original_sample = core.Controller.sample
_original_calibration_state = core.Controller.calibration_state
_original_calibration_attrs = core.Controller.calibration_attrs

_PENDING_KEY = 'manual_low_calibration_pending'
_REQUESTED_AT_KEY = 'manual_low_calibration_requested_at'
_COMPLETED_AT_KEY = 'manual_low_calibration_completed_at'
_LAST_RESULT_KEY = 'manual_low_calibration_last_result'
_BUTTON_ENTITY = 'button.home_energy_manager_request_low_calibration'


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


core.Controller.__init__ = _init_with_manual_low_button
core.Controller.calibration_state = _manual_calibration_state
core.Controller.calibration_attrs = _calibration_attrs_with_manual_request
core.Controller.sample = _sample_with_manual_low_calibration

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
