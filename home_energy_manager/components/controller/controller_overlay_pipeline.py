"""Explicit controller plan-overlay helpers."""
from copy import deepcopy
OVERLAY_ORDER=('power_down','axle','ev_smart_charging')
class _Unchanged:pass
_UNCHANGED=_Unchanged()

def _merge_diff(before,after):
    if isinstance(before,dict) and isinstance(after,dict):
        out={}
        for key in set(before)|set(after):
            if key not in after:out[key]=None;continue
            if key not in before:out[key]=deepcopy(after[key]);continue
            child=_merge_diff(before[key],after[key])
            if child is not _UNCHANGED:out[key]=child
        return out if out else _UNCHANGED
    return _UNCHANGED if before==after else deepcopy(after)

def plan_diff(before,after):
    diff=_merge_diff(before or {},after or {})
    return {} if diff is _UNCHANGED else diff

def _apply_diff(target,diff):
    for key,value in diff.items():
        if value is None:target.pop(key,None)
        elif isinstance(value,dict) and isinstance(target.get(key),dict):_apply_diff(target[key],value)
        else:target[key]=deepcopy(value)

def apply_snapshot_overlay(plan,staged_plan,name,baseline=None):
    """Apply oracle delta to plan without deleting unrelated prior overlays."""
    delta=plan_diff(plan if baseline is None else baseline,staged_plan)
    if delta:_apply_diff(plan,delta)
    pipeline=plan.setdefault('overlay_pipeline',{});applied=list(pipeline.get('applied') or [])
    if name not in applied:applied.append(name)
    pipeline['applied']=applied;pipeline['last_overlay']=name;pipeline['changed']=bool(delta)
    return plan

def note_overlay(plan,name,changed=False):
    pipeline=plan.setdefault('overlay_pipeline',{});applied=list(pipeline.get('applied') or [])
    if name not in applied:applied.append(name)
    pipeline['applied']=applied;pipeline['last_overlay']=name;pipeline['changed']=bool(changed)
    return plan

def validate_overlay_order(plan):
    applied=(plan.get('overlay_pipeline') or {}).get('applied') or [];positions={name:i for i,name in enumerate(OVERLAY_ORDER)}
    seen=[positions[name] for name in applied if name in positions]
    return seen==sorted(seen)
