"""Explicit controller plan-overlay helpers.

Phase 5 keeps the existing legacy planner as a compatibility oracle for EV Smart
Charging and Power Down calculations while making overlay ordering explicit in
the active runtime.  A stage snapshot is compared with the preceding plan and
only the changed plan fields are applied.  The compatibility oracle can be
removed later once the base planner itself has been fully extracted.
"""

from copy import deepcopy

OVERLAY_ORDER = ('power_down', 'axle', 'ev_smart_charging')


def _merge_diff(before, after):
    """Return a nested mapping containing only values changed by ``after``."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = {}
        keys = set(before) | set(after)
        for key in keys:
            if key not in after:
                out[key] = None
                continue
            if key not in before:
                out[key] = deepcopy(after[key])
                continue
            child = _merge_diff(before[key], after[key])
            if child is not _UNCHANGED:
                out[key] = child
        return out if out else _UNCHANGED
    return _UNCHANGED if before == after else deepcopy(after)


class _Unchanged:
    pass


_UNCHANGED = _Unchanged()


def plan_diff(before, after):
    diff = _merge_diff(before or {}, after or {})
    return {} if diff is _UNCHANGED else diff


def _apply_diff(target, diff):
    for key, value in diff.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _apply_diff(target[key], value)
        else:
            target[key] = deepcopy(value)


def apply_snapshot_overlay(plan, staged_plan, name):
    """Apply only the staged plan delta and append lightweight diagnostics."""
    delta = plan_diff(plan, staged_plan)
    if delta:
        _apply_diff(plan, delta)
    pipeline = plan.setdefault('overlay_pipeline', {})
    applied = list(pipeline.get('applied') or [])
    if name not in applied:
        applied.append(name)
    pipeline['applied'] = applied
    pipeline['last_overlay'] = name
    pipeline['changed'] = bool(delta)
    return plan


def note_overlay(plan, name, changed=False):
    pipeline = plan.setdefault('overlay_pipeline', {})
    applied = list(pipeline.get('applied') or [])
    if name not in applied:
        applied.append(name)
    pipeline['applied'] = applied
    pipeline['last_overlay'] = name
    pipeline['changed'] = bool(changed)
    return plan


def validate_overlay_order(plan):
    """Return True when any recorded overlay order respects controller priority."""
    applied = (plan.get('overlay_pipeline') or {}).get('applied') or []
    positions = {name: index for index, name in enumerate(OVERLAY_ORDER)}
    seen = [positions[name] for name in applied if name in positions]
    return seen == sorted(seen)
