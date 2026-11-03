"""Regular battery charge timing is fixed at the start; no live-SOC extension."""
from datetime import datetime

from test_minimise_active_charge_slot_lock import TZ, Controller, fields, plan


def test_live_soc_shortfall_does_not_extend_a_short_planned_slot():
    c = Controller({'cs': '03:20:00', 'ce': '05:40:00', 'soc': '93'},
                   now=datetime(2026, 9, 22, 5, 37, tzinfo=TZ))
    p = plan(charge={
        'start': '2026-09-22T03:20:00+01:00',
        'end': '2026-09-22T05:40:00+01:00',
        'rate_w': 3991, 'target_soc': 100, 'planned_kwh': 13.0,
    })
    assert fields(c, p)['charge_end'] == '05:40:00'


def test_last_five_minutes_do_not_change_active_rate_or_end():
    c = Controller({'cs': '01:47:00', 'ce': '06:00:00',
                    'cr': '3991', 'soc': '99'},
                   now=datetime(2026, 9, 22, 5, 55, tzinfo=TZ))
    p = plan(charge={
        'start': '2026-09-22T05:55:00+01:00',
        'end': '2026-09-22T06:00:00+01:00',
        'rate_w': 5508, 'target_soc': 100, 'planned_kwh': 0.14,
    })
    actual = fields(c, p)
    assert actual['charge_start'] == '01:47:00'
    assert actual['charge_end'] == '06:00:00'
    assert actual['charge_rate'] == 3991


def test_non_ev_regular_charge_is_capped_at_supplier_cutoff():
    c = Controller({'cs': '05:59:00', 'ce': '06:00:00', 'soc': '99'},
                   now=datetime(2026, 9, 22, 5, 55, tzinfo=TZ))
    p = plan(charge={
        'start': '2026-09-22T05:55:00+01:00',
        'end': '2026-09-22T06:05:00+01:00',
        'rate_w': 5508, 'target_soc': 100, 'planned_kwh': 0.14,
    })
    assert fields(c, p)['charge_end'] == '06:00:00'
