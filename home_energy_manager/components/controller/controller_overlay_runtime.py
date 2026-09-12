"""Active controller overlay pipeline.

The legacy planner remains a compatibility oracle while Phase 5 separates its
observable policy deltas into ordered plan overlays.  The underlying base mode
plan is calculated with event sources masked, then Power Down, Axle and EV Smart
Charging are layered in that order.  No stage writes Home Assistant.
"""

from contextlib import asynccontextmanager
from copy import deepcopy

import minimise_export_runtime as legacy_runtime
from controller_overlay_pipeline import apply_snapshot_overlay, note_overlay, validate_overlay_order

core = legacy_runtime.core
_LEGACY_POLICY_PLAN = core.Controller.plan
_AXLE_OVERLAY = legacy_runtime._apply_axle_overlay


async def _no_axle(controller, forecast, plan, window, capacity, reserve, max_charge, max_discharge):
    return plan


# Axle is now invoked explicitly below rather than from minimise_export_runtime.
legacy_runtime._apply_axle_overlay = _no_axle


@asynccontextmanager
async def _masked_event_sources(controller, *, power_down=False, ev=False):
    saved = {}

    def replace(name, value):
        saved[name] = (name in controller.__dict__, controller.__dict__.get(name))
        setattr(controller, name, value)

    if power_down:
        async def no_power_down(*_args, **_kwargs):
            return None
        replace('power_down_info', no_power_down)
    if ev:
        async def no_ev(*_args, **_kwargs):
            return {'enabled': False, 'confirmed': False}
        async def no_planned(*_args, **_kwargs):
            return []
        replace('intelligent_go_info', no_ev)
        replace('intelligent_planned_slots', no_planned)
    try:
        yield
    finally:
        for name, (had_instance_value, value) in saved.items():
            if had_instance_value:
                setattr(controller, name, value)
            else:
                controller.__dict__.pop(name, None)


async def _snapshot(controller, forecast, soc, window, fallback, *, mask_power_down=False, mask_ev=False):
    async with _masked_event_sources(controller, power_down=mask_power_down, ev=mask_ev):
        return await _LEGACY_POLICY_PLAN(controller, forecast, soc, window, fallback=fallback)


async def _plan_with_explicit_overlays(self, forecast, soc, window, fallback=False):
    if fallback:
        return await _LEGACY_POLICY_PLAN(self, forecast, soc, window, fallback=True)

    # Base mode/calibration/minimise-export plan: no paid-event or EV overlay.
    base = await _snapshot(self, forecast, soc, window, False, mask_power_down=True, mask_ev=True)
    if base is None:
        return None
    plan = deepcopy(base)

    # Power Down overlay. EV remains masked so the delta belongs only to the
    # joined paid session and its preparation/protection requirements.
    power_down_snapshot = await _snapshot(self, forecast, soc, window, False, mask_ev=True)
    if power_down_snapshot is None:
        return None
    plan = apply_snapshot_overlay(plan, power_down_snapshot, 'power_down')

    # Axle overlay follows the established paid-event plan and precedes EV. The
    # Axle implementation itself still checks confirmed EV charging so the hard
    # "never export during charging" rule remains enforced even at this stage.
    mode = self.operation_mode()
    axle_before = deepcopy(plan)
    if mode in ('minimise_export', 'maximise_export', 'export_generated'):
        capacity, _ = await self.num('battery_capacity_entity', 'battery_capacity', True)
        reserve, _ = await self.num('battery_reserve_entity', 'battery_reserve', True)
        max_charge, _ = await self.num('inverter_max_charge_rate_entity', 'max_charge_rate', False)
        max_discharge, _ = await self.num('inverter_max_discharge_rate_entity', 'max_discharge_rate', False)
        if None not in (capacity, reserve, max_charge, max_discharge) and capacity > 0 and max_charge > 0 and max_discharge > 0:
            plan = await _AXLE_OVERLAY(
                self, forecast, plan, window,
                float(capacity), float(reserve), float(max_charge), float(max_discharge),
            )
    note_overlay(plan, 'axle', changed=plan != axle_before)

    # EV Smart Charging is last. Compare the compatibility oracle with/without
    # EV while Power Down is enabled, then apply only that delta to the current
    # plan. Thus EV can pre-empt a discharge without erasing unrelated Axle data.
    full_snapshot = await _snapshot(self, forecast, soc, window, False)
    if full_snapshot is None:
        return None
    plan = apply_snapshot_overlay(plan, full_snapshot, 'ev_smart_charging')

    if not validate_overlay_order(plan):
        raise RuntimeError('Controller overlay order invariant violated')
    plan['overlay_pipeline']['order'] = ['power_down', 'axle', 'ev_smart_charging']
    plan['overlay_pipeline']['strategy'] = 'base_plan_then_ordered_overlays'
    return plan


core.Controller.plan = _plan_with_explicit_overlays
