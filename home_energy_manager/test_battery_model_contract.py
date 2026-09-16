from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

from components.controller.controller_battery import charge_minutes

HOME_FORECASTER_APP = Path(__file__).parent / "components" / "home_forecaster" / "app"
sys.path.insert(0, str(HOME_FORECASTER_APP))
from battery_model_contract import (  # noqa: E402
    BATTERY_MODEL_ENTITY,
    BATTERY_MODEL_SCHEMA_VERSION,
    build_battery_model_attributes,
    charge_minutes_from_model,
    generic_bands,
    validate_battery_model_attributes,
)


def model(now, *, top=15.0):
    return build_battery_model_attributes(
        generated_at=now.isoformat(),
        bands=generic_bands(),
        top_completion_allowance_minutes=top,
    )


def controller_for(attrs):
    return SimpleNamespace(_battery_model_source='forecaster',_battery_model_attrs=attrs)


def test_contract_has_stable_entity_and_schema_version():
    assert BATTERY_MODEL_ENTITY == "sensor.home_energy_manager_battery_model"
    assert BATTERY_MODEL_SCHEMA_VERSION == 1


def test_contract_matches_controller_charge_minutes():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    attrs = model(now)
    controller = controller_for(attrs)
    cases = [(4,20,3455,13.5),(20,80,3455,13.5),(80,95,3455,13.5),(95,99,3455,13.5),(99,100,3455,13.5),(39,100,3455,13.5),(70,100,5400,13.5)]
    for soc,target,rate,capacity in cases:
        expected=charge_minutes_from_model(soc,target,rate,capacity,attrs)
        assert charge_minutes(controller,soc,target,rate,capacity)==expected


def test_contract_represents_effective_learned_factor_and_top_allowance():
    now=datetime(2026,9,13,12,0,tzinfo=timezone.utc)
    bands=generic_bands(); top_band=next(b for b in bands if b['soc_lo']==99)
    top_band.update(learned_factor=.25,confidence=.5,effective_factor=.30)
    attrs=build_battery_model_attributes(generated_at=now.isoformat(),bands=bands,top_completion_allowance_minutes=35,diagnostics={'attempts':2,'successes':0,'misses':2,'confidence':.4})
    assert validate_battery_model_attributes(attrs,now=now)==(True,'ok')
    assert attrs['top_completion_allowance_minutes']==35
    assert top_band['effective_factor']==.30


def test_validation_rejects_stale_incomplete_and_incompatible_models():
    now=datetime(2026,9,13,12,0,tzinfo=timezone.utc)
    stale=model(now-timedelta(minutes=16)); assert validate_battery_model_attributes(stale,now=now)==(False,'stale')
    incomplete=model(now); incomplete['bands']=incomplete['bands'][:-1]; assert validate_battery_model_attributes(incomplete,now=now)==(False,'bands')
    incompatible=model(now); incompatible['schema_version']=999; assert validate_battery_model_attributes(incompatible,now=now)==(False,'schema_version')
