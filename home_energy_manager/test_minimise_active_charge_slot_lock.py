"""Regression tests: a running regular Minimise Export charge is immutable."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from components.controller.controller_plan_applier import (
    desired_inverter_fields, locked_minimise_charge_slot,
)

TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 9, 22, 5, 50, tzinfo=TZ)
OFFPEAK = {'start': '2026-09-21T23:00:00+01:00',
           'end': '2026-09-22T06:00:00+01:00', 'rate_p': 6.99}


class HA:
    def __init__(self, values):
        self.values = values

    async def state(self, entity):
        value = self.values.get(entity)
        return {'state': str(value)} if value is not None else None


class Controller:
    tz = TZ

    def __init__(self, values=None, now=NOW):
        self.ha = HA({'cs': '02:09:00', 'ce': '06:00:00',
                      'cr': '3296', 'soc': '92', **(values or {})})
        self.clock = now
        self.c = {
            'charge_slot_1_start_entity': 'cs',
            'charge_slot_1_end_entity': 'ce',
            'charge_rate_entity': 'cr',
            'battery_soc_entity': 'soc',
            'charge_safety_margin_minutes': 10,
            'pause_start_entity': 'ps',
            'pause_end_entity': 'pe',
            'discharge_slot_1_start_entity': 'ds',
            'discharge_slot_1_end_entity': 'de',
            'eco_mode_entity': 'eco',
            'charge_schedule_enable_entity': 'ch_enabled',
            'discharge_schedule_enable_entity': 'dis_enabled',
            'pause_mode_entity': 'pause_mode',
            'discharge_rate_entity': 'dr',
            'charge_slot_1_target_entity': 'ct',
            'discharge_slot_1_target_entity': 'dt',
        }

    def now(self):
        return self.clock

    def tstr(self, value):
        return value.strftime('%H:%M:%S')

    def daily_slot_active(self, start, end):
        try:
            a, b = datetime.strptime(start, '%H:%M:%S').time(), datetime.strptime(end, '%H:%M:%S').time()
        except ValueError:
            return False
        n = self.clock.time()
        return a <= n < b if a < b else a > b and (n >= a or n < b)

    async def preserve_active_slot_start(self, label, start_entity, end_entity, desired_start):
        if label == 'charge' and self.daily_slot_active(
            self.ha.values.get(start_entity, ''), self.ha.values.get(end_entity, '')
        ):
            return self.ha.values[start_entity]
        return desired_start

    def pause_plan(self, window):
        return {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}

    async def num(self, *_args):
        return 4, True


def plan(**overrides):
    result = {
        'operation': {'mode': 'minimise_export'},
        'offpeak': dict(OFFPEAK),
        'calibration': {'state': 'disabled'},
        'charge': {'start': '2026-09-22T05:50:00+01:00',
                   'end': '2026-09-22T06:00:00+01:00',
                   'rate_w': 5516, 'target_soc': 92, 'planned_kwh': 0.0},
        'discharge': {'start': '2026-09-22T20:00:00+01:00',
                      'end': '2026-09-22T20:00:00+01:00',
                      'rate_w': 6000, 'target_soc': 4,
                      'planned_kwh': 0.0, 'kind': 'none'},
        'pause': {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'},
    }
    result.update(overrides)
    return result


def fields(controller, p):
    return {field: desired for field, _entity, desired, _window_end
            in asyncio.run(desired_inverter_fields(controller, p, logging.getLogger(__name__)))}


def test_0550_rate_jump_and_zero_planned_kwh_do_not_modify_running_charge():
    p = plan()
    applied = fields(Controller(), p)
    assert applied['charge_start'] == '02:09:00'
    assert applied['charge_end'] == '06:00:00'
    assert applied['charge_rate'] == 3296
    assert applied['charge_target'] == 92
    assert p['charge']['rate_w'] == 5516  # The new plan may still be published.


def test_active_slot_is_preserved_when_forecast_moves_start_and_end():
    p = plan(charge={'start': '2026-09-22T05:40:00+01:00',
                     'end': '2026-09-22T05:55:00+01:00',
                     'rate_w': 5000, 'target_soc': 92, 'planned_kwh': 0.2})
    applied = fields(Controller(), p)
    assert (applied['charge_start'], applied['charge_end'], applied['charge_rate']) == (
        '02:09:00', '06:00:00', 3296)


def test_future_charge_can_be_replanned():
    c = Controller({'cs': '05:55:00'})
    assert asyncio.run(locked_minimise_charge_slot(c, plan(), logging.getLogger(__name__))) is None
    applied = fields(c, plan())
    assert applied['charge_start'] == '05:50:00'
    assert applied['charge_rate'] == 5516


def test_lock_expires_after_cheap_rate():
    c = Controller(now=datetime(2026, 9, 22, 6, 0, tzinfo=TZ))
    assert asyncio.run(locked_minimise_charge_slot(c, plan(), logging.getLogger(__name__))) is None


def test_high_priority_overlays_can_replace_running_slot():
    baseline = plan()
    variants = (
        {'intelligent_go': {'confirmed': True}},
        {'calibration': {'state': 'deep_recharge'}},
        {'calibration': {'state': 'awaiting_deep_low'}},
        {'calibration': {'state': 'top_due'}},
        {'axle': {'action': 'prepare_in_regular_offpeak'}},
        {'power_down': {'active': True}},
        {'operation': {'mode': 'maximise_export'}},
        {'charge': {**baseline['charge'], 'kind': 'calibration_recharge'}},
    )
    for override in variants:
        c = Controller()
        p = plan(**override)
        assert asyncio.run(locked_minimise_charge_slot(c, p, logging.getLogger(__name__))) is None, override


def test_slot_is_not_locked_when_inverter_rate_or_times_are_unavailable():
    for override in ({'cr': 'unknown'}, {'cs': '06:00:00'},
                     {'ce': '05:45:00'}, {'ce': '06:30:00'}):
        c = Controller(override)
        assert asyncio.run(locked_minimise_charge_slot(c, plan(), logging.getLogger(__name__))) is None
