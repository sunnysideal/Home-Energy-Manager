import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from components.controller import controller_battery as battery


def model_attrs(now, *, factor=.95, top=15.0):
    return {
        'schema_version':1,
        'generated_at':now.isoformat(),
        'bands':[{'soc_lo':lo,'soc_hi':hi,'effective_factor':factor} for lo,hi in battery.SOC_BANDS],
        'top_completion_allowance_minutes':top,
        'generic_top_completion_minutes':15.0,
    }


class Log:
    def __init__(self): self.messages=[]
    def info(self,*args): self.messages.append(('info',args))
    def warning(self,*args): self.messages.append(('warning',args))
    def error(self,*args): self.messages.append(('error',args))


class HA:
    def __init__(self,state): self.value=state
    async def state(self,_entity): return self.value


def controller(now,state):
    return SimpleNamespace(ha=HA(state),now=lambda:now,LOG=Log(),c={})


def test_valid_forecaster_model_is_sole_charge_duration_authority():
    now=datetime.now(timezone.utc)
    attrs=model_attrs(now,factor=.5,top=30.0)
    c=controller(now,{'attributes':attrs})
    asyncio.run(battery.refresh_forecaster_model(c))
    assert c._battery_model_source=='forecaster'
    expected=battery.forecaster_charge_minutes(35,100,3375,13.5,attrs)
    assert battery.charge_minutes(c,35,100,3375,13.5)==expected


def test_missing_invalid_or_stale_model_has_no_controller_fallback():
    now=datetime.now(timezone.utc)
    cases=[None,{'attributes':{}},{'attributes':model_attrs(now-timedelta(minutes=16))}]
    for state in cases:
        c=controller(now,state)
        asyncio.run(battery.refresh_forecaster_model(c))
        assert c._battery_model_source=='unavailable'
        assert c._battery_model_unavailable_reason is not None
        with pytest.raises(RuntimeError,match='authoritative Home Forecaster battery model unavailable'):
            battery.charge_minutes(c,4,81,3455,13.5)


def test_forecaster_model_validation_rejects_future_and_invalid_band_values():
    now=datetime.now(timezone.utc)
    future=model_attrs(now+timedelta(minutes=2))
    ok,reason,_=battery.validate_forecaster_model(future,now)
    assert not ok and reason=='future'
    bad=model_attrs(now)
    bad['bands'][0]['effective_factor']=1.5
    ok,reason,_=battery.validate_forecaster_model(bad,now)
    assert not ok and reason=='band_values'
