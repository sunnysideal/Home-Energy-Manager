import sqlite3
from datetime import datetime,timezone
from types import SimpleNamespace

from components.controller import controller_battery_parity as parity
from components.home_forecaster.app import battery_model_seed


def attrs(now,top=15.0,factor=None):
    bands=[]
    for lo,hi in parity.SOC_BANDS:
        f=parity.GENERIC[(lo,hi)] if factor is None else factor
        bands.append({'soc_lo':lo,'soc_hi':hi,'effective_factor':f})
    return {'schema_version':1,'generated_at':now.isoformat(),'bands':bands,'top_completion_allowance_minutes':top,'generic_top_completion_minutes':15.0}


def controller_minutes(soc,target,rate,cap,model):
    return parity.charge_minutes(soc,target,rate,cap,model)


def test_generic_parity_across_representative_charge_plans():
    now=datetime.now(timezone.utc); model=attrs(now)
    ok,reason,_=parity.validate(model,now); assert ok,reason
    cases=[(4,55,3375),(35,88,3375),(4,81,3455),(80,99,5400),(99,100,5400),(4,100,5400)]
    for soc,target,rate in cases:
        expected=0.0
        for lo,hi in parity.SOC_BANDS:
            ov=max(0,min(target,hi)-max(soc,lo))
            if ov:expected+=(13.5*(ov/100))/((rate/1000)*parity.GENERIC[(lo,hi)])*60
        if target>=100:expected+=15
        assert abs(controller_minutes(soc,target,rate,13.5,model)-expected)<1e-9


def test_learned_and_top_completion_change_duration_deterministically():
    now=datetime.now(timezone.utc); generic=attrs(now); learned=attrs(now,top=27.0)
    for band in learned['bands']:
        if band['soc_lo']>=80:band['effective_factor']=max(.05,band['effective_factor']*.8)
    assert parity.charge_minutes(80,100,5400,13.5,learned)>parity.charge_minutes(80,100,5400,13.5,generic)


def test_model_validation_reports_missing_stale_and_invalid():
    now=datetime.now(timezone.utc)
    ok,reason,_=parity.validate({},now); assert not ok and reason=='schema_version'
    stale=attrs(datetime(2026,1,1,tzinfo=timezone.utc)); ok,reason,_=parity.validate(stale,now); assert not ok and reason=='stale'
    bad=attrs(now); bad['bands'][0]['effective_factor']=2; ok,reason,_=parity.validate(bad,now); assert not ok and reason=='band_values'


class Store:
    def __init__(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE battery_charge_bands(band_lo REAL,band_hi REAL,learned_factor REAL,confidence REAL,full_equiv REAL,obs_count INTEGER,p10 REAL,p90 REAL,updated_at TEXT,PRIMARY KEY(band_lo,band_hi))')
    def meta_get(self,key,default=None):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone(); return row[0] if row else default
    def meta_set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',(key,value)); self.db.commit()


class Client:
    def __init__(self,seed):self.seed=seed
    def state_optional(self,entity):return {'attributes':self.seed}


def test_controller_seed_is_applied_once_and_survives_restart_semantics():
    now=datetime.now(timezone.utc); seed=attrs(now,top=24.0); seed['source']='controller_migration_seed'; seed['top_completion']={'attempts':3,'successes':2,'misses':1}
    for band in seed['bands']:
        band.update({'generic_factor':band['effective_factor'],'learned_factor':None,'confidence':0,'samples':0,'full_equiv':0,'p10':None,'p90':None,'updated_at':now.isoformat()})
    store=Store(); model=SimpleNamespace(SOC_BANDS=parity.SOC_BANDS,base=SimpleNamespace(LOG=SimpleNamespace(info=lambda *a,**k:None)))
    assert battery_model_seed.seed_if_needed(Client(seed),store,model,now)
    assert store.meta_get('battery_model_migration_state')=='seeded'
    assert store.meta_get('battery_model_top_completion_allowance_minutes')=='24.0'
    first=list(store.db.execute('SELECT * FROM battery_charge_bands ORDER BY band_lo'))
    changed=dict(seed); changed['top_completion_allowance_minutes']=60
    assert not battery_model_seed.seed_if_needed(Client(changed),store,model,now)
    second=list(store.db.execute('SELECT * FROM battery_charge_bands ORDER BY band_lo'))
    assert [tuple(r) for r in first]==[tuple(r) for r in second]
    assert store.meta_get('battery_model_top_completion_allowance_minutes')=='24.0'
