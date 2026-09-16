import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from components.controller import controller_battery as battery


def model_attrs(now, *, factor=None, top=15.0):
    bands=[]
    for lo,hi in battery.SOC_BANDS:
        bands.append({
            'soc_lo':lo,
            'soc_hi':hi,
            'effective_factor':battery.GENERIC[(lo,hi)] if factor is None else factor,
        })
    return {
        'schema_version':1,
        'generated_at':now.isoformat(),
        'bands':bands,
        'top_completion_allowance_minutes':top,
        'generic_top_completion_minutes':15.0,
    }


class Log:
    def __init__(self): self.messages=[]
    def info(self,*args): self.messages.append(('info',args))
    def warning(self,*args): self.messages.append(('warning',args))


class HA:
    def __init__(self,state): self.value=state
    async def state(self,_entity): return self.value


class DB:
    ok=False
    def get(self,_key): return None


def controller(now, state):
    return SimpleNamespace(
        ha=HA(state),
        now=lambda:now,
        LOG=Log(),
        db=DB(),
        c={'generic_dwell_minutes':15},
    )


def test_valid_forecaster_model_becomes_authoritative_for_charge_duration():
    now=datetime.now(timezone.utc)
    attrs=model_attrs(now,factor=.5,top=30.0)
    c=controller(now,{'attributes':attrs})
    asyncio.run(battery.refresh_forecaster_model(c))
    assert c._battery_model_source=='forecaster'
    expected=battery.forecaster_charge_minutes(35,100,3375,13.5,attrs)
    fallback=battery.fallback_charge_minutes(c,35,100,3375,13.5)
    actual=battery.charge_minutes(c,35,100,3375,13.5)
    assert actual==expected
    assert actual!=fallback


def test_missing_invalid_or_stale_model_uses_existing_fallback_calculation():
    now=datetime.now(timezone.utc)
    cases=[
        None,
        {'attributes':{}},
        {'attributes':model_attrs(now-timedelta(minutes=16))},
    ]
    for state in cases:
        c=controller(now,state)
        asyncio.run(battery.refresh_forecaster_model(c))
        assert c._battery_model_source=='fallback'
        assert c._battery_model_fallback_reason is not None
        assert battery.charge_minutes(c,4,81,3455,13.5)==battery.fallback_charge_minutes(c,4,81,3455,13.5)


def test_source_switch_preserves_identical_result_for_identical_model_values():
    now=datetime.now(timezone.utc)
    attrs=model_attrs(now)
    c=controller(now,{'attributes':attrs})
    fallback=battery.fallback_charge_minutes(c,4,100,5400,13.5)
    asyncio.run(battery.refresh_forecaster_model(c))
    assert c._battery_model_source=='forecaster'
    assert battery.charge_minutes(c,4,100,5400,13.5)==fallback


def test_forecaster_model_validation_rejects_future_and_invalid_band_values():
    now=datetime.now(timezone.utc)
    future=model_attrs(now+timedelta(minutes=2))
    ok,reason,_=battery.validate_forecaster_model(future,now)
    assert not ok and reason=='future'
    bad=model_attrs(now)
    bad['bands'][0]['effective_factor']=1.5
    ok,reason,_=battery.validate_forecaster_model(bad,now)
    assert not ok and reason=='band_values'
