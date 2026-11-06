"""Apply controller plans to Home Assistant/inverter entities.

Planning produces a desired plan; this module owns translation of that plan into
mutable inverter fields. Individual verified writes still use ``ensure`` so the
existing retry, readback, audit and runtime charge-target suppression semantics
remain unchanged.
"""
import asyncio
from datetime import datetime, time, timedelta, timezone
from controller_utils import parse_dt, iso, as_float
from controller_minimise_offpeak import active_or_next_offpeak


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


async def locked_minimise_charge_slot(controller, plan, log):
    """Keep an already-running regular Minimise Export charge slot unchanged.

    This reads the inverter's actual active schedule/rate, not a fresh forecast.
    Explicit priority interventions must remain free to replace that schedule.
    """
    if (plan.get('operation') or {}).get('mode') != 'minimise_export':
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
    if not off_start or not off_end:
        return None
    # The forecast usually supplies the NEXT overnight window, while the
    # minimise-export coordinator may already be charging in the CURRENT one.
    # Use its identical window selection rather than comparing dates from
    # different nights (which previously unlocked the live rate and times).
    active_window, active = active_or_next_offpeak(
        controller, {'start': off_start, 'end': off_end, 'rate_p': offpeak.get('rate_p')}
    )
    if not active:
        return None
    off_end = active_window['end']
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
    # Recognise the already-running regular charge by its observed timestamps,
    # not by matching a mutable tariff-derived end clock. Otherwise an EV
    # discount extending the forecast cheap period unlocks and interrupts it.
    try:
        now = controller.now().astimezone(controller.tz)
        observed_start = datetime.combine(now.date(), time.fromisoformat(start), controller.tz)
        if observed_start.astimezone(timezone.utc) > now.astimezone(timezone.utc):
            observed_start -= timedelta(days=1)
        observed_end = datetime.combine(now.date(), time.fromisoformat(end), controller.tz)
        if observed_end.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            observed_end += timedelta(days=1)
    except ValueError:
        return None
    if not (active_window['start'].astimezone(timezone.utc) <= observed_start.astimezone(timezone.utc)
            and observed_end.astimezone(timezone.utc) <= off_end.astimezone(timezone.utc)):
        return None
    # An explicit EV battery top-up outside regular off-peak remains manageable.
    if (plan.get('intelligent_go') or {}).get('intelligent_charge_target_soc') is not None:
        return None
    log.info('Preserving active minimise-export charge slot: start=%s end=%s rate=%.0fW; ignoring routine replan start=%s end=%s rate=%sW',
             start, end, rate, charge.get('start'), charge.get('end'), charge.get('rate_w'))
    return start, end, int(round(rate))


async def desired_inverter_fields(controller, plan, log):
    """Translate a calculated plan to desired inverter fields without writing."""
    repair_disabled_discharge(controller,plan,log); repair_expired_charge(controller,plan,log)
    # One-off EV slots must be current, with an end strictly after the current
    # instant. Do not program stale/repeating inverter clock times.
    ig=plan.get('intelligent_go') or {}
    if ig.get('confirmed') and ig.get('intelligent_charge_target_soc') is not None:
        charge=plan.get('charge') or {}
        cs=parse_dt(charge.get('start'));ce=parse_dt(charge.get('end'))
        now=controller.now()
        if not cs or not ce or cs.astimezone(timezone.utc)>=ce.astimezone(timezone.utc) or ce.astimezone(timezone.utc)<=now.astimezone(timezone.utc):
            log.error('EV smart charging: rejecting stale/invalid top-up start=%s end=%s now=%s',cs,ce,now)
            charge['start']=charge['end']=iso(now.replace(second=0,microsecond=0))
            charge['planned_kwh']=0.0
            ig['intelligent_charge_target_soc']=None
            ig['charge_reason']='invalid_or_expired_smart_slot'
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
    else:
        charge_end=parse_dt(plan['charge']['end'])
        # Never extend a regular charge outside its actual cheap-rate window.
        # EV-confirmed periods outside regular off-peak are separate overlays.
        if (plan.get('operation') or {}).get('mode') == 'minimise_export':
            active_window, active = active_or_next_offpeak(controller, window)
            charge_start_dt = parse_dt((plan.get('charge') or {}).get('start'))
            if (active and charge_start_dt and
                    active_window['start'] <= charge_start_dt < active_window['end'] and
                    charge_end > active_window['end']):
                log.warning('Capping regular charge end at current cheap-rate cutoff: %s -> %s',
                            iso(charge_end), iso(active_window['end']))
                charge_end = active_window['end']
        charge_end_time=controller.tstr(charge_end)
        desired_start=controller.tstr(parse_dt(plan['charge']['start']))
        observed_end_state=await controller.ha.state(controller.c['charge_slot_1_end_entity'])
        observed_end=str((observed_end_state or {}).get('state') or '')
        # An active EV half-hour is not an overnight charging session.
        if observed_end==charge_end_time:
            charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],desired_start)
        else:
            charge_start=desired_start
        charge_rate=plan['charge']['rate_w']
    discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['start'])))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],wend),('pause_start',controller.c['pause_start_entity'],pause_start,wend),('pause_end',controller.c['pause_end_entity'],pause['end'],wend),('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,wend),('charge_end',controller.c['charge_slot_1_end_entity'],charge_end_time,wend),('charge_target',controller.c['charge_slot_1_target_entity'],plan['charge']['target_soc'],wend),('charge_rate',controller.c['charge_rate_entity'],charge_rate,wend),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,parse_dt(plan['offpeak']['start'])),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(parse_dt(plan['discharge']['end'])),parse_dt(plan['offpeak']['start'])),('discharge_rate',controller.c['discharge_rate_entity'],plan['discharge']['rate_w'],parse_dt(plan['offpeak']['start']))]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),parse_dt(plan['offpeak']['start'])))
    return fields


async def apply_plan(controller, plan, log):
    apply_started=asyncio.get_running_loop().time(); controller.confirmed_writes_this_apply=0; fields=await desired_inverter_fields(controller,plan,log); results=[]
    # Charge selectors are written individually. Neutralise an active charge
    # before replacing its window; re-arm only after both values are confirmed.
    field_map={item[0]:item for item in fields}
    current_start=await controller.ha.state(controller.c['charge_slot_1_start_entity'])
    current_end=await controller.ha.state(controller.c['charge_slot_1_end_entity'])
    active=controller.daily_slot_active(str((current_start or {}).get('state') or ''),str((current_end or {}).get('state') or ''))
    changed=(str((current_start or {}).get('state') or '')!=str(field_map['charge_start'][2]) or
             str((current_end or {}).get('state') or '')!=str(field_map['charge_end'][2]))
    if active and changed:
        enabled=await controller.ha.state(controller.c['charge_schedule_enable_entity'])
        if str((enabled or {}).get('state') or '').lower() in ('on','true','1'):
            if not await controller.ensure('charge_enable',controller.c['charge_schedule_enable_entity'],'off',None):
                log.error('Cannot safely replace active charge slot: failed to disable charging')
                return False
            fields=[item for item in fields if item[0]!='charge_enable']+[field_map['charge_enable']]
    for item in fields:
        if item[0]=='charge_enable' and item[2]=='on' and not all(results):
            log.error('Keeping charge schedule disabled: one or more inverter writes failed')
            return False
        field_started=asyncio.get_running_loop().time(); result=await controller.ensure(*item); results.append(result); elapsed=asyncio.get_running_loop().time()-field_started
        if elapsed>=1.0:log.info('Apply timing: field=%s elapsed=%.3fs result=%s',item[0],elapsed,'ok' if result else 'failed')
    log.info('Apply timing: total=%.3fs fields=%d writes_confirmed=%d success=%s',asyncio.get_running_loop().time()-apply_started,len(fields),controller.confirmed_writes_this_apply,'yes' if all(results) else 'no'); return all(results)


def observed_charge_within_offpeak(start_text, end_text, window, tz):
    """Validate inverter clock selectors within supplier regular cheap wall times.

    This is a schedule-preservation guard, never a substitute battery model.
    The inverter exposes HH:MM selectors rather than offset-aware instants.
    """
    try:
        from datetime import datetime, time
        start = time.fromisoformat(str(start_text))
        end = time.fromisoformat(str(end_text))
        ws = window['start'].astimezone(tz).time().replace(tzinfo=None)
        we = window['end'].astimezone(tz).time().replace(tzinfo=None)
        a = start.hour * 60 + start.minute
        b = end.hour * 60 + end.minute
        lo = ws.hour * 60 + ws.minute
        hi = we.hour * 60 + we.minute
        if a == b or a == hi or b == lo:
            return False
        # Compare clock boundaries in an unwrapped overnight timeline.
        if hi <= lo:
            hi += 1440
            if a < lo:
                a += 1440
            if b <= lo:
                b += 1440
        return lo <= a < b <= hi
    except (ValueError, TypeError, AttributeError, KeyError):
        return False


async def apply_safe_fallback(controller, window, cap=None, hardware_max_charge_w=None):
    """Apply the existing conservative fallback through the write boundary."""
    now=controller.now(); export_start=controller.export_start(window) if window else now.replace(second=0,microsecond=0); pause=controller.pause_plan(window)
    pause_start=await controller.preserve_active_slot_start('pause',controller.c['pause_start_entity'],controller.c['pause_end_entity'],pause['start']); discharge_start=await controller.preserve_active_slot_start('discharge',controller.c['discharge_slot_1_start_entity'],controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start))
    fields=[('eco',controller.c['eco_mode_entity'],'on',None),('charge_enable',controller.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',controller.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',controller.c['pause_mode_entity'],pause['mode'],window['end'] if window else None),('pause_start',controller.c['pause_start_entity'],pause_start,window['end'] if window else None),('pause_end',controller.c['pause_end_entity'],pause['end'],window['end'] if window else None),('discharge_start',controller.c['discharge_slot_1_start_entity'],discharge_start,window['end'] if window else None),('discharge_end',controller.c['discharge_slot_1_end_entity'],controller.tstr(export_start),window['end'] if window else None)]
    reserve,_=await controller.num('battery_reserve_entity','battery_reserve',False)
    if reserve is not None:fields.append(('discharge_target',controller.c['discharge_slot_1_target_entity'],int(round(reserve)),window['end'] if window else None))
    if window and cap:
        # A transient invalid forecaster model must not erase a valid delayed
        # charge schedule. Without that model we cannot safely recompute start
        # or rate. Still perform other degraded safety writes above.
        observed_start=await controller.ha.state(controller.c['charge_slot_1_start_entity'])
        observed_end=await controller.ha.state(controller.c['charge_slot_1_end_entity'])
        start_text=(observed_start or {}).get('state')
        end_text=(observed_end or {}).get('state')
        if observed_charge_within_offpeak(start_text,end_text,window,controller.tz):
            controller.LOG.warning(
                'Battery model unavailable: preserving verified inverter charge window %s-%s and charge rate; no substitute model used',
                start_text,end_text)
        else:
            charge_start=await controller.preserve_active_slot_start('charge',controller.c['charge_slot_1_start_entity'],controller.c['charge_slot_1_end_entity'],controller.tstr(window['start']))
            rate=cap*1000*float(controller.c.get('preferred_charge_c_rate',.25))
            rate=min(rate,hardware_max_charge_w) if hardware_max_charge_w else rate
            controller.LOG.warning(
                'Battery model unavailable and inverter charge schedule not verified (%s-%s): using conservative full regular cheap window',
                start_text,end_text)
            fields += [('charge_start',controller.c['charge_slot_1_start_entity'],charge_start,window['end']),('charge_end',controller.c['charge_slot_1_end_entity'],controller.tstr(window['end']),window['end']),('charge_rate',controller.c['charge_rate_entity'],int(round(rate)),window['end'])]
    return all([await controller.ensure(*item) for item in fields])
