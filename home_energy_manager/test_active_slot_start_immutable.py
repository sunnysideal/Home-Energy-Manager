import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from components.controller.app import Controller

TZ=ZoneInfo("Europe/London")

class FakeHA:
    def __init__(self, states): self._states=states
    async def state(self, entity): return {"state": self._states[entity]}

def controller(states, now):
    c=Controller.__new__(Controller)
    c.ha=FakeHA(states)
    c.tz=TZ
    c.now=lambda: now
    return c

def test_daily_slot_active_normal_and_wrapped():
    c=controller({}, datetime(2026,9,6,11,50,tzinfo=TZ))
    assert c.daily_slot_active("11:30:00","17:00:00")
    assert not c.daily_slot_active("12:00:00","17:00:00")
    c.now=lambda: datetime(2026,9,6,23,45,tzinfo=TZ)
    assert c.daily_slot_active("23:30:00","05:30:00")
    c.now=lambda: datetime(2026,9,7,4,0,tzinfo=TZ)
    assert c.daily_slot_active("23:30:00","05:30:00")

def test_preserves_start_of_already_active_pause_slot():
    c=controller({"start":"11:30:00","end":"17:00:00"}, datetime(2026,9,6,11,50,tzinfo=TZ))
    got=asyncio.run(c.preserve_active_slot_start("pause","start","end","11:50:00"))
    assert got=="11:30:00"

def test_allows_start_change_before_slot_begins():
    c=controller({"start":"12:00:00","end":"17:00:00"}, datetime(2026,9,6,11,50,tzinfo=TZ))
    got=asyncio.run(c.preserve_active_slot_start("pause","start","end","11:50:00"))
    assert got=="11:50:00"
