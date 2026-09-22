"""Apply controller plans to Home Assistant/inverter entities.

Planning produces a desired plan; this module owns translation of that plan into
mutable inverter fields. Individual verified writes still use ``ensure`` so the
existing retry, readback, audit and runtime charge-target suppression semantics
remain unchanged.
"""
import asyncio
from datetime import timedelta
from controller_utils import parse_dt, iso, as_float


def _positive_transfer(slot):
    start=parse_dt((slot or {}).get('start')); end=parse_dt((slot or {}).get('end')); planned=as_float((slot or {}).get('planned_kwh'))
    return start,end,planned is not None and planned>0 and start is not None and end is not None and end>start


def repair_expired_charge(controller, plan, log):
    """Neutralise an expired controller-owned charge instead of replaying its clock times."""
    charge=plan.get('charge') or {}; start,end,positive=_positive_transfer(charge)
    if not positive:return
    now=controller.now()
    try:start=start.astimezone(controller.tz); end=end.astimezone(controller.tz); now=now.astimezone(controller.tz)
    except Exception:return
    if now<end:return
    kind=str(charge.get('kind') or '')
    if not (kind.startswith('calibration_') or kind in ('regular','minimise_export','overnight')):return
    log.error('Missed controller charge window: kind=%s slot=%s-%s now=%s; disabling expired slot rather than replaying it',kind,start.strftime('%Y-%m-%d %H:%M'),end.strftime('%Y-%m-%d %H:%M'),now.strftime('%Y-%m-%d %H:%M'))
    anchor=end.replace(second=0,microsecond=0)
    charge['start']=iso(anchor); charge['end']=iso(anchor); charge['planned_kwh']=0.0
    if kind.startswith('calibration_'):charge['kind']='calibration_recharge_missed'


def resolve_pause_transfer_conflicts(controller, plan, pause, log):
    """Ensure a blocking pause can never coexist with a deliberate battery transfer.

    This is deliberately enforced at the final plan-to-inverter boundary so every
    planner (including calibration and future overlays) receives the same safety
    protection. Future regular/calibration charging also truncates PauseBoth or
    PauseCharge at charge start, avoiding reliance on an exact controller tick.
    """
    pause=dict(pause or {'mode':'Disabled','start':'00:00:00','end':'00:00:00'})
    mode=pause.get('mode') or 'Disabled'; now=controller.now()
    try:now=now.astimezone(controller.tz)
    except Exception:pass
    cstart,cend,cpositive=_positive_transfer(plan.get('charge'))
    dstart,dend,dpositive=_positive_transfer(plan.get('discharge'))
    if cstart:
        try:cstart=cstart.astimezone(controller.tz); cend=cend.astimezone(controller.tz)
        except Exception:pass
    if dstart:
        try:dstart=dstart.astimezone(controller.tz); dend=dend.astimezone(controller.tz)
        except Exception:pass

    if cpositive and cstart<=now<cend and mode in ('PauseCharge','PauseBoth'):
        log.warning('Battery action transition: %s -> Charge reason=%s slot=%s-%s; releasing blocking pause',mode,(plan.get('charge') or {}).get('kind') or 'planned_charge',cstart.strftime('%H:%M'),cend.strftime('%H:%M'))
        return {'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
    if dpositive and dstart<=now<dend and mode in ('PauseDischarge','PauseBoth'):
        log.warning('Battery action transition: %s -> Discharge reason=%s slot=%s-%s; releasing blocking pause',mode,(plan.get('discharge') or {}).get('kind') or 'planned_discharge',dstart.strftime('%H:%M'),dend.strftime('%H:%M'))
        return {'mode':'Disabled','start':'00:00:00','end':'00:00:00'}

    # Confirmed EV Smart Charging has its own explicit pause overlay and must not
    # be shortened merely because a separate future regular charge exists.
    confirmed=bool(plan.get('intelligent_go',{}).get('confirmed'))
    if cpositive and not confirmed and mode in ('PauseCharge','PauseBoth') and now<cstart:
        off=plan.get('offpeak') or {}; ostart=parse_dt(off.get('start')); oend=parse_dt(off.get('end'))
        try:ostart=ostart.astimezone(controller.tz) if ostart else None; oend=oend.astimezone(controller.tz) if oend else None
        except Exception:pass
        pause_end=str(pause.get('end') or '')
        if ostart and oend and ostart<=cstart<=oend and pause_end==controller.tstr(oend):
            # Only the full regular cheap-window preservation pause is shortened.
            # A deliberately shorter overlay is left untouched.
            if pause_end!=controller.tstr(cstart):
                log.info('Pause/charge invariant: truncating %s at planned charge start %s (was %s)',mode,controller.tstr(cstart),pause_end)
                pause['end']=controller.tstr(cstart)
    return pause


def repair_disabled_discharge(controller, plan, log):
    discharge=plan.get('discharge') or {}
    if discharge.get('kind')!='none':return
    start=parse_dt(discharge.get('start')); end=parse_dt(discharge.get('end'))
    if start is not None and end is not None and start==end:return
    offpeak={'start':parse_dt(plan['offpeak']['start']).astimezone(controller.tz),'end':parse_dt(plan['offpeak']['end']).astimezone(controller.tz),'rate_p':plan['offpeak']['rate_p']}
    disabled_at=controller.disabled_discharge_time(offpeak)
    log.warning('Correcting invalid disabled discharge slot before apply: start=%s end=%s -> %s-%s',discharge.get('start'),discharge.get('end'),disabled_at.strftime('%H:%M'),disabled_at.strftime('%H:%M'))
    plan['discharge']['start']=iso(disabled_at); plan['discharge']['end']=iso(disabled_at); plan['discharge']['planned_kwh']=0.0


async def charge_end_with_live_extension(controller, plan, log, step_minutes=5):
    """Extend a controller-owned cheap-rate charge in small live-SOC steps."""
    charge=plan.get('charge') or {}; offpeak=plan.get('offpeak') or {}
    start=parse_dt(charge.get('start')); end=parse_dt(charge.get('end')); off_end=parse_dt(offpeak.get('end'))
    target=as_float(charge.get('target_soc')); rate=as_float(charge.get('rate_w')); planned_kwh=as_float(charge.get('planned_kwh'))
    if not start or not end or not off_end or target is None or rate is None or rate<=0 or planned_kwh is None or planned_kwh<=0:return end
    now=controller.now()
    try:start=start.astimezone(controller.tz); end=end.astimezone(controller.tz); off_end=off_end.astimezone(controller.tz); now=now.astimezone(controller.tz)
    except Exception:return end
    safety_end=off_end-timedelta(minutes=max(0,float(controller.c.get('charge_safety_margin_minutes',10))))
    if not (start<=now<safety_end) or end>=safety_end:return end
    step=timedelta(minutes=max(1,int(step_minutes)))
    if now<end-step:return end
    st=await controller.ha.state(controller.c.get('battery_soc_entity','')); soc=as_float(st.get('state')) if st else None
    if soc is None or soc>=target-0.5:return end
    extended=min(safety_end,max(end+step,now+step))
    if extended<=end:return end
    log.info('Cheap-rate charge last-mile extension: SOC %.1f%% < logical target %.1f%%; end %s -> %s (safety_end=%s offpeak_end=%s)',soc,target,end.strftime('%H:%M'),extended.strftime('%H:%M'),safety_end.strftime('%H:%M'),off_end.strftime('%H:%M'))
    return extended


async def locked_minimise_charge_slot(controller, plan, log):
    """Keep an already-running regular Minimise Export charge slot unchanged.

    This reads the inverter's actual active schedule/rate, not a fresh forecast.
    Explicit priority interventions must remain free to replace that schedule.
    """
    if (plan.get('operation') or {}).get('mode') != 'minimise_export':
        return None
    if (plan.get('intelligent_go') or {}).get('confirmed'):
        return None
    if (plan.get('calibration') or {}).get('state') not in ('disabled', 'normal'):
        return None
    if (plan.get('axle') or {}).get('action') not in (None, 'ignored'):
        return None
    if plan.get('power_down'):
        return None
    offpeak = plan.get('offpeak') or {}
    off_start = parse_dt(offpeak.get('start'))
    off_end = parse_dt(offpeak.get('end'))
    if not off_start or not off_end or not (off_start <= controller.now() < off_end):
        return None
    charge = plan.get('charge') or {}
    if str(charge.get('kind') or '') not in ('', 'regular', 'minimise_export', 'overnight'):
        return None
    start_state = await controller.ha.state(controller.c['charge_slot_1_start_entity'])
    end_state = await controller.ha.state(controller.c['charge_slot_1_end_entity'])
    rate_state = await controller.ha.state(controller.c['charge_rate_entity'])
    start = str((start_state or {}).get('state') or '')
    end = str((end_state or {}).get('state') or '')
    rate = as_float((rate_state or {}).get('state'))
    if rate is None or rate <= 0 or not controller.daily_slot_active(start, end):
        return None
    # Only lock the normal slot that ends at the regular cheap-rate boundary.
    # A distinct active charge schedule must remain manageable by its owner.
    if end != controller.tstr(off_end):
        return None
    log.info('Preserving active minimise-export charge slot: start=%s end=%s rate=%.0fW; ignoring routine replan start=%s end=%s rate=%sW',
             start, end, rate, charge.get('start'), charge.get('end'), charge.get('rate_w'))
    return start, end, int(round(rate))


async def desired_inverter_fields(controller, plan, log):
    """Translate a calculated plan to desired inverter fields without writing."""
    repair_disabled_discharge(controller,plan,log); repair_expired_charge(controller,plan,log)
    wend=parse_dt(plan['offpeak']['end']); window={'start':parse_dt(plan['offpeak']['start']).astimezone(controller.tz),'end':wend.astimezone(controller.tz),'rate_p':plan['offpeak']['rate_p']}
    pause=plan.get('pause') or controller.pause_plan(window)
    if plan.get('intelligent_go',{}).get('confirmed'):
        ig=plan['intelligent_go']; ia=parse_dt(ig.get('slot_start')); ib=parse_dt(ig.get('slot_end')); mode=ig.get('pause_mode') or 'Disabled'
        if mode in ('PauseDischarge','PauseBoth') and ia and ib:pause={'mode':mode,'start':controller.tstr(ia),'end':controller.tstr(ib)}
        elif mode=='PauseCharge':pause=plan.get('pause') or pause
        else:pause={'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
    pause=resolve_pause_transfer_conflicts(controller,plan,pause,log)
    pause_start=await controller.preserve_active_slot_start('pause',controller.c['pause_start_entity'],controller.c['pause_end_entity'],pause['start'])
    locked_charge=await locked_minimise_charge_slot(controller,plan,log)
    if locked_charge is not None:
        charge_start,charge_end_time,charge_rate=locked_charge
        charge_end=parse_dt(plan['offpeak']['end'])
    else:
        charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],controller.tstr(parse_dt(plan['charge']['start'])))
        charge_end=await charge_end_with_live_extension(controller,plan,log)
        charge_end_time=controller.tstr(charge_end)
        charge_rate=plan['charge']['rate_w']
    discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['start'])))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],wend),('pause_start',controller.c['pause_start_entity'],pause_start,wend),('pause_end',controller.c['pause_end_entity'],pause['end'],wend),('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,wend),('charge_end',controller.c['charge_slot_1_end_entity'],charge_end_time,wend),('charge_target',controller.c['charge_slot_1_target_entity'],plan['charge']['target_soc'],wend),('charge_rate',controller.c['charge_rate_entity'],charge_rate,wend),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,parse_dt(plan['offpeak']['start'])),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['end'])),parse_dt(plan['offpeak']['start'])),('discharge_rate',controller.c['discharge_rate_entity'],plan['discharge']['rate_w'],parse_dt(plan['offpeak']['start']))]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),parse_dt(plan['offpeak']['start'])))
    return fields


async def apply_plan(controller, plan, log):
    apply_started=asyncio.get_running_loop().time(); controller.confirmed_writes_this_apply=0; fields=await desired_inverter_fields(controller,plan,log); results=[]
    for item in fields:
        field_started=asyncio.get_running_loop().time(); result=await controller.ensure(*item); results.append(result); elapsed=asyncio.get_running_loop().time()-field_started
        if elapsed>=1.0:log.info('Apply timing: field=%s elapsed=%.3fs result=%s',item[0],elapsed,'ok' if result else 'failed')
    log.info('Apply timing: total=%.3fs fields=%d writes_confirmed=%d success=%s',asyncio.get_running_loop().time()-apply_started,len(fields),controller.confirmed_writes_this_apply,'yes' if all(results) else 'no'); return all(results)


async def apply_safe_fallback(controller, window, cap=None, hardware_max_charge_w=None):
    """Apply the existing conservative fallback through the write boundary."""
    now=controller.now(); export_start=controller.export_start(window) if window else now.replace(second=0,microsecond=0); pause=controller.pause_plan(window)
    pause_start=await controller.preserve_active_slot_start('pause',controller.c['pause_start_entity'],controller.c['pause_end_entity'],pause['start']); discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],window['end'] if window else None),('pause_start',controller.c['pause_start_entity'],pause_start,window['end'] if window else None),('pause_end',controller.c['pause_end_entity'],pause['end'],window['end'] if window else None),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,window['end'] if window else None),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start),window['end'] if window else None)]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),window['end'] if window else None))
    if window and cap:
        charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],controller.tstr(window['start'])); rate=cap*1000*float(controller.c.get('preferred_charge_c_rate',.25)); rate=min(rate,hardware_max_charge_w) if hardware_max_charge_w else rate
        fields += [('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,window['end']),('charge_end',controller.c['charge_slot_1_end_entity'],controller.tstr(window['end']),window['end']),('charge_target',controller.c['charge_slot_1_target_entity'],100,window['end']),('charge_rate',controller.c['charge_rate_entity'],int(round(rate)),window['end'])]
    return all([await controller.ensure(*item) for item in fields])
