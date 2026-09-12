from pathlib import Path
ROOT=Path(__file__).parent
CTRL=ROOT/'components'/'controller'

def test_overlay_pipeline_order_is_explicit():
    text=(CTRL/'controller_overlay_pipeline.py').read_text().replace(' ','')
    assert "OVERLAY_ORDER=('power_down','axle','ev_smart_charging')" in text
    runtime=(CTRL/'controller_overlay_runtime.py').read_text()
    assert "['power_down','axle','ev_smart_charging']" in runtime

def test_overlay_runtime_has_no_ha_write_path():
    for name in ('controller_overlay_pipeline.py','controller_overlay_runtime.py'):
        text=(CTRL/name).read_text();assert '.ha.write(' not in text;assert '.ensure(' not in text

def test_runtime_packages_legacy_and_explicit_overlay_layers():
    docker=(ROOT/'Dockerfile').read_text()
    assert 'minimise_export_runtime.py /app/runtime/controller/legacy_minimise_export_runtime.py' in docker
    assert 'controller_overlay_runtime.py /app/runtime/controller/minimise_export_runtime.py' in docker
    assert 'controller_overlay_pipeline.py /app/runtime/controller/controller_overlay_pipeline.py' in docker

def test_snapshot_delta_preserves_unrelated_overlay_fields():
    import sys
    sys.path.insert(0,str(CTRL))
    from controller_overlay_pipeline import apply_snapshot_overlay,note_overlay,validate_overlay_order
    before={'charge':{'target_soc':50},'discharge':{'kind':'none'}}
    after={'charge':{'target_soc':60},'discharge':{'kind':'intelligent_export_suspended'}}
    plan={'charge':{'target_soc':50},'discharge':{'kind':'axle_export'},'axle':{'action':'axle_export'}}
    note_overlay(plan,'power_down');note_overlay(plan,'axle')
    apply_snapshot_overlay(plan,after,'ev_smart_charging',baseline=before)
    assert plan['axle']['action']=='axle_export'
    assert plan['charge']['target_soc']==60
    assert plan['discharge']['kind']=='intelligent_export_suspended'
    assert validate_overlay_order(plan)
