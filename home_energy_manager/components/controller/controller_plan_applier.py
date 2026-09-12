"""Apply controller plans to Home Assistant/inverter entities.

Planning produces a desired plan; this module owns translation of that plan into
mutable inverter fields. Individual verified writes still use ``ensure`` so the
existing retry, readback, audit and runtime charge-target suppression semantics
remain unchanged.
"""
import asyncio
from controller_utils import parse_dt, iso


def repair_disabled_discharge(controller, plan, log):
    discharge=plan.get('discharge') or {}
    if discharge.get('kind')!='none':return
    start=parse_dt(discharge.get('start')); end=parse_dt(discharge.get('end'))
    if start is not None and end is not None and start==end:return
    offpeak={'start':parse_dt(plan['offpeak']['start']).astimezone(controller.tz),'end':parse_dt(plan['offpeak']['end']).astimezone(controller.tz),'rate_p':plan['offpeak']['rate_p']}
    disabled_at=controller.disabled_discharge_time(offpeak)
    log.warning('Correcting invalid disabled discharge slot before apply: start=%s end=%s -> %s-%s',discharge.get('start'),discharge.get('end'),disabled_at.strftime('%H:%M'),disabled_at.strftime('%H:%M'))
    plan['discharge']['start']=iso(disabled_at); plan['discharge']['end']=iso(disabled_at); plan['discharge']['planned_kwh']=0.0


async def desired_inverter_fields(controller, plan, log):
    """Translate a calculated plan to desired inverter fields without writing."""
    repair_disabled_discharge(controller,plan,log)
    wend=parse_dt(plan['offpeak']['end'])
    window={'start':parse_dt(plan['offpeak']['start']).astimezone(controller.tz),'end':wend.astimezone(controller.tz),'rate_p':plan['offpeak']['rate_p']}
    pause=plan.get('pause') or controller.pause_plan(window)
    if plan.get('intelligent_go',{}).get('confirmed'):
        ig=plan['intelligent_go']; ia=parse_dt(ig.get('slot_start')); ib=parse_dt(ig.get('slot_end')); mode=ig.get('pause_mode') or 'Disabled'
        if mode=='PauseDischarge' and ia and ib:pause={'mode':'PauseDischarge','start':controller.tstr(ia),'end':controller.tstr(ib)}
        elif mode=='PauseCharge':pause=plan.get('pause') or pause
        else:pause={'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
    pause_start=await controller.preserve_active_slot_start('pause',controller.c['pause_start_entity'],controller.c['pause_end_entity'],pause['start'])
    charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],controller.tstr(parse_dt(plan['charge']['start'])))
    discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['start'])))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],wend),('pause_start',controller.c['pause_start_entity'],pause_start,wend),('pause_end',controller.c['pause_end_entity'],pause['end'],wend),('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,wend),('charge_end',controller.c['charge_slot_1_end_entity'],controller.tstr(parse_dt(plan['charge']['end'])),wend),('charge_target',controller.c['charge_slot_1_target_entity'],plan['charge']['target_soc'],wend),('charge_rate',controller.c['charge_rate_entity'],plan['charge']['rate_w'],wend),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,parse_dt(plan['offpeak']['start'])),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['end'])),parse_dt(plan['offpeak']['start'])),('discharge_rate',controller.c['discharge_rate_entity'],plan['discharge']['rate_w'],parse_dt(plan['offpeak']['start']))]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),parse_dt(plan['offpeak']['start'])))
    return fields


async def apply_plan(controller, plan, log):
    apply_started=asyncio.get_running_loop().time(); controller.confirmed_writes_this_apply=0
    fields=await desired_inverter_fields(controller,plan,log); results=[]
    for item in fields:
        field_started=asyncio.get_running_loop().time(); result=await controller.ensure(*item); results.append(result); elapsed=asyncio.get_running_loop().time()-field_started
        if elapsed>=1.0:log.info('Apply timing: field=%s elapsed=%.3fs result=%s',item[0],elapsed,'ok' if result else 'failed')
    log.info('Apply timing: total=%.3fs fields=%d writes_confirmed=%d success=%s',asyncio.get_running_loop().time()-apply_started,len(fields),controller.confirmed_writes_this_apply,'yes' if all(results) else 'no')
    return all(results)


async def apply_safe_fallback(controller, window, cap=None, hardware_max_charge_w=None):
    """Apply the existing conservative fallback through the write boundary."""
    now=controller.now(); export_start=controller.export_start(window) if window else now.replace(second=0,microsecond=0); pause=controller.pause_plan(window)
    pause_start=await controller.preserve_active_slot_start('pause',controller.c['pause_start_entity'],controller.c['pause_end_entity'],pause['start'])
    discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],window['end'] if window else None),('pause_start',controller.c['pause_start_entity'],pause_start,window['end'] if window else None),('pause_end',controller.c['pause_end_entity'],pause['end'],window['end'] if window else None),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,window['end'] if window else None),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start),window['end'] if window else None)]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),window['end'] if window else None))
    if window and cap:
        charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],controller.tstr(window['start']))
        rate=cap*1000*float(controller.c.get('preferred_charge_c_rate',.25)); rate=min(rate,hardware_max_charge_w) if hardware_max_charge_w else rate
        fields += [('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,window['end']),('charge_end',controller.c['charge_slot_1_end_entity'],controller.tstr(window['end']),window['end']),('charge_target',controller.c['charge_slot_1_target_entity'],100,window['end']),('charge_rate',controller.c['charge_rate_entity'],int(round(rate)),window['end'])]
    return all([await controller.ensure(*item) for item in fields])
