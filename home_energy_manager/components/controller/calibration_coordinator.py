"""Single owner of calibration request intent, state precedence and observation.

PR 1 of #116: retain the existing automatic/SoC-crossing observer and the
legacy natural-first discharge/recharge planner, called explicitly by the
active controller until their respective later refactor stages.

MQTT callbacks only set Events. All persistent state changes run in the
Controller's sampling thread. No inverter write is made here.
"""
import threading

import app_core as core
from common.mqtt_button import publish_command_button

LOW_PENDING = 'manual_low_calibration_pending'
LOW_REQUESTED_AT = 'manual_low_calibration_requested_at'
LOW_COMPLETED_AT = 'manual_low_calibration_completed_at'
LOW_RESULT = 'manual_low_calibration_last_result'
LOW_BUTTON = 'button.home_energy_manager_request_low_calibration'
HIGH_PENDING = 'manual_high_calibration_pending'
HIGH_REQUESTED_AT = 'manual_high_calibration_requested_at'
HIGH_COMPLETED_AT = 'manual_high_calibration_completed_at'
HIGH_RESULT = 'manual_high_calibration_last_result'
HIGH_ATTEMPT_AT = 'manual_high_calibration_last_attempt_at'
HIGH_BUTTON = 'button.home_energy_manager_request_high_calibration'
CLEAR_BUTTON = 'button.home_energy_manager_clear_calibration_state'
CLEAR_AT = 'calibration_state_cleared_at'
CLEAR_RESULT = 'calibration_state_clear_result'
CLEAR_KEYS = 'calibration_state_cleared_keys'
CLEARABLE = (
    (LOW_PENDING, False),
    (HIGH_PENDING, False),
    ('high_calibration_requested', False),
    ('manual_high_calibration_requested', False),
    ('calibration_low_reached_at', None),
)


class CalibrationCoordinator:
    """Own request state, manual cycle precedence and observed completion."""

    def __init__(self, controller, automatic_state):
        self.controller = controller
        self.automatic_state = automatic_state
        self._install_buttons()

    def _pending(self, key):
        c = self.controller
        return bool(c.db.ok and c.db.get(key))

    def _install_buttons(self):
        c = self.controller
        for attribute, available_attribute, entity, name, icon in (
            ('_manual_low_calibration_press', '_manual_low_calibration_button_available',
             LOW_BUTTON, 'Request Low Calibration', 'mdi:battery-arrow-down'),
            ('_clear_calibration_state_press', '_clear_calibration_state_button_available',
             CLEAR_BUTTON, 'Clear Calibration State', 'mdi:battery-off-outline'),
            ('_manual_high_calibration_press', '_manual_high_calibration_button_available',
             HIGH_BUTTON, 'Request High Calibration', 'mdi:battery-arrow-up'),
        ):
            event = threading.Event()
            setattr(c, attribute, event)
            available = publish_command_button(c.mqtt, entity, name, icon, event.set)
            setattr(c, available_attribute, available)
            if not available:
                core.LOG.warning('%s unavailable because Controller MQTT is unavailable', name)

    def _latch_low(self):
        c = self.controller
        if not c.db.ok:
            return
        if self._pending(LOW_PENDING):
            core.LOG.info('Manual low calibration button press ignored because one is already pending')
            return
        now_iso = core.iso(c.now())
        c.db.set(LOW_PENDING, True)
        c.db.set(LOW_REQUESTED_AT, now_iso)
        c.db.set(LOW_RESULT, 'pending')
        c.db.set(LOW_COMPLETED_AT, None)
        core.LOG.info(
            'Manual low calibration requested: button=%s target=%.1f%%; request latched until actual low-SOC completion',
            LOW_BUTTON, float(c.c.get('deep_cycle_floor_soc', 4)),
        )

    def _latch_high(self):
        c = self.controller
        if not c.db.ok:
            return
        if self._pending(HIGH_PENDING):
            core.LOG.info('Manual high calibration button press ignored because one is already pending')
            return
        now_iso = core.iso(c.now())
        c.db.set(HIGH_PENDING, True)
        c.db.set(HIGH_REQUESTED_AT, now_iso)
        c.db.set(HIGH_COMPLETED_AT, None)
        c.db.set(HIGH_ATTEMPT_AT, None)
        c.db.set(HIGH_RESULT, 'pending')
        core.LOG.info(
            'Manual high calibration requested: button=%s target=100%%; request latched until actual full-SOC completion',
            HIGH_BUTTON,
        )

    def _clear(self):
        c = self.controller
        if not c.db.ok:
            return
        cleared = []
        low_was_pending = self._pending(LOW_PENDING)
        high_was_pending = any(bool(c.db.get(key)) for key in (
            HIGH_PENDING, 'high_calibration_requested', 'manual_high_calibration_requested',
        ))
        for key, cleared_value in CLEARABLE:
            current = c.db.get(key)
            active = current is not None if key == 'calibration_low_reached_at' else bool(current)
            if not active:
                continue
            c.db.set(key, cleared_value)
            cleared.append(key)
        if low_was_pending:
            c.db.set(LOW_RESULT, 'cleared')
        if high_was_pending:
            c.db.set(HIGH_RESULT, 'cleared')
        result = 'cleared' if cleared else 'nothing_to_clear'
        c.db.set(CLEAR_AT, core.iso(c.now()))
        c.db.set(CLEAR_RESULT, result)
        c.db.set(CLEAR_KEYS, cleared)
        core.LOG.info(
            'Calibration state clear requested: result=%s cleared=%s; historical calibration timestamps and learning preserved',
            result, ','.join(cleared) if cleared else 'none',
        )

    def state(self):
        c = self.controller
        state = self.automatic_state(c)
        # The automatically scheduled deep cycle, or the manually requested
        # low cycle, has priority over any queued high request.
        if state != 'deep_recharge' and c.db.ok and c.db.get('calibration_low_reached_at'):
            state = 'deep_recharge'
        if state not in ('deep_recharge', 'awaiting_deep_low') and self._pending(LOW_PENDING):
            state = 'awaiting_deep_low'
        if state in ('awaiting_deep_low', 'deep_recharge'):
            return state
        return 'top_due' if self._pending(HIGH_PENDING) else state

    def _low_status(self):
        c = self.controller
        if self._pending(LOW_PENDING):
            state = c.calibration_state()
            if state == 'awaiting_deep_low':
                return 'planned', 'user_requested'
            return 'waiting', f'calibration_state_{state}'
        completed = c.db.get(LOW_COMPLETED_AT) if c.db.ok else None
        return ('completed', 'target_observed') if completed else ('not_requested', 'none')

    def _high_status(self):
        c = self.controller
        if self._pending(HIGH_PENDING):
            state = c.calibration_state()
            if state in ('awaiting_deep_low', 'deep_recharge'):
                return 'blocked', f'existing_{state}'
            if getattr(c, 'active', None) is not None:
                return 'charging', 'user_requested'
            if state == 'top_due':
                return 'planned', 'user_requested'
            return 'waiting', f'calibration_state_{state}'
        completed = c.db.get(HIGH_COMPLETED_AT) if c.db.ok else None
        return ('completed', 'target_observed') if completed else ('not_requested', 'none')

    def attributes(self, base_attrs):
        c = self.controller
        attrs = dict(base_attrs)
        low_state, low_reason = self._low_status()
        high_state, high_reason = self._high_status()
        get = c.db.get if c.db.ok else lambda key, default=None: default
        attrs.update({
            'low_calibration_requested': self._pending(LOW_PENDING),
            'low_calibration_request_button': LOW_BUTTON,
            'low_calibration_request_button_available': bool(getattr(c, '_manual_low_calibration_button_available', False)),
            'low_calibration_requested_at': get(LOW_REQUESTED_AT),
            'low_calibration_request_state': low_state,
            'low_calibration_request_reason': low_reason,
            'low_calibration_request_target_soc': float(c.c.get('deep_cycle_floor_soc', 4)),
            'low_calibration_request_completed_at': get(LOW_COMPLETED_AT),
            'low_calibration_request_last_result': get(LOW_RESULT),
            'calibration_clear_button': CLEAR_BUTTON,
            'calibration_clear_button_available': bool(getattr(c, '_clear_calibration_state_button_available', False)),
            'calibration_state_cleared_at': get(CLEAR_AT),
            'calibration_state_clear_result': get(CLEAR_RESULT),
            'calibration_state_cleared_keys': get(CLEAR_KEYS, []),
            'high_calibration_requested': self._pending(HIGH_PENDING),
            'high_calibration_request_button': HIGH_BUTTON,
            'high_calibration_request_button_available': bool(getattr(c, '_manual_high_calibration_button_available', False)),
            'high_calibration_requested_at': get(HIGH_REQUESTED_AT),
            'high_calibration_request_state': high_state,
            'high_calibration_request_reason': high_reason,
            'high_calibration_request_target_soc': 100.0,
            'high_calibration_request_completed_at': get(HIGH_COMPLETED_AT),
            'high_calibration_request_last_attempt_at': get(HIGH_ATTEMPT_AT),
            'high_calibration_request_last_result': get(HIGH_RESULT),
        })
        return attrs

    def before_sample(self):
        """Capture request intent in the same order as the previous wrappers."""
        c = self.controller
        # Previously the outer high wrapper processed the high press before the
        # inner low wrapper processed low and clear. Retain that ordering.
        for attribute, action in (
            ('_manual_high_calibration_press', self._latch_high),
            ('_manual_low_calibration_press', self._latch_low),
            ('_clear_calibration_state_press', self._clear),
        ):
            event = getattr(c, attribute, None)
            if event is not None and event.is_set():
                event.clear()
                action()
        return {
            'low_pending': self._pending(LOW_PENDING),
            'high_pending': self._pending(HIGH_PENDING),
            'before_deep': c.db.get('last_deep_calibration_at') if c.db.ok else None,
        }

    async def after_sample(self, before):
        """Apply low, then high completion from the same genuine HA SOC sample."""
        c = self.controller
        if not c.db.ok:
            return
        entity = c.c.get('battery_soc_entity', '')
        st = await c.ha.state(entity) if entity else None
        soc = core.as_float(st.get('state')) if st else None
        if soc is None:
            return
        now_iso = core.iso(c.now())
        floor = float(c.c.get('deep_cycle_floor_soc', 4))
        low_reached = c.db.get('calibration_low_reached_at')

        if before['low_pending'] and soc <= floor:
            if not low_reached:
                low_reached = now_iso
                c.db.set('calibration_low_reached_at', low_reached)
                core.LOG.info(
                    'Manual low calibration endpoint observed with automatic calibration disabled: soc=%.1f%% floor=%.1f%%; preserving normal deep-recharge completion',
                    soc, floor,
                )
            c.db.set(LOW_PENDING, False)
            c.db.set(LOW_COMPLETED_AT, low_reached)
            c.db.set(LOW_RESULT, 'completed')
            core.LOG.info(
                'Manual low calibration completed: soc=%.1f%% floor=%.1f%% last_deep_before=%s low_reached_at=%s; request cleared and recharge state preserved',
                soc, floor, before['before_deep'] or 'unknown', low_reached,
            )
        elif not c.c.get('calibration_enabled', True) and low_reached and soc >= 100:
            c.db.set('last_full_soc_at', now_iso)
            c.db.set('last_deep_calibration_at', now_iso)
            c.db.set('calibration_low_reached_at', None)
            core.LOG.info(
                'Manual low calibration recharge completed with automatic calibration disabled: soc=%.1f%% last_deep_calibration_at=%s; automatic calibration remains disabled',
                soc, now_iso,
            )

        if before['high_pending']:
            if getattr(c, 'active', None) is not None:
                c.db.set(HIGH_ATTEMPT_AT, now_iso)
                c.db.set(HIGH_RESULT, 'charging')
            # Actual 100% is required; never complete a high request at 99%.
            if soc >= 100:
                c.db.set('last_full_soc_at', now_iso)
                c.db.set(HIGH_PENDING, False)
                c.db.set(HIGH_COMPLETED_AT, now_iso)
                c.db.set(HIGH_ATTEMPT_AT, now_iso)
                c.db.set(HIGH_RESULT, 'completed')
                core.LOG.info(
                    'Manual high calibration completed: soc=%.1f%% last_full_soc_at=%s; request cleared and automatic calibration setting remains %s',
                    soc, now_iso, 'enabled' if c.c.get('calibration_enabled', True) else 'disabled',
                )
