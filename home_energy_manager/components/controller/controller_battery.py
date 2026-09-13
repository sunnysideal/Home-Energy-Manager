"""Battery planning calculations extracted from the controller compatibility core."""

from controller_utils import as_float, clamp, iso

SOC_BANDS=[(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,95),(95,98),(98,99),(99,100)]
GENERIC={(0,10):.95,(10,20):.95,(20,30):.95,(30,40):.95,(40,50):.95,(50,60):.95,(60,70):.95,(70,80):.95,(80,90):.93,(90,95):.88,(95,98):.78,(98,99):.58,(99,100):.35}

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

def band_factor(c,band):
    if not c.db.ok:return GENERIC[band]
    r=c.db.conn.execute('SELECT * FROM learned_bands WHERE band_lo=? AND band_hi=?',band).fetchone()
    if not r or r['learned_factor'] is None:return GENERIC[band]
    confidence=clamp(r['confidence'],0,1)
    return GENERIC[band]*(1-confidence)+r['learned_factor']*confidence

def dwell(c):
    """Conservative completion allowance for a requested 100% charge."""
    generic=float(c.c.get('generic_dwell_minutes',15))
    learned=as_float(c.db.get('learned_dwell_minutes')) if c.db.ok else None
    if learned is None:return generic
    return max(generic,learned)

def learn_top_completion(c,n,end_soc,active,target):
    """Learn extra top-end time from both completed and missed 100% attempts."""
    if not c.db.ok or not active or target is None or target<100:return
    planned_end=active.get('end')
    if planned_end is None:return
    first100=active.get('first100')
    generic=float(c.c.get('generic_dwell_minutes',15))
    current=as_float(c.db.get('learned_dwell_minutes'))
    if current is None:current=generic
    attempts=int(c.db.get('top_completion_attempts') or 0)+1
    successes=int(c.db.get('top_completion_successes') or 0)
    misses=int(c.db.get('top_completion_misses') or 0)
    prior=current
    if first100 is None or end_soc<100:
        shortfall=max(0.0,100.0-float(end_soc))
        step=max(10.0,5.0+shortfall*5.0)
        current=min(90.0,current+step)
        misses+=1; result='miss'; timing=None
    else:
        successes+=1
        timing=max(0.0,(planned_end-first100).total_seconds()/60.0)
        if current>generic and timing>=10.0:
            current=max(generic,current-min(2.0,max(0.0,timing-5.0)*0.10))
        result='success'
    confidence=min(1.0,attempts/5.0)
    c.db.set('learned_dwell_minutes',round(current,2))
    c.db.set('learned_dwell_confidence',confidence)
    c.db.set('top_completion_attempts',attempts)
    c.db.set('top_completion_successes',successes)
    c.db.set('top_completion_misses',misses)
    c.db.set('top_completion_last_result',result)
    c.db.set('top_completion_last_end_soc',float(end_soc))
    c.db.set('top_completion_last_first_100_at',iso(first100) if first100 else None)
    c.db.set('top_completion_last_updated_at',iso(n))
    c.LOG.info(
        'Top charge learning: result=%s end_soc=%.1f%% first100=%s spare_after_100=%s allowance=%.1f->%.1fmin attempts=%d successes=%d misses=%d confidence=%.2f',
        result,float(end_soc),iso(first100) if first100 else 'none',
        'unknown' if timing is None else f'{timing:.1f}min',prior,current,attempts,successes,misses,confidence)

def charge_minutes(c,soc,target,rate,cap):
    if rate<=0 or target<=soc:return 0.0
    mins=0.0
    for lo,hi in SOC_BANDS:
        ov=max(0,min(target,hi)-max(soc,lo))
        if ov:mins+=(cap*(ov/100))/((rate/1000)*max(.05,band_factor(c,(lo,hi))))*60
    if target>=100:mins+=dwell(c)
    return mins

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
