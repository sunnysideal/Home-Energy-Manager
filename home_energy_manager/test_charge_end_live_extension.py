import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from components.controller.controller_plan_applier import charge_end_with_live_extension

TZ=ZoneInfo('Europe/London')


class FakeHA:
    def __init__(self,soc):self.soc=soc
    async def state(self,entity):return {'state':str(self.soc)}


class FakeController:
    def __init__(self,now,soc,margin=10):
        self.tz=TZ; self._now=now; self.ha=FakeHA(soc)
        self.c={'battery_soc_entity':'sensor.battery_soc','charge_safety_margin_minutes':margin}
    def now(self):return self._now


def plan(end='2026-09-14T05:40:00+01:00',target=100):
    return {
        'offpeak':{'start':'2026-09-13T23:00:00+01:00','end':'2026-09-14T06:00:00+01:00','rate_p':6.99},
        'charge':{'start':'2026-09-14T03:20:00+01:00','end':end,'rate_w':6000,'target_soc':target,'planned_kwh':13.0},
    }


def run(now,soc,end='2026-09-14T05:40:00+01:00',target=100,margin=10):
    c=FakeController(datetime.fromisoformat(now),soc,margin)
    return asyncio.run(charge_end_with_live_extension(c,plan(end,target),logging.getLogger(__name__)))


def test_under_target_extends_end_while_cheap():
    got=run('2026-09-14T05:37:00+01:00',93)
    assert got.strftime('%H:%M')=='05:45'


def test_target_reached_does_not_extend():
    got=run('2026-09-14T05:37:00+01:00',100)
    assert got.strftime('%H:%M')=='05:40'


def test_extension_never_crosses_safety_boundary():
    got=run('2026-09-14T05:48:00+01:00',93,end='2026-09-14T05:49:00+01:00')
    assert got.strftime('%H:%M')=='05:50'


def test_no_extension_after_safety_boundary():
    got=run('2026-09-14T05:51:00+01:00',93,end='2026-09-14T05:49:00+01:00')
    assert got.strftime('%H:%M')=='05:49'


def test_no_extension_before_last_mile_window():
    got=run('2026-09-14T05:30:00+01:00',93)
    assert got.strftime('%H:%M')=='05:40'
