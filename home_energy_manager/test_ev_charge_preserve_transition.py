"""Regression: a completed EV top-up must not become a daytime-to-overnight charge."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from components.controller.controller_plan_applier import (
    apply_plan, desired_inverter_fields, charge_end_with_live_extension,
)
from test_minimise_active_charge_slot_lock import Controller, plan

TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 1, 9, 15, tzinfo=TZ)
OVERNIGHT = {
    "start": "2026-11-01T23:00:00+00:00",
    "end": "2026-11-02T06:00:00+00:00",
    "rate_p": 6.66,
}


def preserving_plan():
    p = plan(
        offpeak=dict(OVERNIGHT),
        charge={
            "start": "2026-11-02T02:36:00+00:00",
            "end": "2026-11-02T06:00:00+00:00",
            "rate_w": 3442,
            "target_soc": 83,
            "planned_kwh": 10.88,
        },
        intelligent_go={
            "confirmed": True,
            "slot_start": "2026-11-01T09:00:00+00:00",
            "slot_end": "2026-11-01T09:30:00+00:00",
            "pause_mode": "PauseBoth",
            "intelligent_charge_target_soc": None,
            "charge_reason": "battery_energy_sufficient",
        },
    )
    return p


def make_controller():
    return Controller(
        values={"cs": "09:02:00", "ce": "09:30:00", "cr": "3442", "soc": "84"},
        now=NOW,
    )


def field_values(controller, p):
    return {
        key: desired
        for key, _entity, desired, _deadline in
        asyncio.run(desired_inverter_fields(controller, p, logging.getLogger(__name__)))
    }


def test_ev_preserve_restores_future_overnight_without_joining_clock_times():
    c = make_controller()
    desired = field_values(c, preserving_plan())
    assert desired["charge_start"] == "02:36:00"
    assert desired["charge_end"] == "06:00:00"
    assert desired["pause_mode"] == "PauseBoth"
    assert desired["pause_start"] == "09:00:00"
    assert desired["pause_end"] == "09:30:00"


def test_ev_charge_still_preserves_same_active_half_hour_start():
    c = make_controller()
    p = preserving_plan()
    p["charge"] = {
        "start": "2026-11-01T09:11:00+00:00",
        "end": "2026-11-01T09:30:00+00:00",
        "rate_w": 3442,
        "target_soc": 87,
        "planned_kwh": 0.45,
    }
    p["intelligent_go"]["intelligent_charge_target_soc"] = 87
    p["intelligent_go"]["pause_mode"] = "Disabled"
    desired = field_values(c, p)
    assert desired["charge_start"] == "09:02:00"
    assert desired["charge_end"] == "09:30:00"


def test_confirmed_ev_charge_is_never_extended_into_regular_offpeak():
    c = make_controller()
    p = preserving_plan()
    p["charge"] = {
        "start": "2026-11-01T09:11:00+00:00",
        "end": "2026-11-01T09:30:00+00:00",
        "rate_w": 3442,
        "target_soc": 95,
        "planned_kwh": 0.5,
    }
    end = asyncio.run(charge_end_with_live_extension(c, p, logging.getLogger(__name__)))
    assert end.isoformat() == "2026-11-01T09:30:00+00:00"


def test_active_ev_to_overnight_write_disables_schedule_until_times_confirmed():
    c = make_controller()
    c.ha.values["ch_enabled"] = "on"
    c.ha.writes = []
    c.confirmed_writes_this_apply = 0

    async def ensure(field, entity, value, _deadline=None):
        c.ha.writes.append((field, value))
        c.ha.values[entity] = value
        c.confirmed_writes_this_apply += 1
        return True

    c.ensure = ensure
    result = asyncio.run(apply_plan(c, preserving_plan(), logging.getLogger(__name__)))
    assert result is True
    writes = c.ha.writes
    off = writes.index(("charge_enable", "off"))
    start = writes.index(("charge_start", "02:36:00"))
    end = writes.index(("charge_end", "06:00:00"))
    on = writes.index(("charge_enable", "on"))
    assert off < start < on
    assert off < end < on
    assert c.ha.values["cs"] == "02:36:00"
    assert c.ha.values["ce"] == "06:00:00"


def test_failed_slot_write_never_reenables_charge():
    c = make_controller()
    c.ha.values["ch_enabled"] = "on"
    c.ha.writes = []
    c.confirmed_writes_this_apply = 0

    async def ensure(field, entity, value, _deadline=None):
        c.ha.writes.append((field, value))
        if field == "charge_end":
            return False
        c.ha.values[entity] = value
        return True

    c.ensure = ensure
    result = asyncio.run(apply_plan(c, preserving_plan(), logging.getLogger(__name__)))
    assert result is False
    assert c.ha.values["ch_enabled"] == "off"
    assert ("charge_enable", "on") not in c.ha.writes
