from datetime import datetime, timezone, timedelta
from pathlib import Path
import importlib.util
import sys
import tempfile

MODULE=Path(__file__).parent/'components/home_forecaster/app/main.py'
spec=importlib.util.spec_from_file_location('hf_main',MODULE)
m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)


def test_dispatch_energy_is_apportioned_to_forecast_slot():
    s=datetime(2026,9,10,18,0,tzinfo=timezone.utc)
    e=s+timedelta(minutes=30)
    dispatch={"start":s,"end":s+timedelta(hours=1),"energy_kwh":6.0,"power_kw":None,"raw":{}}
    assert abs(m.ev_energy_for_slot(s,e,[dispatch],None)-3.0) < 1e-9


def test_dispatch_uses_learned_power_when_no_energy_in_dispatch():
    s=datetime(2026,9,10,18,0,tzinfo=timezone.utc)
    e=s+timedelta(minutes=30)
    dispatch={"start":s,"end":e,"energy_kwh":None,"power_kw":None,"raw":{}}
    assert abs(m.ev_energy_for_slot(s,e,[dispatch],7.0)-3.5) < 1e-9


def test_ev_power_learning_uses_active_ct_energy_slots():
    with tempfile.TemporaryDirectory() as td:
        store=m.Store(Path(td)/'f.db')
        day=datetime(2026,9,1,tzinfo=timezone.utc).date()
        store.save_fallback(day,'ev',datetime(2026,9,1,0,0,tzinfo=timezone.utc),'00:00',0.01)
        store.save_fallback(day,'ev',datetime(2026,9,1,0,30,tzinfo=timezone.utc),'00:30',3.4)
        store.save_fallback(day,'ev',datetime(2026,9,1,1,0,tzinfo=timezone.utc),'01:00',3.6)
        store.commit()
        assert abs(m.learned_ev_power_kw(store)-7.0) < 1e-9
