"""Battery planning calculations owned by Controller policy.

The physical battery model is authoritative in Home Forecaster.  Controller
consumes sensor.home_energy_manager_battery_model and deliberately has no local,
generic, persisted, or last-known model fallback for charge-duration planning.
"""

from datetime import timezone

from controller_utils import as_float, clamp, parse_dt

SOC_BANDS=[(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,95),(95,98),(98,99),(99,100)]
MODEL_ENTITY='sensor.home_energy_manager_battery_model'


def soc_at(c,s,when,attr='forecast_no_slots'):
    best=None
    for a,b,_r,x in c.intervals(s,attr):
        v=as_float(x.get('soc'))
        if v is not None and a<=when: best=v
        if v is not None and (a<=when<b or abs((a-when).total_seconds())<=60): return v
    return best


def forecast_battery_kwh_between(c,s,start,end,attr='forecast_no_slots'):
    if end<=start:return 0.0
    total=0.0
    for a,b,_r,x in c.intervals(s,attr):
        oa=max(a,start); ob=min(b,end)
        if ob<=oa:continue
        frac=(ob-oa).total_seconds()/max(1e-9,(b-a).total_seconds())
        v=as_float(x.get('battery_kwh'))
        if v is not None:total+=v*frac
    return total


def planned_discharge_soc_adjustment(c,s,ds,de,rate_w,cap,reserve):
    if not ds or not de or de<=ds or rate_w<=0 or cap<=0:return 0.0
    ds=max(ds,c.now())
    if de<=ds:return 0.0
    forced=(de-ds).total_seconds()/3600*(rate_w/1000)
    baseline=max(0.0,forecast_battery_kwh_between(c,s,ds,de))
    stored=min(max(0.0,forced-baseline)/0.95,max(0.0,cap*(100-reserve)/100))
    return 100*stored/cap


def projected_charge_start_soc(c,s,when,adjust,reserve):
    base=soc_at(c,s,when)
    return None if base is None else clamp(base-adjust,reserve,100.0)


def validate_forecaster_model(attrs,now):
    """Validate the Home Forecaster battery-model contract used for planning."""
    if not isinstance(attrs,dict) or attrs.get('schema_version')!=1:return False,'schema_version',None
    stamp=parse_dt(attrs.get('generated_at'))
    if not stamp:return False,'generated_at',None
    age=(now.astimezone(timezone.utc)-stamp.astimezone(timezone.utc)).total_seconds()
    if age>900:return False,'stale',age
    if age < -60:return False,'future',age
    bands=attrs.get('bands')
    if not isinstance(bands,list) or len(bands)!=len(SOC_BANDS):return False,'bands',age
    try:
        factors={(float(b['soc_lo']),float(b['soc_hi'])):float(b['effective_factor']) for b in bands}
        top=float(attrs['top_completion_allowance_minutes']); generic=float(attrs['generic_top_completion_minutes'])
    except (KeyError,TypeError,ValueError):return False,'fields',age
    if set(factors)!=set((float(a),float(b)) for a,b in SOC_BANDS) or any(not .05<=x<=1 for x in factors.values()):return False,'band_values',age
    if generic<0 or top<generic:return False,'top_completion',age
    return True,'ok',age


async def refresh_forecaster_model(c):
    """Refresh the sole authoritative battery model for this planning pass."""
    previous=(getattr(c,'_battery_model_source',None),getattr(c,'_battery_model_unavailable_reason',None))
    attrs={}; age=None; reason=None
    try:
        st=await c.ha.state(MODEL_ENTITY)
        attrs=st.get('attributes',{}) if st else {}
        ok,why,age=validate_forecaster_model(attrs,c.now())
        if not ok:reason='forecaster_model_'+why
    except Exception as exc:
        ok=False; reason='forecaster_model_read_error'
        c.LOG.warning('Battery planning model read failed; no Controller fallback is permitted: %s',exc)
    if ok:
        c._battery_model_attrs=attrs
        c._battery_model_source='forecaster'
        c._battery_model_unavailable_reason=None
        c._battery_model_age_seconds=age
    else:
        c._battery_model_attrs=None
        c._battery_model_source='unavailable'
        c._battery_model_unavailable_reason=reason
        c._battery_model_age_seconds=age
    current=(c._battery_model_source,c._battery_model_unavailable_reason)
    if current!=previous:
        if c._battery_model_source=='forecaster':
            c.LOG.info('Battery planning model source: forecaster entity=%s age=%.1fs',MODEL_ENTITY,age)
        else:
            c.LOG.error('Battery planning model unavailable: reason=%s entity=%s; Controller will fail closed to safe/degraded control',reason,MODEL_ENTITY)
    return c._battery_model_source


def forecaster_charge_minutes(soc,target,rate,cap,attrs):
    """Calculate charge duration from a validated Home Forecaster battery model."""
    if rate<=0 or target<=soc:return 0.0
    factors={(float(b['soc_lo']),float(b['soc_hi'])):float(b['effective_factor']) for b in attrs['bands']}
    mins=0.0
    for lo,hi in SOC_BANDS:
        ov=max(0,min(target,hi)-max(soc,lo))
        if ov:mins+=(cap*(ov/100))/((rate/1000)*max(.05,factors[(float(lo),float(hi))]))*60
    if target>=100:mins+=float(attrs['top_completion_allowance_minutes'])
    return mins


def charge_minutes(c,soc,target,rate,cap):
    """Calculate duration using only the authoritative Home Forecaster model."""
    attrs=getattr(c,'_battery_model_attrs',None)
    if getattr(c,'_battery_model_source',None)!='forecaster' or not isinstance(attrs,dict):
        raise RuntimeError('authoritative Home Forecaster battery model unavailable')
    return forecaster_charge_minutes(soc,target,rate,cap,attrs)


def latest_charge_start_for_rate(c,s,w,target,rate,cap,reserve,adjust,earliest=None):
    margin=float(c.c.get('charge_safety_margin_minutes',10)); lo=max(w['start'],earliest or w['start']); hi=w['end']
    if hi<=lo:return lo,projected_charge_start_soc(c,s,lo,adjust,reserve),False
    def feasible(at):
        soc=projected_charge_start_soc(c,s,at,adjust,reserve)
        if soc is None:return False,None
        return charge_minutes(c,soc,target,rate,cap)+margin<=(w['end']-at).total_seconds()/60+1e-6,soc
    ok,soc=feasible(lo)
    if not ok:return lo,soc,False
    left,right=lo,hi; best=lo
    for _ in range(24):
        if (right-left).total_seconds()<=30:break
        mid=left+(right-left)/2; ok,_=feasible(mid)
        if ok:best=mid; left=mid
        else:right=mid
    best=best.replace(second=0,microsecond=0)
    return best,projected_charge_start_soc(c,s,best,adjust,reserve),True


def choose_rate_and_start(c,s,w,target,cap,reserve,adjust,hw,earliest=None):
    preferred=cap*1000*float(c.c.get('preferred_charge_c_rate',.25)); maximum=cap*1000*float(c.c.get('max_charge_c_rate',.4))
    if hw:maximum=min(maximum,hw)
    preferred=min(preferred,maximum); earliest=max(w['start'],earliest or w['start'])
    solve=lambda rate:latest_charge_start_for_rate(c,s,w,target,rate,cap,reserve,adjust,earliest)
    start,soc,ok=solve(preferred)
    if ok:return int(round(preferred)),start,soc,True
    if maximum<=preferred:
        start,soc,ok=solve(maximum); return int(round(maximum)),start,soc,ok
    max_start,max_soc,max_ok=solve(maximum)
    if not max_ok:return int(round(maximum)),earliest,max_soc,False
    lo,hi=preferred,maximum; best=(maximum,max_start,max_soc)
    for _ in range(24):
        mid=(lo+hi)/2; st,ss,ok=solve(mid)
        if ok:best=(mid,st,ss); hi=mid
        else:lo=mid
    return int(round(best[0])),best[1],best[2],True


def choose_rate(c,soc,target,cap,w,hw):
    preferred=cap*1000*float(c.c.get('preferred_charge_c_rate',.25)); maximum=cap*1000*float(c.c.get('max_charge_c_rate',.4)); maximum=min(maximum,hw) if hw else maximum; preferred=min(preferred,maximum)
    avail=(w['end']-w['start']).total_seconds()/60; margin=float(c.c.get('charge_safety_margin_minutes',10))
    fits=lambda rate:charge_minutes(c,soc,target,rate,cap)+margin<=avail
    if fits(preferred):return int(round(preferred))
    if maximum<=preferred or not fits(maximum):return int(round(maximum))
    lo,hi=preferred,maximum
    for _ in range(24):
        mid=(lo+hi)/2
        if fits(mid):hi=mid
        else:lo=mid
    return int(round(hi))
