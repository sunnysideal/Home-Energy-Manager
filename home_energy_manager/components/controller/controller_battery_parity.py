"""Read-only battery-model migration diagnostics. No control decisions use this module."""
from datetime import timezone
from controller_battery import GENERIC, SOC_BANDS
from controller_utils import as_float, iso, parse_dt

MODEL_ENTITY='sensor.home_energy_manager_battery_model'
SEED_ENTITY='sensor.home_energy_manager_battery_model_seed'
PARITY_ENTITY='sensor.home_energy_manager_battery_model_parity'


def seed_attributes(c):
    bands=[]
    for lo,hi in SOC_BANDS:
        r=c.db.conn.execute('SELECT * FROM learned_bands WHERE band_lo=? AND band_hi=?',(lo,hi)).fetchone() if c.db.ok else None
        learned=as_float(r['learned_factor']) if r else None
        confidence=max(0.0,min(1.0,float(r['confidence'] or 0))) if r else 0.0
        generic=GENERIC[(lo,hi)]
        effective=generic*(1-confidence)+learned*confidence if learned is not None else generic
        bands.append({'soc_lo':lo,'soc_hi':hi,'generic_factor':generic,'learned_factor':learned,'confidence':confidence,'effective_factor':effective,'samples':int(r['obs_count'] or 0) if r else 0,'full_equiv':float(r['full_equiv'] or 0) if r else 0,'p10':as_float(r['p10']) if r else None,'p90':as_float(r['p90']) if r else None,'updated_at':r['updated_at'] if r else None})
    return {'friendly_name':'Home Energy Manager Battery Model Seed','schema_version':1,'generated_at':iso(c.now()),'source':'controller_migration_seed','bands':bands,'top_completion_allowance_minutes':float(c.dwell()),'generic_top_completion_minutes':float(c.c.get('generic_dwell_minutes',15)),'top_completion':{'attempts':int(c.db.get('top_completion_attempts') or 0) if c.db.ok else 0,'successes':int(c.db.get('top_completion_successes') or 0) if c.db.ok else 0,'misses':int(c.db.get('top_completion_misses') or 0) if c.db.ok else 0}}


async def publish_seed(c):
    attrs=seed_attributes(c)
    if not c.mqtt.publish_sensor(SEED_ENTITY,'ready',attrs):await c.ha.publish(SEED_ENTITY,'ready',attrs)


def validate(attrs,now):
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


def charge_minutes(soc,target,rate,cap,attrs):
    factors={(float(b['soc_lo']),float(b['soc_hi'])):float(b['effective_factor']) for b in attrs['bands']}
    mins=0.0
    for lo,hi in SOC_BANDS:
        ov=max(0,min(target,hi)-max(soc,lo))
        if ov:mins+=(cap*(ov/100))/((rate/1000)*max(.05,factors[(float(lo),float(hi))]))*60
    if target>=100:mins+=float(attrs['top_completion_allowance_minutes'])
    return mins


async def publish_parity(c,plan):
    charge=plan.get('charge',{}); forecast=plan.get('forecast',{})
    soc=as_float(forecast.get('estimated_charge_start_soc')); target=as_float(charge.get('target_soc')); rate=as_float(charge.get('rate_w'))
    out={'friendly_name':'Home Energy Manager Battery Model Parity','evaluated_at':iso(c.now()),'production_authority':'controller','forecaster_affects_control':False,'start_soc':soc,'target_soc':target,'requested_rate_w':rate}
    reason='no_charge_plan' if soc is None or target is None or rate is None or rate<=0 or target<=soc else None
    cap=None; attrs={}; age=None
    if reason is None:
        st=await c.ha.state(c.c.get('battery_capacity_entity','')); cap=as_float(st.get('state')) if st else None
        if cap is None or cap<=0:reason='battery_capacity_unavailable'
    if reason is None:
        st=await c.ha.state(MODEL_ENTITY); attrs=st.get('attributes',{}) if st else {}
        ok,why,age=validate(attrs,c.now())
        if not ok:reason='forecaster_model_'+why
    if reason:
        out.update({'status':'unavailable','reason':reason,'model_age_seconds':age})
        c.LOG.info('Battery model parity unavailable: %s',reason)
    else:
        cm=float(c.charge_minutes(soc,target,rate,cap)); fm=charge_minutes(soc,target,rate,cap,attrs); delta=fm-cm; pct=delta/cm*100 if cm else 0
        bands=[]
        lookup={(float(b['soc_lo']),float(b['soc_hi'])):b for b in attrs['bands']}
        for lo,hi in SOC_BANDS:
            if max(0,min(target,hi)-max(soc,lo))>0:bands.append({'soc_lo':lo,'soc_hi':hi,'controller_factor':float(c.band_factor((lo,hi))),'forecaster_factor':float(lookup[(float(lo),float(hi))]['effective_factor'])})
        migration=((attrs.get('diagnostics') or {}).get('top_completion') or {}).get('migration_state')
        out.update({'status':'match' if abs(delta)<=.05 else 'difference','reason':'ok','capacity_kwh':cap,'controller_minutes':round(cm,4),'forecaster_minutes':round(fm,4),'delta_minutes':round(delta,4),'delta_percent':round(pct,4),'model_generated_at':attrs.get('generated_at'),'model_age_seconds':round(age,1),'migration_state':migration,'controller_top_completion_minutes':float(c.dwell()),'forecaster_top_completion_minutes':float(attrs['top_completion_allowance_minutes']),'bands':bands})
        c.LOG.info('Battery model parity: soc=%.1f target=%.1f rate=%.0fW controller=%.2fmin forecaster=%.2fmin delta=%+.2fmin (%+.2f%%) migration=%s',soc,target,rate,cm,fm,delta,pct,migration)
    if not c.mqtt.publish_sensor(PARITY_ENTITY,out['status'],out):await c.ha.publish(PARITY_ENTITY,out['status'],out)
    return out
