"""Active controller overlay pipeline.

The legacy planner remains a compatibility oracle while Phase 5 separates its
observable policy deltas into ordered plan overlays. The underlying base mode
plan is calculated with event sources masked, then Power Down, Axle and EV Smart
Charging are layered in that order. No stage writes Home Assistant.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
import legacy_minimise_export_runtime as legacy_runtime
from controller_overlay_pipeline import apply_snapshot_overlay,note_overlay,validate_overlay_order
from minimise_export_headroom import highest_soc_without_avoidable_export
core=legacy_runtime.core
_LEGACY_POLICY_PLAN=core.Controller.plan
_AXLE_OVERLAY=legacy_runtime._apply_axle_overlay
async def _no_axle(controller,forecast,plan,window,capacity,reserve,max_charge,max_discharge):return plan
legacy_runtime._apply_axle_overlay=_no_axle
@asynccontextmanager
async def _masked_event_sources(controller,*,power_down=False,ev=False):
    saved={}
    def replace(name,value):saved[name]=(name in controller.__dict__,controller.__dict__.get(name));setattr(controller,name,value)
    if power_down:
        async def no_power_down(*_args,**_kwargs):return None
        replace('power_down_info',no_power_down)
    if ev:
        async def no_ev(*_args,**_kwargs):return {'enabled':False,'confirmed':False}
        async def no_planned(*_args,**_kwargs):return []
        replace('intelligent_go_info',no_ev);replace('intelligent_planned_slots',no_planned)
    try:yield
    finally:
        for name,(had,value) in saved.items():
            if had:setattr(controller,name,value)
            else:controller.__dict__.pop(name,None)
async def _snapshot(controller,forecast,soc,window,fallback,*,mask_power_down=False,mask_ev=False):
    async with _masked_event_sources(controller,power_down=mask_power_down,ev=mask_ev):return await _LEGACY_POLICY_PLAN(controller,forecast,soc,window,fallback=fallback)

def _solar_headroom_target(controller,forecast,window,capacity,reserve,max_charge,max_discharge):
    active=legacy_runtime._current_regular_offpeak(controller,window)
    if active:
        bridge_start=active['end'];next_offpeak_start=window['start']
    else:
        bridge_start=window['end'];next_offpeak_start=legacy_runtime._shift_local_day(controller,window['start'],1)
    segments=controller.forecast_net_segments(forecast,bridge_start,next_offpeak_start,'forecast_no_slots')
    covered=sum(max(0.0,(b-a).total_seconds()) for a,b,_load,_pv in segments)
    wanted=max(0.0,(next_offpeak_start-bridge_start).total_seconds())
    complete=covered>=wanted-1.0 and bool(segments)
    if not complete:
        return 100,{
            'solar_headroom_target_soc':100,
            'solar_headroom_forecast_complete':False,
            'solar_headroom_strategy':'incomplete_forecast_fail_safe_full',
            'solar_headroom_bridge_start':core.iso(bridge_start),
            'solar_headroom_next_offpeak_start':core.iso(next_offpeak_start),
        }
    target,diag=highest_soc_without_avoidable_export(
        segments,float(capacity),float(reserve),float(max_charge),float(max_discharge)
    )
    return int(target),{
        'solar_headroom_target_soc':int(target),
        'solar_headroom_forecast_complete':True,
        'solar_headroom_strategy':diag.get('strategy'),
        'solar_headroom_avoidable_export_kwh':diag.get('avoidable_export_kwh'),
        'solar_headroom_bridge_start':core.iso(bridge_start),
        'solar_headroom_next_offpeak_start':core.iso(next_offpeak_start),
    }

@asynccontextmanager
async def _minimise_export_solar_floor(controller,forecast,window):
    original=controller.c.get('minimise_export_min_soc',25)
    diagnostics=None
    if controller.operation_mode()=='minimise_export':
        capacity,_=await controller.num('battery_capacity_entity','battery_capacity',True);reserve,_=await controller.num('battery_reserve_entity','battery_reserve',True)
        max_charge,_=await controller.num('inverter_max_charge_rate_entity','max_charge_rate',False);max_discharge,_=await controller.num('inverter_max_discharge_rate_entity','max_discharge_rate',False)
        if None in (capacity,reserve,max_charge,max_discharge) or capacity<=0 or max_charge<=0 or max_discharge<=0:
            solar_target=100
            diagnostics={'solar_headroom_target_soc':100,'solar_headroom_forecast_complete':False,'solar_headroom_strategy':'missing_battery_inputs_fail_safe_full'}
        else:
            solar_target,diagnostics=_solar_headroom_target(controller,forecast,window,capacity,reserve,max_charge,max_discharge)
        diagnostics['configured_floor_soc']=int(round(float(original)))
        controller.c['minimise_export_min_soc']=max(float(original),float(solar_target))
        core.LOG.info('Minimise export solar headroom: configured_floor=%s%% solar_target=%s%% strategy=%s',original,solar_target,diagnostics.get('solar_headroom_strategy'))
    try:yield diagnostics
    finally:controller.c['minimise_export_min_soc']=original

async def _plan_with_explicit_overlays(self,forecast,soc,window,fallback=False):
    if fallback:return await _LEGACY_POLICY_PLAN(self,forecast,soc,window,fallback=True)
    async with _minimise_export_solar_floor(self,forecast,window) as solar_diag:
        base=await _snapshot(self,forecast,soc,window,False,mask_power_down=True,mask_ev=True)
        if base is None:return None
        plan=deepcopy(base)
        power_down_snapshot=await _snapshot(self,forecast,soc,window,False,mask_ev=True)
        if power_down_snapshot is None:return None
        plan=apply_snapshot_overlay(plan,power_down_snapshot,'power_down')
        mode=self.operation_mode();axle_before=deepcopy(plan)
        if mode in ('minimise_export','maximise_export','export_generated'):
            capacity,_=await self.num('battery_capacity_entity','battery_capacity',True);reserve,_=await self.num('battery_reserve_entity','battery_reserve',True)
            max_charge,_=await self.num('inverter_max_charge_rate_entity','max_charge_rate',False);max_discharge,_=await self.num('inverter_max_discharge_rate_entity','max_discharge_rate',False)
            if None not in (capacity,reserve,max_charge,max_discharge) and capacity>0 and max_charge>0 and max_discharge>0:plan=await _AXLE_OVERLAY(self,forecast,plan,window,float(capacity),float(reserve),float(max_charge),float(max_discharge))
        note_overlay(plan,'axle',changed=plan!=axle_before)
        full_snapshot=await _snapshot(self,forecast,soc,window,False)
        if full_snapshot is None:return None
        plan=apply_snapshot_overlay(plan,full_snapshot,'ev_smart_charging',baseline=power_down_snapshot)
        if solar_diag is not None:
            minimise=plan.setdefault('minimise_export',{})
            final_target=max(int(minimise.get('required_target_soc') or 0),int(solar_diag['solar_headroom_target_soc']),int(solar_diag['configured_floor_soc']))
            minimise.update(solar_diag);minimise['required_target_soc']=final_target;minimise['strategy']='avoid_peak_import_and_preserve_pv_headroom'
        if not validate_overlay_order(plan):raise RuntimeError('Controller overlay order invariant violated')
        plan['overlay_pipeline']['order']=['power_down','axle','ev_smart_charging'];plan['overlay_pipeline']['strategy']='base_plan_then_ordered_overlays'
        return plan
core.Controller.plan=_plan_with_explicit_overlays
