from pathlib import Path

ROOT=Path(__file__).parent
CONTROLLER=ROOT/'components'/'controller'


def test_plan_applier_exists_and_is_packaged():
    module=CONTROLLER/'controller_plan_applier.py'
    assert module.exists()
    docker=(ROOT/'Dockerfile').read_text()
    assert 'controller_plan_applier.py /app/runtime/controller/controller_plan_applier.py' in docker


def test_composition_root_routes_normal_and_fallback_apply():
    source=(CONTROLLER/'app.py').read_text()
    assert 'from controller_plan_applier import apply_plan as _apply_plan, apply_safe_fallback as _apply_safe_fallback' in source
    assert 'async def apply(self,plan):return await _apply_plan(self,plan,_legacy.LOG)' in source
    assert 'async def safe(self,window,cap=None,hw=None):return await _apply_safe_fallback(self,window,cap,hw)' in source


def test_desired_field_translation_is_write_free():
    source=(CONTROLLER/'controller_plan_applier.py').read_text()
    start=source.index('async def desired_inverter_fields')
    end=source.index('\n\nasync def apply_plan',start)
    translation=source[start:end]
    assert '.write(' not in translation
    assert 'ensure(' not in translation


def test_only_apply_functions_cross_verified_write_boundary():
    source=(CONTROLLER/'controller_plan_applier.py').read_text()
    assert 'result=await controller.ensure(*item)' in source
    assert 'return all([await controller.ensure(*item) for item in fields])' in source


def test_charge_and_discharge_targets_keep_existing_semantics():
    source=(CONTROLLER/'controller_plan_applier.py').read_text()
    assert "('charge_target',controller.c['charge_slot_1_target_entity'],plan['charge']['target_soc'],wend)" in source
    assert "('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve))" in source
    active=(CONTROLLER/'active_runtime.py').read_text()
    assert "if field == 'charge_target':" in active
    assert 'Charge target write suppressed' in active
