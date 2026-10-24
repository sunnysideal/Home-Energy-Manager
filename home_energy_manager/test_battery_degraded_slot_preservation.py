"""Regression: degraded model cannot reset a late cheap-rate charge start."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from components.controller.controller_plan_applier import (
    observed_charge_within_offpeak, apply_safe_fallback,
)
TZ=ZoneInfo("Europe/London")
WINDOW={"start":datetime(2026,10,24,23,tzinfo=TZ),"end":datetime(2026,10,25,6,tzinfo=TZ)}

def test_overnight_delayed_charge_survives_autumn_fallback():
    assert observed_charge_within_offpeak("03:00:00","06:00:00",WINDOW,TZ)
    assert not observed_charge_within_offpeak("22:00:00","06:00:00",WINDOW,TZ)
    assert not observed_charge_within_offpeak("03:00:00","07:00:00",WINDOW,TZ)

def test_reject_zero_duration_and_outside_cheap():
    assert not observed_charge_within_offpeak("00:00:00","00:00:00",WINDOW,TZ)
    assert not observed_charge_within_offpeak("06:00:00","06:00:00",WINDOW,TZ)
    assert not observed_charge_within_offpeak("06:30:00","07:00:00",WINDOW,TZ)

def test_safe_fallback_does_not_write_verified_0300_charge_start_or_rate():
    class HA:
        async def state(self,entity):
            return {"state":{"charge_start":"03:00:00","charge_end":"06:00:00"}[entity]}
    class Log:
        def warning(self,*args):pass
    class Controller:
        tz=TZ
        ha=HA()
        LOG=Log()
        c={"charge_slot_1_start_entity":"charge_start",
           "charge_slot_1_end_entity":"charge_end","preferred_charge_c_rate":0.25,
           "pause_start_entity":"pause_start","pause_end_entity":"pause_end",
           "discharge_slot_1_start_entity":"discharge_start",
           "discharge_slot_1_end_entity":"discharge_end",
           "eco_mode_entity":"eco","charge_schedule_enable_entity":"charge_enable",
           "discharge_schedule_enable_entity":"discharge_enable",
           "pause_mode_entity":"pause_mode","discharge_slot_1_target_entity":"discharge_target",
           "discharge_rate_entity":"discharge_rate",
           "charge_rate_entity":"charge_rate",
           "charge_slot_1_target_entity":"charge_target"}
        writes=[]
        def now(self):return datetime(2026,10,24,14,tzinfo=TZ)
        def export_start(self,w):return datetime(2026,10,24,20,tzinfo=TZ)
        def pause_plan(self,w):return {"mode":"Disabled","start":"00:00:00","end":"00:00:00"}
        async def preserve_active_slot_start(self,label,start,end,desired):return desired
        def tstr(self,dt):return dt.strftime("%H:%M:%S")
        async def num(self,*args):return (4,None)
        async def ensure(self,field,entity,value,deadline=None):
            self.writes.append((field,value))
            return True
    c=Controller()
    assert asyncio.run(apply_safe_fallback(c,WINDOW,13.77,6000))
    touched={x[0] for x in c.writes}
    assert "charge_start" not in touched
    assert "charge_end" not in touched
    assert "charge_rate" not in touched
    assert "charge_target" not in touched
    assert "eco" in touched
