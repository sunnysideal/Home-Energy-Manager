#!/usr/bin/env python3
"""One-shot user-requested low-calibration overlay.

This runtime sits above active_runtime and only changes calibration request policy.
All actual calibration planning and inverter writes continue through the existing
Controller planner/applier path.
"""
import asyncio
from datetime import timezone

import active_runtime as runtime

core = runtime.core
_original_sample = core.Controller.sample
_original_calibration_state = core.Controller.calibration_state
_original_calibration_attrs = core.Controller.calibration_attrs

_PENDING_KEY = 'manual_low_calibration_pending'
_REQUESTED_AT_KEY = 'manual_low_calibration_requested_at'
_LAST_SEEN_KEY = 'manual_low_calibration_last_seen_input'
_COMPLETED_AT_KEY = 'manual_low_calibration_completed_at'
_LAST_RESULT_KEY = 'manual_low_calibration_last_result'


def _request_entity(controller):
    return str(controller.c.get('calibration_low_request_entity') or '').strip()


def _pending(controller):
    return bool(controller.db.ok and controller.db.get(_PENDING_KEY))


def _manual_calibration_state(self):
    state = _original_calibration_state(self)
    if state == 'disabled':
        return state
    # Existing low-end completion/recharge state always wins. Otherwise a
    # latched manual request is deliberately indistinguishable from an
    # automatically due deep calibration to the existing planner.
    if state == 'deep_recharge':
        return state
    if _pending(self):
        return 'awaiting_deep_low'
    return state


def _request_status(controller):
    if not controller.c.get('calibration_enabled', True):
        return 'blocked', 'calibration_disabled'
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
        'low_calibration_request_entity': _request_entity(self) or None,
        'low_calibration_requested_at': self.db.get(_REQUESTED_AT_KEY) if self.db.ok else None,
        'low_calibration_request_state': status,
        'low_calibration_request_reason': reason,
        'low_calibration_request_target_soc': float(self.c.get('deep_cycle_floor_soc', 4)),
        'low_calibration_request_completed_at': self.db.get(_COMPLETED_AT_KEY) if self.db.ok else None,
        'low_calibration_request_last_result': self.db.get(_LAST_RESULT_KEY) if self.db.ok else None,
    })
    return attrs


def _is_input_button(entity_id):
    return entity_id.startswith('input_button.')


def _is_input_boolean(entity_id):
    return entity_id.startswith('input_boolean.')


async def _observe_low_calibration_request(controller):
    entity = _request_entity(controller)
    if not entity or not controller.db.ok:
        return
    st = await controller.ha.state(entity)
    if not st:
        return
    value = str(st.get('state') or '').strip()
    previous = controller.db.get(_LAST_SEEN_KEY)

    # First observation seeds edge detection. This avoids replaying an old
    # input_button timestamp or a pre-existing input_boolean state on upgrade.
    if previous is None:
        controller.db.set(_LAST_SEEN_KEY, value)
        return

    trigger = False
    if _is_input_button(entity):
        trigger = bool(value and value not in ('unknown', 'unavailable') and value != previous)
    elif _is_input_boolean(entity):
        trigger = previous.lower() != 'on' and value.lower() == 'on'
    else:
        # Generic event-like helpers are supported by treating any state change
        # as a request. input_button/input_boolean remain the documented paths.
        trigger = bool(value and value not in ('unknown', 'unavailable') and value != previous)

    if value != previous:
        controller.db.set(_LAST_SEEN_KEY, value)
    if not trigger:
        return
    if _pending(controller):
        core.LOG.info('Manual low calibration request ignored because one is already pending: entity=%s state=%s', entity, value)
        return

    now_iso = core.iso(controller.now())
    controller.db.set(_PENDING_KEY, True)
    controller.db.set(_REQUESTED_AT_KEY, now_iso)
    controller.db.set(_LAST_RESULT_KEY, 'pending')
    controller.db.set(_COMPLETED_AT_KEY, None)
    core.LOG.info(
        'Manual low calibration requested: entity=%s state=%s target=%.1f%%; request latched until actual low-SOC completion',
        entity, value, float(controller.c.get('deep_cycle_floor_soc', 4)),
    )


async def _sample_with_manual_low_calibration(self):
    # Observe before the underlying sample so a newly pressed request can enter
    # the existing awaiting_deep_low state in the same planning/sample cycle.
    await _observe_low_calibration_request(self)
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


core.Controller.calibration_state = _manual_calibration_state
core.Controller.calibration_attrs = _calibration_attrs_with_manual_request
core.Controller.sample = _sample_with_manual_low_calibration

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
