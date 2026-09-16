#!/usr/bin/env python3
"""User-requested high/full battery calibration control.

This layer extends the existing package-owned calibration controls. A manual high
request is persistent Controller intent only: it reuses the normal ``top_due``
planning path, Forecaster-owned battery charge-duration model, Controller safety
rules and plan-applier. It never writes inverter settings directly.
"""
import asyncio
import threading

import manual_low_calibration_runtime as runtime
from common.mqtt_button import publish_command_button

core = runtime.core
_original_init = core.Controller.__init__
_original_sample = core.Controller.sample
_original_calibration_state = core.Controller.calibration_state
_original_calibration_attrs = core.Controller.calibration_attrs

_PENDING_KEY = 'manual_high_calibration_pending'
_REQUESTED_AT_KEY = 'manual_high_calibration_requested_at'
_COMPLETED_AT_KEY = 'manual_high_calibration_completed_at'
_LAST_RESULT_KEY = 'manual_high_calibration_last_result'
_LAST_ATTEMPT_AT_KEY = 'manual_high_calibration_last_attempt_at'
_BUTTON_ENTITY = 'button.home_energy_manager_request_high_calibration'
_TARGET_SOC = 100.0


def _pending(controller):
    return bool(controller.db.ok and controller.db.get(_PENDING_KEY))


def _latch_request(controller):
    """Consume one MQTT press on the Controller thread and persist one request."""
    if not controller.db.ok:
        return
    if _pending(controller):
        core.LOG.info('Manual high calibration button press ignored because one is already pending')
        return
    now_iso = core.iso(controller.now())
    controller.db.set(_PENDING_KEY, True)
    controller.db.set(_REQUESTED_AT_KEY, now_iso)
    controller.db.set(_COMPLETED_AT_KEY, None)
    controller.db.set(_LAST_ATTEMPT_AT_KEY, None)
    controller.db.set(_LAST_RESULT_KEY, 'pending')
    core.LOG.info(
        'Manual high calibration requested: button=%s target=100%%; request latched until actual full-SOC completion',
        _BUTTON_ENTITY,
    )


def _install_request_button(controller):
    event = threading.Event()
    controller._manual_high_calibration_press = event
    available = publish_command_button(
        controller.mqtt,
        _BUTTON_ENTITY,
        'Request High Calibration',
        'mdi:battery-arrow-up',
        event.set,
    )
    controller._manual_high_calibration_button_available = available
    if not available:
        core.LOG.warning('Manual high calibration request button unavailable because Controller MQTT is unavailable')


def _init_with_high_calibration_button(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    _install_request_button(self)


def _manual_high_calibration_state(self):
    """Map explicit high intent into the existing automatic top-calibration state."""
    state = _original_calibration_state(self)
    # A low/deep calibration already in progress or already due retains priority;
    # the high request stays latched and is reconsidered after that cycle finishes.
    if state in ('awaiting_deep_low', 'deep_recharge'):
        return state
    if _pending(self):
        # ``calibration_enabled`` governs automatic periodic scheduling only.
        # Reusing top_due keeps all normal tariff, event and safety policy intact.
        return 'top_due'
    return state


def _request_status(controller):
    if _pending(controller):
        state = controller.calibration_state()
        if state in ('awaiting_deep_low', 'deep_recharge'):
            return 'blocked', f'existing_{state}'
        if getattr(controller, 'active', None) is not None:
            return 'charging', 'user_requested'
        if state == 'top_due':
            return 'planned', 'user_requested'
        return 'waiting', f'calibration_state_{state}'
    completed = controller.db.get(_COMPLETED_AT_KEY) if controller.db.ok else None
    return ('completed', 'target_observed') if completed else ('not_requested', 'none')


def _calibration_attrs_with_high_request(self):
    attrs = _original_calibration_attrs(self)
    status, reason = _request_status(self)
    attrs.update({
        'high_calibration_requested': _pending(self),
        'high_calibration_request_button': _BUTTON_ENTITY,
        'high_calibration_request_button_available': bool(getattr(self, '_manual_high_calibration_button_available', False)),
        'high_calibration_requested_at': self.db.get(_REQUESTED_AT_KEY) if self.db.ok else None,
        'high_calibration_request_state': status,
        'high_calibration_request_reason': reason,
        'high_calibration_request_target_soc': _TARGET_SOC,
        'high_calibration_request_completed_at': self.db.get(_COMPLETED_AT_KEY) if self.db.ok else None,
        'high_calibration_request_last_attempt_at': self.db.get(_LAST_ATTEMPT_AT_KEY) if self.db.ok else None,
        'high_calibration_request_last_result': self.db.get(_LAST_RESULT_KEY) if self.db.ok else None,
    })
    return attrs


async def _sample_with_manual_high_calibration(self):
    # MQTT callbacks only set a thread-safe Event. Persistence and policy stay on
    # the Controller thread, so reconnect/restart cannot replay an old command.
    request_event = getattr(self, '_manual_high_calibration_press', None)
    if request_event is not None and request_event.is_set():
        request_event.clear()
        _latch_request(self)

    was_pending = _pending(self)
    await _original_sample(self)
    if not self.db.ok:
        return

    entity = self.c.get('battery_soc_entity', '')
    st = await self.ha.state(entity) if entity else None
    soc = core.as_float(st.get('state')) if st else None
    if soc is None or not was_pending:
        return

    now_iso = core.iso(self.now())
    if getattr(self, 'active', None) is not None:
        self.db.set(_LAST_ATTEMPT_AT_KEY, now_iso)
        self.db.set(_LAST_RESULT_KEY, 'charging')

    # Actual reported 100% is the existing top-calibration success criterion.
    # Do not clear at 99%, on plan creation, or merely because charging began.
    if soc >= _TARGET_SOC:
        self.db.set('last_full_soc_at', now_iso)
        self.db.set(_PENDING_KEY, False)
        self.db.set(_COMPLETED_AT_KEY, now_iso)
        self.db.set(_LAST_ATTEMPT_AT_KEY, now_iso)
        self.db.set(_LAST_RESULT_KEY, 'completed')
        core.LOG.info(
            'Manual high calibration completed: soc=%.1f%% last_full_soc_at=%s; request cleared and automatic calibration setting remains %s',
            soc, now_iso, 'enabled' if self.c.get('calibration_enabled', True) else 'disabled',
        )


core.Controller.__init__ = _init_with_high_calibration_button
core.Controller.calibration_state = _manual_high_calibration_state
core.Controller.calibration_attrs = _calibration_attrs_with_high_request
core.Controller.sample = _sample_with_manual_high_calibration

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
