# IMPORTANT PROJECT INSTRUCTIONS
# Before modifying controller behaviour, read AGENTS.md.
# AGENTS.md contains authoritative hard invariants and change-control rules.
# Optimisation logic must never override those rules without explicit user approval.
#
import asyncio
import signal, aiohttp, json, logging, math, os, shutil, sqlite3
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from common.mqtt import MQTTPublisher

VERSION='0.1.40'; DB_SCHEMA_VERSION=1
DB_PATH=Path(os.environ.get('CONTROLLER_DB_PATH','/data/controller.db')); OPTIONS_PATH=Path(os.environ.get('OPTIONS_PATH','/data/options.json'))
LOG=logging.getLogger('home_energy_controller')
logging.basicConfig(level=os.environ.get('LOG_LEVEL','INFO').upper(),format='%(asctime)s %(levelname)s %(message)s')

SOC_BANDS=[(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,95),(95,98),(98,99),(99,100)]
GENERIC={(0,10):.95,(10,20):.95,(20,30):.95,(30,40):.95,(40,50):.95,(50,60):.95,(60,70):.95,(70,80):.95,(80,90):.93,(90,95):.88,(95,98):.78,(98,99):.58,(99,100):.35}
REQUIRED_DISCOVERED=[
    'battery_soc_entity','battery_capacity_entity','battery_reserve_entity',
    'pv_energy_total_entity','grid_import_energy_total_entity','grid_export_energy_total_entity',
    'inverter_max_charge_rate_entity','inverter_max_discharge_rate_entity',
    'eco_mode_entity','charge_schedule_enable_entity','discharge_schedule_enable_entity',
    'charge_slot_1_start_entity','charge_slot_1_end_entity','charge_slot_1_target_entity','charge_rate_entity',
    'discharge_slot_1_start_entity','discharge_slot_1_end_entity','discharge_slot_1_target_entity','discharge_rate_entity',
    'pause_mode_entity','pause_start_entity','pause_end_entity'
]

def as_float(v):
    try:
        if v in (None,'','unknown','unavailable'): return None
        x=float(v); return x if math.isfinite(x) else None
    except: return None

def parse_dt(v):
    if not v: return None
    try: return datetime.fromisoformat(str(v).replace('Z','+00:00'))
    except: return None

def clamp(v,a,b): return max(a,min(b,v))
def iso(v): return v.isoformat() if v else None

def weighted_quantile(vals,wts,q):
    pts=sorted((float(v),float(w)) for v,w in zip(vals,wts) if w>0)
    if not pts:return None
    tgt=sum(w for _,w in pts)*q; acc=0
    for v,w in pts:
        acc+=w
        if acc>=tgt:return v
    return pts[-1][0]

def recency_weight(ts,now,half=45.0):
    age=max(0,(now-ts).total_seconds()/86400)
    return .5**(age/half)

class DB:
    def __init__(self,path): self.path=path; self.conn=None; self.ok=False
    def open(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        existed=self.path.exists(); self.conn=sqlite3.connect(self.path); self.conn.row_factory=sqlite3.Row
        if existed:
            row=self.conn.execute('PRAGMA integrity_check').fetchone()
            if not row or row[0]!='ok': self.conn.close(); self.conn=None; return
        self.conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        row=self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone(); cur=int(row[0]) if row else 0
        if cur and cur<DB_SCHEMA_VERSION:
            self.conn.commit(); self.conn.close(); stamp=datetime.now().strftime('%Y%m%d%H%M%S'); shutil.copy2(self.path,self.path.with_name(self.path.name+'.bak.'+stamp)); self.conn=sqlite3.connect(self.path); self.conn.row_factory=sqlite3.Row
            backups=sorted(self.path.parent.glob(self.path.name+'.bak.*'),reverse=True)
            [p.unlink(missing_ok=True) for p in backups[3:]]
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pending_writes(field TEXT PRIMARY KEY,entity_id TEXT NOT NULL,desired TEXT NOT NULL,window_end TEXT,retry_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS write_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,field TEXT NOT NULL,entity_id TEXT NOT NULL,requested TEXT,observed TEXT,success INTEGER NOT NULL,retry_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS plan_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,field TEXT NOT NULL,old_value TEXT,new_value TEXT,reason_code TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS charge_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,start_soc REAL,end_soc REAL,requested_rate_w REAL,charged_kwh REAL,reached_100 INTEGER NOT NULL,dwell_minutes REAL,eligible INTEGER NOT NULL,rejection_reason TEXT,processed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS band_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id INTEGER NOT NULL,observed_at TEXT NOT NULL,band_lo REAL NOT NULL,band_hi REAL NOT NULL,coverage REAL NOT NULL,effective_factor REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS learned_bands(band_lo REAL NOT NULL,band_hi REAL NOT NULL,learned_factor REAL,confidence REAL NOT NULL DEFAULT 0,full_equiv REAL NOT NULL DEFAULT 0,obs_count INTEGER NOT NULL DEFAULT 0,p10 REAL,p90 REAL,updated_at TEXT NOT NULL,PRIMARY KEY(band_lo,band_hi));
        CREATE TABLE IF NOT EXISTS daily_summary(day TEXT PRIMARY KEY,planned_charge_kwh REAL,actual_charge_kwh REAL,charge_variance_kwh REAL,charge_variance_reason TEXT,reached_100 INTEGER,end_offpeak_soc INTEGER,planned_export_kwh REAL,actual_export_kwh REAL,export_variance_kwh REAL,export_variance_reason TEXT,actual_export_end TEXT,updated_at TEXT NOT NULL);
        ''')
        self.conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(DB_SCHEMA_VERSION),)); self.conn.commit(); self.ok=True
    def get(self,k,d=None):
        if not self.ok:return d
        r=self.conn.execute('SELECT value FROM kv WHERE key=?',(k,)).fetchone()
        if not r:return d
        try:return json.loads(r[0])
        except:return r[0]
    def set(self,k,v):
        if not self.ok:return
        self.conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(k,json.dumps(v),datetime.now(timezone.utc).isoformat())); self.conn.commit()
    def prune(self,now):
        if not self.ok:return
        c90=(now-timedelta(days=90)).isoformat(); c30=(now-timedelta(days=30)).isoformat(); c365=(now-timedelta(days=365)).isoformat()
        self.conn.execute('DELETE FROM write_audit WHERE ts<?',(c90,)); self.conn.execute('DELETE FROM plan_changes WHERE ts<?',(c90,)); self.conn.execute('DELETE FROM daily_summary WHERE updated_at<?',(c90,)); self.conn.execute('DELETE FROM charge_sessions WHERE eligible=1 AND ended_at<?',(c90,)); self.conn.execute('DELETE FROM charge_sessions WHERE eligible=0 AND ended_at<?',(c30,)); self.conn.execute('DELETE FROM band_observations WHERE observed_at<?',(c365,)); self.conn.commit()
    def close(self):
        if self.conn:self.conn.commit(); self.conn.close()

class HA:
    def __init__(self):
        t=os.environ.get('SUPERVISOR_TOKEN');
        if not t: raise RuntimeError('SUPERVISOR_TOKEN missing')
        self.t=t; self.s=None; self.base='http://supervisor/core/api'; self.ws='ws://supervisor/core/websocket'
    async def open(self): self.s=aiohttp.ClientSession(headers={'Authorization':'Bearer '+self.t})
    async def close(self): await self.s.close()
    async def state(self,e):
        if not e:return None
        try:
            async with self.s.get(f'{self.base}/states/{e}',timeout=15) as r:return await r.json() if r.status==200 else None
        except:return None
    async def states(self):
        try:
            async with self.s.get(f'{self.base}/states',timeout=20) as r:return await r.json() if r.status==200 else []
        except:return []
    async def publish(self,e,state,attrs):
        async with self.s.post(f'{self.base}/states/{e}',json={'state':state,'attributes':attrs},timeout=15) as r:
            if r.status not in (200,201):raise RuntimeError(f'publish HTTP {r.status}')
    async def write(self,e,v):
        d=e.split('.',1)[0]; data={'entity_id':e}
        if d=='switch': service='turn_on' if str(v).lower() in ('on','true','1') else 'turn_off'
        elif d in ('number','input_number'): service='set_value'; data['value']=float(v)
        elif d in ('select','input_select'): service='select_option'; data['option']=str(v)
        elif d=='time': service='set_value'; data['time']=str(v)
        elif d=='input_datetime': service='set_datetime'; data['time']=str(v)
        else: raise ValueError('Unsupported writable entity domain '+d)
        async with self.s.post(f'{self.base}/services/{d}/{service}',json=data,timeout=20) as r:
            if r.status!=200: raise RuntimeError(f'{d}.{service} HTTP {r.status}')
    async def events(self,ready=None):
        while True:
            try:
                async with self.s.ws_connect(self.ws,heartbeat=30) as ws:
                    m=await ws.receive_json()
                    if m.get('type')=='auth_required': await ws.send_json({'type':'auth','access_token':self.t}); m=await ws.receive_json()
                    if m.get('type')!='auth_ok': raise RuntimeError('WS auth failed')
                    await ws.send_json({'id':1,'type':'subscribe_events','event_type':'state_changed'})
                    ack=await ws.receive_json()
                    if ack.get('type')!='result' or not ack.get('success',False): raise RuntimeError('WS event subscription failed')
                    if ready is not None:ready.set()
                    async for m in ws:
                        if m.type==aiohttp.WSMsgType.TEXT:
                            j=json.loads(m.data)
                            if j.get('type')=='event': yield j['event']['data']
            except asyncio.CancelledError: raise
            except Exception as e: LOG.warning('WebSocket reconnect: %s',e); await asyncio.sleep(5)

class Controller:
    def __init__(self,cfg,ha,db):
        self.c=cfg; self.ha=ha; self.db=db; self.mqtt=MQTTPublisher('controller','Home Energy Manager – Controller','Home Energy Controller',VERSION,ha.t); self.tz=ZoneInfo(cfg.get('timezone','Europe/London')); self.lock=asyncio.Lock(); self.queued=None; self.health='degraded'; self.errors=[]; self.changes=[]; self.confirmed=db.get('confirmed_plan') if db.ok else None; self.last_attempt=None; self.last_success=None; self.active=None; self.samples=[]; self.last_sample=None; self.daily={}; self.discovery_ready=False; self.refresh_request_entity=''; self.confirmed_writes_this_apply=0; self.last_source_forecast_sequence=int(db.get('last_source_forecast_sequence',0) or 0) if db.ok else 0; self.pending_refresh_request_id=int(db.get('pending_refresh_request_id',0) or 0) if db.ok else 0; self.last_refresh_ack_sequence=int(db.get('last_refresh_ack_sequence',0) or 0) if db.ok else 0; self.intelligent_sensor_state='unknown'; self.waiting_for_controller_refresh=False; self.waiting_for_controller_refresh_since=None
    def discover_from_forecast(self,s):
        attrs=s.get('attributes',{}) if isinstance(s,dict) else {}
        ci=attrs.get('controller_inputs')
        if not isinstance(ci,dict):
            self.discovery_ready=False
            self.err('input','controller_inputs','Forecast sensor does not expose controller_inputs; Home Energy Forecaster v0.2.8+ is required',100)
            return False
        if int(ci.get('version') or 0) < 3:
            self.discovery_ready=False
            self.err('input','controller_inputs','Forecast controller_inputs v3 required; update Home Energy Forecaster to v0.2.8+',100)
            return False
        self.refresh_request_entity=str(ci.get('refresh_request_entity') or '').strip()
        if not self.refresh_request_entity:
            self.discovery_ready=False
            self.err('input','controller_inputs','Forecast controller_inputs.refresh_request_entity is missing',100)
            return False
        ents=ci.get('entities')
        if not isinstance(ents,dict):
            self.discovery_ready=False
            self.err('input','controller_inputs','Forecast controller_inputs.entities is missing/invalid',100)
            return False
        mapping={
            'battery_soc':'battery_soc_entity',
            'battery_capacity':'battery_capacity_entity',
            'battery_reserve':'battery_reserve_entity',
            'pv_energy_total':'pv_energy_total_entity',
            'grid_import_energy_total':'grid_import_energy_total_entity',
            'grid_export_energy_total':'grid_export_energy_total_entity',
            'inverter_max_charge_rate':'inverter_max_charge_rate_entity',
            'inverter_max_discharge_rate':'inverter_max_discharge_rate_entity',
            'eco_mode':'eco_mode_entity',
            'charge_schedule_enable':'charge_schedule_enable_entity',
            'discharge_schedule_enable':'discharge_schedule_enable_entity',
            'charge_slot_1_start':'charge_slot_1_start_entity',
            'charge_slot_1_end':'charge_slot_1_end_entity',
            'charge_slot_1_target':'charge_slot_1_target_entity',
            'charge_rate':'charge_rate_entity',
            'discharge_slot_1_start':'discharge_slot_1_start_entity',
            'discharge_slot_1_end':'discharge_slot_1_end_entity',
            'discharge_slot_1_target':'discharge_slot_1_target_entity',
            'discharge_rate':'discharge_rate_entity',
            'pause_mode':'pause_mode_entity',
            'pause_start':'pause_start_entity',
            'pause_end':'pause_end_entity',
        }
        for src,dst in mapping.items():
            self.c[dst]=str(ents.get(src,'')).strip()
        vals=ci.get('values') if isinstance(ci.get('values'),dict) else {}
        metering=attrs.get('metering') if isinstance(attrs.get('metering'),dict) else {}
        meter_import_entity=str(metering.get('import_entity') or '').strip()
        meter_export_entity=str(metering.get('export_entity') or '').strip()
        if meter_import_entity:
            self.c['grid_import_energy_total_entity']=meter_import_entity
        if meter_export_entity:
            self.c['grid_export_energy_total_entity']=meter_export_entity
        ci_import_source=str(vals.get('grid_import_meter_source') or '').strip()
        ci_export_source=str(vals.get('grid_export_meter_source') or '').strip()
        meter_import_source=str(metering.get('import_source') or '').strip()
        meter_export_source=str(metering.get('export_source') or '').strip()
        if meter_import_source and ci_import_source and meter_import_source != ci_import_source:
            LOG.warning('Forecast meter provenance mismatch for import: metering=%s controller_inputs=%s; using metering', meter_import_source, ci_import_source)
        if meter_export_source and ci_export_source and meter_export_source != ci_export_source:
            LOG.warning('Forecast meter provenance mismatch for export: metering=%s controller_inputs=%s; using metering', meter_export_source, ci_export_source)
        self.c['grid_import_meter_source']=meter_import_source or ci_import_source or 'battery'
        self.c['grid_export_meter_source']=meter_export_source or ci_export_source or 'battery'
        self.c['ev_included_in_battery_load']=bool(metering.get('ev_included_in_battery_load', vals.get('ev_included_in_battery_load',False)))
        tz=str(ci.get('timezone') or self.c.get('timezone') or 'Europe/London')
        try:self.tz=ZoneInfo(tz)
        except Exception:
            self.discovery_ready=False
            self.err('input','timezone',f'Invalid timezone from forecast: {tz}',90)
            return False
        missing=[k for k in REQUIRED_DISCOVERED if not str(self.c.get(k,'')).strip()]
        if missing:
            self.discovery_ready=False
            self.err('input','controller_inputs','Forecast controller_inputs missing: '+', '.join(missing[:5]),100)
            return False
        self.discovery_ready=True
        self.clear('input','controller_inputs'); self.clear('input','timezone')
        if self.db.ok:
            self.db.set('controller_inputs',{'version':ci.get('version'),'timezone':tz,'refresh_request_entity':self.refresh_request_entity,'entities':{src:self.c.get(dst) for src,dst in mapping.items()},'grid_import_meter_source':self.c.get('grid_import_meter_source'),'grid_export_meter_source':self.c.get('grid_export_meter_source'),'ev_included_in_battery_load':self.c.get('ev_included_in_battery_load',False)})
        return True
    def forecast_sequence(self,s):
        try:return int(s.get('attributes',{}).get('forecast_sequence',0))
        except (TypeError,ValueError):return 0
    def forecast_trigger(self,s):
        return str(s.get('attributes',{}).get('forecast_trigger') or '').strip().lower()
    def controller_triggered_forecast(self,s):
        return self.forecast_trigger(s).startswith('controller_refresh')
    async def request_forecast_refresh(self,source_sequence):
        if not self.refresh_request_entity:return False
        prev=int(self.db.get('last_refresh_request_id',0) or 0) if self.db.ok else 0
        request_id=max(int(datetime.now(timezone.utc).timestamp()*1000),prev+1)
        attrs={'friendly_name':'Home Energy Forecast Refresh Request','requested_at':iso(self.now()),'source_forecast_sequence':int(source_sequence or 0),'controller_version':VERSION}
        self.waiting_for_controller_refresh=True
        self.waiting_for_controller_refresh_since=self.now()
        started=asyncio.get_running_loop().time()
        try:
            await self.ha.publish(self.refresh_request_entity,request_id,attrs)
        except Exception as exc:
            self.waiting_for_controller_refresh=False
            self.waiting_for_controller_refresh_since=None
            self.err('write','forecast_refresh',f'Could not request forecast refresh: {exc}',35)
            return False
        elapsed=asyncio.get_running_loop().time()-started
        if self.db.ok:self.db.set('last_refresh_request_id',request_id)
        self.clear('write','forecast_refresh')
        LOG.info('Requested immediate forecast refresh: request_id=%d source_sequence=%d publish=%.3fs; blocking scheduled control until controller_refresh publishes',
                 request_id,int(source_sequence or 0),elapsed)
        return True
    def now(self):return datetime.now(self.tz)
    def half_hour_slot(self,when=None):
        n=(when or self.now()).astimezone(self.tz)
        minute=30 if n.minute>=30 else 0
        start=n.replace(minute=minute,second=0,microsecond=0)
        return start,start+timedelta(minutes=30)

    def persisted_intelligent_slot(self):
        if not self.db.ok:return None
        r=self.db.get('intelligent_confirmed_slot')
        if not isinstance(r,dict):return None
        a=parse_dt(r.get('start')); b=parse_dt(r.get('end'))
        if not a or not b:return None
        a=a.astimezone(self.tz); b=b.astimezone(self.tz)
        n=self.now()
        if not (a<=n<b):
            if b<=n:self.db.set('intelligent_confirmed_slot',None)
            return None
        return {'start':a,'end':b}

    async def intelligent_planned_slots(self):
        """Return future/current planned Intelligent dispatch intervals.

        These are advisory only. They must never be treated as guaranteed cheap
        energy. They are used solely to avoid *starting* a normal export that
        would overlap an upcoming planned car-dispatch window.
        """
        entity=str(self.c.get('intelligent_dispatch_entity','')).strip()
        if not self.c.get('intelligent_go_enabled',True) or not entity:
            return []
        st=await self.ha.state(entity)
        if not st:return []
        attrs=st.get('attributes',{}) if isinstance(st,dict) else {}
        planned=attrs.get('planned_dispatches')
        if not isinstance(planned,list):return []
        n=self.now()
        out=[]
        for e in planned:
            if not isinstance(e,dict):continue
            a=parse_dt(e.get('start')); b=parse_dt(e.get('end'))
            if not a or not b or b<=a:continue
            a=a.astimezone(self.tz); b=b.astimezone(self.tz)
            if b<=n:continue
            out.append((a,b,e))
        return sorted(out,key=lambda z:z[0])

    async def intelligent_go_info(self):
        """Confirm an Intelligent Go settlement slot from the user's reliable sensor.

        A single observed ON at any point confirms the whole enclosing 30-minute
        settlement period. Confirmation is persisted until the slot boundary so
        a brief sensor OFF or controller restart cannot make the controller
        resume export within that already-confirmed cheap half-hour.
        """
        entity=str(self.c.get('intelligent_car_charging_entity',
                              'binary_sensor.octopus_slot_actually_charging')).strip()
        if not self.c.get('intelligent_go_enabled',True) or not entity:
            return {'enabled':False,'entity_id':entity,'sensor_on':False,'confirmed':False,
                    'slot_start':None,'slot_end':None}
        st=await self.ha.state(entity)
        state=str(st.get('state','unknown')).lower() if st else 'unavailable'
        sensor_on=state=='on'
        self.intelligent_sensor_state=state

        slot=self.persisted_intelligent_slot()
        if sensor_on:
            a,b=self.half_hour_slot()
            slot={'start':a,'end':b}
            if self.db.ok:
                prior=self.db.get('intelligent_confirmed_slot')
                if not isinstance(prior,dict) or prior.get('start')!=iso(a) or prior.get('end')!=iso(b):
                    self.db.set('intelligent_confirmed_slot',{'start':iso(a),'end':iso(b)})
                    LOG.info('Intelligent Go slot confirmed by %s: %s-%s',
                             entity,a.strftime('%H:%M'),b.strftime('%H:%M'))
        confirmed=slot is not None
        return {
            'enabled':True,
            'entity_id':entity,
            'sensor_state':state,
            'sensor_on':sensor_on,
            'confirmed':confirmed,
            'slot_start':slot['start'] if slot else None,
            'slot_end':slot['end'] if slot else None,
            'whole_half_hour_cheap':confirmed,
            'export_suspended':confirmed,
        }

    def disabled_discharge_time(self,w):
        # Keep a disabled slot stable at the normal evening export anchor rather
        # than moving it when an active cheap window temporarily becomes the
        # control window. A disabled slot is always represented as start == end.
        try:
            return self.export_start(w).replace(second=0,microsecond=0)
        except Exception:
            return self.now().replace(second=0,microsecond=0)

    def operation_mode(self):
        m=str(self.c.get('operation_mode','maximise_export'))
        return m if m in ('maximise_export','minimise_export','export_generated') else 'maximise_export'
    def calibration_state(self):
        if not self.c.get('calibration_enabled',True):return 'disabled'
        n=self.now()
        low=parse_dt(self.db.get('calibration_low_reached_at')) if self.db.ok else None
        last_deep=parse_dt(self.db.get('last_deep_calibration_at')) if self.db.ok else None
        last_full=parse_dt(self.db.get('last_full_soc_at')) if self.db.ok else None
        deep_days=int(self.c.get('deep_cycle_every_days',60)); top_days=int(self.c.get('top_full_every_days',14))
        deep_due=last_deep is not None and n.astimezone(timezone.utc)-last_deep.astimezone(timezone.utc)>=timedelta(days=deep_days)
        top_due=last_full is not None and n.astimezone(timezone.utc)-last_full.astimezone(timezone.utc)>=timedelta(days=top_days)
        if low:return 'deep_recharge'
        if deep_due:return 'awaiting_deep_low'
        if top_due:return 'top_due'
        return 'normal'
    def calibration_attrs(self):
        return {
            'state':self.calibration_state(),
            'last_full_soc_at':self.db.get('last_full_soc_at') if self.db.ok else None,
            'last_deep_calibration_at':self.db.get('last_deep_calibration_at') if self.db.ok else None,
            'low_reached_at':self.db.get('calibration_low_reached_at') if self.db.ok else None,
        }
    async def update_effective_export_accounting(self):
        """Track fallback house-side export, excluding EV flow.

        When the forecaster has selected a true utility-meter export entity this
        proxy is deliberately bypassed: actual utility export is already known.
        """
        if not self.db.ok or not self.discovery_ready:
            return None
        if str(self.c.get('grid_export_meter_source') or 'battery') == 'true_meter':
            return None

        export_entity=str(self.c.get('grid_export_energy_total_entity','')).strip()
        car_entity=str(self.c.get('intelligent_car_charging_entity',
                                  'binary_sensor.octopus_slot_actually_charging')).strip()
        if not export_entity:
            return None

        est=await self.ha.state(export_entity)
        if not est:
            return self.db.get('effective_export_accounting')
        total=as_float(est.get('state'))
        if total is None:
            return self.db.get('effective_export_accounting')

        cst=await self.ha.state(car_entity) if car_entity else None
        car_on=bool(cst and str(cst.get('state','')).lower()=='on')
        n=self.now()
        day=n.date().isoformat()

        a=self.db.get('effective_export_accounting')
        if not isinstance(a,dict) or a.get('day')!=day:
            a={
                'day':day,
                'last_total_kwh':float(total),
                'last_at':iso(n),
                'last_car_on':bool(car_on),
                'credited_export_kwh':0.0,
                'excluded_while_car_charging_kwh':0.0,
                'tracking_started_at':iso(n),
                'pre_tracking_export_credited':False,
            }
            self.db.set('effective_export_accounting',a)
            LOG.info('Effective export accounting started: day=%s baseline_total=%.3fkWh car_on=%s; pre-tracking export not credited',
                     day,float(total),'yes' if car_on else 'no')
            return a

        prev_total=as_float(a.get('last_total_kwh'))
        prev_car=bool(a.get('last_car_on'))
        credited=float(a.get('credited_export_kwh') or 0.0)
        excluded=float(a.get('excluded_while_car_charging_kwh') or 0.0)

        if prev_total is not None:
            delta=float(total)-float(prev_total)
            if 0.0 <= delta <= 10.0 and delta>0:
                if car_on or prev_car:
                    excluded += delta
                    LOG.info('Effective export accounting: excluded %.3fkWh apparent export while car charging (credited=%.3f excluded=%.3f)',
                             delta,credited,excluded)
                else:
                    credited += delta

        a.update({
            'last_total_kwh':float(total),
            'last_at':iso(n),
            'last_car_on':bool(car_on),
            'credited_export_kwh':round(credited,6),
            'excluded_while_car_charging_kwh':round(excluded,6),
        })
        self.db.set('effective_export_accounting',a)
        return a

    def effective_export_accounting(self):
        if not self.db.ok:
            return None
        a=self.db.get('effective_export_accounting')
        if not isinstance(a,dict) or a.get('day')!=self.now().date().isoformat():
            return None
        return a

    def export_generated_inputs(self,s):
        attrs=s.get('attributes',{}) if s else {}
        today=attrs.get('today',{}) if isinstance(attrs,dict) else {}
        actual=today.get('actual',{}) if isinstance(today,dict) else {}
        total=today.get('total',{}) if isinstance(today,dict) else {}
        no_slots=attrs.get('no_slots_totals',{}) if isinstance(attrs,dict) else {}
        no_slots_today=no_slots.get('today',{}) if isinstance(no_slots,dict) else {}

        pv=as_float(total.get('pv_kwh')) if isinstance(total,dict) else None
        raw_house_export=as_float(actual.get('export_kwh')) if isinstance(actual,dict) else None
        natural_future=as_float(no_slots_today.get('export_kwh')) if isinstance(no_slots_today,dict) else None
        # Meter provenance belongs to this forecast payload.  Derive it here
        # rather than relying on mutable controller discovery state, so the
        # source used for Export Generated always matches today.actual.export_kwh.
        metering=attrs.get('metering') if isinstance(attrs.get('metering'),dict) else {}
        ci=attrs.get('controller_inputs') if isinstance(attrs.get('controller_inputs'),dict) else {}
        vals=ci.get('values') if isinstance(ci.get('values'),dict) else {}
        meter_source=str(metering.get('export_source') or vals.get('grid_export_meter_source') or self.c.get('grid_export_meter_source') or 'battery').strip()
        meter_entity=str(metering.get('export_entity') or ((ci.get('entities') or {}).get('grid_export_energy_total') if isinstance(ci.get('entities'),dict) else '') or self.c.get('grid_export_energy_total_entity') or '').strip()
        accounting=None if meter_source=='true_meter' else self.effective_export_accounting()

        if pv is None:return None
        if natural_future is None:
            natural_future=0.0

        if meter_source=='true_meter':
            credited=max(0.0,raw_house_export or 0.0)
        else:
            credited=as_float((accounting or {}).get('credited_export_kwh'))
            if credited is None:
                credited=0.0

        solar_target=max(0.0,pv)
        exported_so_far=max(0.0,credited)
        forecast_natural_export=max(0.0,natural_future)
        remaining=max(0.0,solar_target-exported_so_far-forecast_natural_export)

        return {
            'solar_target_kwh':solar_target,
            'exported_so_far_kwh':exported_so_far,
            'raw_house_export_so_far_kwh':max(0.0,raw_house_export or 0.0),
            'excluded_while_car_charging_kwh':0.0 if meter_source=='true_meter' else max(0.0,as_float((accounting or {}).get('excluded_while_car_charging_kwh')) or 0.0),
            'export_meter_source':meter_source,
            'export_meter_entity':meter_entity,
            'raw_meter_export_so_far_kwh':max(0.0,raw_house_export or 0.0),
            'forecast_natural_export_kwh':forecast_natural_export,
            'remaining_kwh':remaining,
            'accounting_started_at':(accounting or {}).get('tracking_started_at'),
        }

    def forecast_net_segments(self,s,start,end,attr='forecast_no_slots'):
        """Return prorated AC load/PV energy segments for an arbitrary window."""
        out=[]
        if not start or not end or end<=start:return out
        for a,b,_rate,x in self.intervals(s,attr):
            ov_start=max(a,start); ov_end=min(b,end)
            if ov_end<=ov_start:continue
            dur=max(1e-9,(b-a).total_seconds())
            frac=(ov_end-ov_start).total_seconds()/dur
            load=as_float(x.get('load_kwh')) or 0.0
            pv=as_float(x.get('pv_kwh')) or 0.0
            out.append((ov_start,ov_end,load*frac,pv*frac))
        return out

    def forecast_export_kwh_between(self,s,start,end,attr='forecast_no_slots'):
        if not start or not end or end<=start:return 0.0
        total=0.0
        for a,b,_rate,x in self.intervals(s,attr):
            ov_start=max(a,start); ov_end=min(b,end)
            if ov_end<=ov_start:continue
            dur=max(1e-9,(b-a).total_seconds())
            frac=(ov_end-ov_start).total_seconds()/dur
            total += max(0.0,(as_float(x.get('export_kwh')) or 0.0)*frac)
        return total

    def forced_incremental_grid_export_kwh(self,s,start,end,rate_w):
        """Forecast extra grid export caused by a forced-discharge window.

        `remaining_kwh` in export_generated already subtracts forecast natural
        export, so this returns only export *above* the no-slots baseline.

        Forced grid export is approximated from the AC balance in each no-slots
        forecast segment:
            forced_export = max(0, forced_battery + PV - load)
        and the no-slots slot's own export is subtracted to avoid double-counting
        natural PV export that export_generated has already reserved for.
        """
        if not start or not end or end<=start or rate_w<=0:return 0.0
        total=0.0
        covered=0.0
        for a,b,_rate,x in self.intervals(s,'forecast_no_slots'):
            ov_start=max(a,start); ov_end=min(b,end)
            if ov_end<=ov_start:continue
            dur=max(1e-9,(b-a).total_seconds())
            ov=(ov_end-ov_start).total_seconds()
            frac=ov/dur
            load=(as_float(x.get('load_kwh')) or 0.0)*frac
            pv=(as_float(x.get('pv_kwh')) or 0.0)*frac
            natural_export=(as_float(x.get('export_kwh')) or 0.0)*frac
            forced_battery=(rate_w/1000.0)*(ov/3600.0)
            forced_export=max(0.0,forced_battery+pv-load)
            total += max(0.0,forced_export-natural_export)
            covered += ov

        # Conservative fallback for any tiny forecast gap: assume house load
        # consumes the forced output rather than pretending all 6 kW is exported.
        # In normal operation the forecast covers the whole planning horizon.
        wanted=(end-start).total_seconds()
        if covered < wanted-1.0:
            gap=max(0.0,wanted-covered)
            # No reliable load/PV data for the gap. Count no export from it.
            # This makes the solver extend rather than terminate early.
            _=gap
        return total

    def export_generated_end(self,s,start,latest,remaining_kwh,rate_w):
        """Find the earliest minute-boundary end that meets remaining grid export."""
        if not start or not latest or latest<=start or remaining_kwh<=0 or rate_w<=0:
            return start,0.0,False

        max_export=self.forced_incremental_grid_export_kwh(s,start,latest,rate_w)
        if max_export+1e-6 < remaining_kwh:
            return latest,max_export,False

        lo=start
        hi=latest
        # Binary search at second resolution, then round UP to the next whole
        # minute because the inverter pause/slot selectors are minute based.
        for _ in range(28):
            mid=lo+(hi-lo)/2
            got=self.forced_incremental_grid_export_kwh(s,start,mid,rate_w)
            if got>=remaining_kwh:
                hi=mid
            else:
                lo=mid

        end=hi
        if end.second or end.microsecond:
            end=(end+timedelta(minutes=1)).replace(second=0,microsecond=0)
        else:
            end=end.replace(second=0,microsecond=0)
        end=min(latest.replace(second=0,microsecond=0),end)
        got=self.forced_incremental_grid_export_kwh(s,start,end,rate_w)
        return end,got,got+0.02>=remaining_kwh

    def export_generated_guardrail_end(self,s,start,latest,rate_w,arrival_soc,cap,reserve):
        """Latest forced-export end that still preserves the arrival SOC guardrail.

        The guardrail is an SOC constraint, not a grid-export-energy constraint.
        Forced battery output also serves house load and incurs discharge losses,
        so limiting by exported kWh can over-discharge the battery.  Reuse the
        same incremental SOC-depletion model used by charge planning and solve
        directly for the latest safe end time.
        """
        if not start or not latest or latest<=start or rate_w<=0 or cap<=0:
            return start,0.0
        buffer=max(float(reserve),float(self.c.get('safety_buffer_soc',20)))
        available_pct=max(0.0,float(arrival_soc)-buffer)
        if available_pct<=1e-6:
            return start,0.0

        def depletion(end):
            return self.planned_discharge_soc_adjustment(s,start,end,rate_w,cap,reserve)

        if depletion(latest)<=available_pct+1e-6:
            end=latest.replace(second=0,microsecond=0)
            return end,self.forced_incremental_grid_export_kwh(s,start,end,rate_w)

        lo=start
        hi=latest
        for _ in range(28):
            mid=lo+(hi-lo)/2
            if depletion(mid)<=available_pct:
                lo=mid
            else:
                hi=mid

        # Round DOWN so minute-resolution inverter controls cannot cross the
        # guardrail merely because of time rounding.
        end=lo.replace(second=0,microsecond=0)
        if end<start:
            end=start.replace(second=0,microsecond=0)
        got=self.forced_incremental_grid_export_kwh(s,start,end,rate_w)
        return end,got

    def export_generated_topup_need(self,s,export_info,arrival_soc,cap,reserve,dr,w):
        """Return extra stored battery kWh needed for the full export target.

        This uses exactly the same SOC-depletion model as the evening guardrail.
        A confirmed daytime Intelligent slot can therefore add only the energy
        required to make the full generation-matched export reachable while the
        predicted SOC at regular off-peak arrival remains at/above the buffer.
        """
        if not export_info or cap<=0 or dr<=0 or not w:
            return {'needed_stored_kwh':0.0,'safe_export_kwh':0.0,'shortfall_kwh':0.0,
                    'target_reachable_without_topup':True}
        remaining=max(0.0,float(export_info.get('remaining_kwh') or 0.0))
        if remaining<=0:
            return {'needed_stored_kwh':0.0,'safe_export_kwh':0.0,'shortfall_kwh':0.0,
                    'target_reachable_without_topup':True}

        buffer=max(float(reserve),float(self.c.get('safety_buffer_soc',20)))
        es=self.export_start(w)
        latest=w['start']
        if latest<=es:
            return {'needed_stored_kwh':remaining,'safe_export_kwh':0.0,'shortfall_kwh':remaining,
                    'target_reachable_without_topup':False,'target_reachable_with_full_topup':False}

        safe_end,safe_export=self.export_generated_guardrail_end(
            s,es,latest,dr,arrival_soc,cap,reserve
        )
        shortfall=max(0.0,remaining-safe_export)
        if shortfall<=0.02:
            return {'needed_stored_kwh':0.0,'safe_export_kwh':safe_export,'shortfall_kwh':0.0,
                    'target_reachable_without_topup':True}

        target_end,target_export,time_reachable=self.export_generated_end(
            s,es,latest,remaining,dr
        )
        if not time_reachable:
            return {'needed_stored_kwh':0.0,'safe_export_kwh':safe_export,
                    'shortfall_kwh':shortfall,'target_reachable_without_topup':False,
                    'target_reachable_with_full_topup':False,
                    'max_export_with_topup_kwh':target_export}

        required_depletion_pct=self.planned_discharge_soc_adjustment(
            s,es,target_end,dr,cap,reserve
        )
        required_arrival_soc=buffer+required_depletion_pct
        needed_pct=max(0.0,required_arrival_soc-float(arrival_soc))
        headroom_pct=max(0.0,100.0-float(arrival_soc))
        achievable_pct=min(needed_pct,headroom_pct)
        needed_stored=cap*achievable_pct/100.0
        reachable_with_topup=needed_pct<=headroom_pct+0.05

        return {'needed_stored_kwh':needed_stored,'safe_export_kwh':safe_export,
                'shortfall_kwh':shortfall,'target_reachable_without_topup':False,
                'target_reachable_with_full_topup':reachable_with_topup,
                'required_arrival_soc':min(100.0,required_arrival_soc),
                'max_export_with_topup_kwh':remaining if reachable_with_topup else safe_export}

    def power_down_protected_soc(self,s,session_end,next_offpeak_start,cap,reserve):
        """SOC required at Power Down end to avoid later peak import.

        Work backwards from the normal safety-buffer SOC required at the next
        off-peak start. Forecast load deficits consume stored energy at the
        forecaster's 95% discharge efficiency; forecast PV surplus can replenish
        it at 95% charge efficiency. The reserve is a hard floor throughout.
        """
        buffer=max(float(reserve),float(self.c.get('safety_buffer_soc',20)))
        if not session_end or not next_offpeak_start or session_end>=next_offpeak_start:
            return buffer,0.0,True
        reserve_kwh=cap*float(reserve)/100.0
        required=cap*buffer/100.0
        segs=self.forecast_net_segments(s,session_end,next_offpeak_start,'forecast_no_slots')
        # Missing forecast coverage is unsafe: preserve the full battery rather
        # than create peak imports from an optimistic assumption.
        covered=sum((b-a).total_seconds() for a,b,_,_ in segs)
        wanted=(next_offpeak_start-session_end).total_seconds()
        if wanted>60 and covered < wanted-60:
            return 100.0,max(0.0,cap*(100.0-buffer)/100.0),False
        for _a,_b,load,pv in reversed(segs):
            net=load-pv
            if net>=0:
                required += net/0.95
            else:
                required=max(reserve_kwh,required-(-net)*0.95)
        achievable=required<=cap+1e-6
        required=min(cap,required)
        extra=max(0.0,required-cap*buffer/100.0)
        return clamp(required/cap*100.0,reserve,100.0),extra,achievable

    async def power_down_info(self,s,w,cap,reserve,dr):
        """Read BottlecapDave joined Power Down sessions and build a plan input.

        The events entity is authoritative for joined sessions. The baseline
        sensor is optional and informational only: a paid Power Down is not
        capped at the baseline because the controller deliberately exports as
        much as is economically safe.
        """
        if not self.c.get('power_down_enabled',True):return None
        event_entity=str(self.c.get('power_down_events_entity','')).strip()
        if not event_entity:return None
        st=await self.ha.state(event_entity)
        if not st:
            self.err('input','power_down_events','Configured Power Down events entity unavailable',35)
            return None
        attrs=st.get('attributes',{}) if isinstance(st,dict) else {}
        joined=attrs.get('joined_events')
        if not isinstance(joined,list):
            self.err('input','power_down_events','Power Down events entity has no joined_events attribute',35)
            return None
        self.clear('input','power_down_events')
        n=self.now()
        candidates=[]
        for e in joined:
            if not isinstance(e,dict):continue
            a=parse_dt(e.get('start')); b=parse_dt(e.get('end'))
            if not a or not b or b<=a:continue
            a=a.astimezone(self.tz); b=b.astimezone(self.tz)
            if b<=n:continue
            candidates.append((a,b,e))
        if not candidates:return None
        a,b,e=min(candidates,key=lambda z:z[0])

        import_baseline_total=None
        export_baseline_total=None
        import_baseline_incomplete=None
        export_baseline_incomplete=None

        import_baseline_entity=str(self.c.get('power_down_import_baseline_entity','')).strip()
        export_baseline_entity=str(self.c.get('power_down_export_baseline_entity','')).strip()

        if import_baseline_entity:
            bs=await self.ha.state(import_baseline_entity)
            if bs:
                ba=bs.get('attributes',{}) if isinstance(bs,dict) else {}
                import_baseline_total=as_float(ba.get('total_baseline'))
                import_baseline_incomplete=ba.get('is_incomplete_calculation')

        if export_baseline_entity:
            bs=await self.ha.state(export_baseline_entity)
            if bs:
                ba=bs.get('attributes',{}) if isinstance(bs,dict) else {}
                export_baseline_total=as_float(ba.get('total_baseline'))
                export_baseline_incomplete=ba.get('is_incomplete_calculation')

        net_baseline_total=None
        if import_baseline_total is not None or export_baseline_total is not None:
            net_baseline_total=(import_baseline_total or 0.0)-(export_baseline_total or 0.0)

        # If the session is before the next cheap window, protect enough energy
        # after it to reach that window with the normal safety buffer intact.
        if b<=w['start']:
            protected,post_kwh,avoidable=self.power_down_protected_soc(
                s,b,w['start'],cap,reserve
            )
        elif a<w['start']<b:
            # Session overlaps the beginning of cheap rate. There is no later
            # peak interval to bridge once cheap rate begins.
            protected=max(float(reserve),float(self.c.get('safety_buffer_soc',20)))
            post_kwh=0.0; avoidable=True
        else:
            # Session is after this next cheap window. Tonight should fill to
            # 100%; detailed export protection will be recalculated once the
            # controller's "next offpeak" advances beyond the session.
            protected=None; post_kwh=None; avoidable=True

        start_soc=None
        if a<=n<b:
            start_soc=await self.live_soc()
        if start_soc is None:
            start_soc=self.soc_at(s,max(a,n),'forecast_no_slots')

        available_ac=0.0
        session_limit_ac=max(0.0,(b-max(a,n)).total_seconds()/3600.0*(dr/1000.0))
        if protected is not None and start_soc is not None:
            stored=max(0.0,cap*(start_soc-protected)/100.0)
            available_ac=min(stored*0.95,session_limit_ac)

        reward=as_float(e.get('octopoints_per_kwh'))
        return {
            'entity_id':event_entity,
            'event_id':e.get('id'),
            'start':a,'end':b,
            'active':a<=n<b,
            'reward_octopoints_per_kwh':reward,
            'import_baseline_total_kwh':import_baseline_total,
            'export_baseline_total_kwh':export_baseline_total,
            'net_baseline_total_kwh':net_baseline_total,
            'import_baseline_incomplete':import_baseline_incomplete,
            'export_baseline_incomplete':export_baseline_incomplete,
            'protected_soc':protected,
            'post_session_required_kwh':post_kwh,
            'post_session_peak_import_avoidable':avoidable,
            'forecast_session_start_soc':start_soc,
            'max_safe_battery_output_kwh':available_ac,
            'before_next_offpeak':b<=w['start'],
            'after_next_offpeak':a>=w['end'],
        }

    def current_active_offpeak(self):
        n=self.now()
        candidates=[]
        if self.db.ok:
            for key in ('status_plan','next_offpeak'):
                r=self.db.get(key)
                if key=='status_plan' and isinstance(r,dict):r=r.get('offpeak')
                if not isinstance(r,dict):continue
                a=parse_dt(r.get('start')); b=parse_dt(r.get('end')); rate=as_float(r.get('rate_p'))
                if a and b and rate is not None:
                    a=a.astimezone(self.tz); b=b.astimezone(self.tz)
                    if a<=n<b:candidates.append({'start':a,'end':b,'rate_p':rate})
        return max(candidates,key=lambda x:x['start']) if candidates else None
    async def live_soc(self):
        st=await self.ha.state(self.c.get('battery_soc_entity',''))
        return as_float(st.get('state')) if st else None
    def err(self,t,f,m,impact):
        self.errors=[e for e in self.errors if not(e['type']==t and e['field']==f)]+[{'type':t,'field':f,'message':m,'_impact':impact}]; self.errors=sorted(self.errors,key=lambda x:x['_impact'],reverse=True)[:5]
    def clear(self,t=None,f=None):self.errors=[e for e in self.errors if not((t is None or e['type']==t) and (f is None or e['field']==f))]
    async def publish_learning_entities(self):
        if not self.db.ok:return
        rows=self.db.conn.execute('SELECT * FROM learned_bands ORDER BY band_lo').fetchall()
        curve=[]
        for r in rows:
            rate=self.db.conn.execute('''SELECT SUM(cs.requested_rate_w*bo.coverage)/NULLIF(SUM(bo.coverage),0) AS rate_w FROM band_observations bo JOIN charge_sessions cs ON cs.id=bo.session_id WHERE bo.band_lo=? AND bo.band_hi=? AND cs.requested_rate_w>0''',(r['band_lo'],r['band_hi'])).fetchone()
            nominal=as_float(rate['rate_w']) if rate else None
            factor=as_float(r['learned_factor'])
            curve.append({'soc':round((r['band_lo']+r['band_hi'])/2,1),'soc_lo':r['band_lo'],'soc_hi':r['band_hi'],'effective_factor':round(factor,4) if factor is not None else None,'effective_percent':round(factor*100,1) if factor is not None else None,'effective_power_w':round(nominal*factor) if nominal is not None and factor is not None else None,'nominal_requested_power_w':round(nominal) if nominal is not None else None,'confidence':round(float(r['confidence'] or 0),3),'samples':int(r['obs_count'] or 0),'full_equiv':round(float(r['full_equiv'] or 0),3),'p10':round(r['p10'],4) if r['p10'] is not None else None,'p90':round(r['p90'],4) if r['p90'] is not None else None,'updated_at':r['updated_at']})
        latest=max((x['updated_at'] for x in curve if x['updated_at']),default=None)
        total_obs=sum(x['samples'] for x in curve)
        curve_attrs={'friendly_name':'Home Energy Manager Battery Charge Curve','icon':'mdi:chart-bell-curve-cumulative','unit_of_measurement':'bands','curve':curve,'band_count':len(curve),'observation_count':total_obs,'last_learning_update':latest,'note':'effective_power_w is learned effective stored-energy rate at the weighted mean requested charge power observed for that SOC band'}
        curve_entity='sensor.home_energy_manager_battery_charge_curve'
        if not self.mqtt.publish_sensor(curve_entity,len(curve),curve_attrs):await self.ha.publish(curve_entity,len(curve),curve_attrs)
        sessions=self.db.conn.execute('SELECT COUNT(*) AS n, SUM(CASE WHEN eligible=1 THEN 1 ELSE 0 END) AS eligible, SUM(CASE WHEN eligible=0 THEN 1 ELSE 0 END) AS rejected, MAX(ended_at) AS last_session FROM charge_sessions').fetchone()
        learn_attrs={'friendly_name':'Home Energy Manager Battery Learning','icon':'mdi:school','charge_curve_bands':len(curve),'charge_curve_observations':total_obs,'charge_sessions':int(sessions['n'] or 0),'eligible_charge_sessions':int(sessions['eligible'] or 0),'rejected_charge_sessions':int(sessions['rejected'] or 0),'last_charge_session':sessions['last_session'],'last_learning_update':latest,'charge_curve_entity':curve_entity,'discharge_curve_learning':False,'idle_loss_learning':False}
        learn_state='learned' if curve else ('collecting' if sessions['n'] else 'waiting')
        learn_entity='sensor.home_energy_manager_battery_learning'
        if not self.mqtt.publish_sensor(learn_entity,learn_state,learn_attrs):await self.ha.publish(learn_entity,learn_state,learn_attrs)

    async def publish(self,plan=None):
        await self.publish_learning_entities()
        a={'friendly_name':'Home Energy Controller','version':VERSION,'forecast_entity':self.c.get('home_energy_forecast_entity','sensor.home_energy_forecast'),'controller_inputs_ready':self.discovery_ready,'source_forecast_sequence':self.last_source_forecast_sequence,'forecast_refresh':{'request_entity':self.refresh_request_entity or None,'awaiting':bool(self.pending_refresh_request_id),'pending_request_id':self.pending_refresh_request_id or None,'last_ack_sequence':self.last_refresh_ack_sequence or None},'operation_mode':self.operation_mode(),'calibration':self.calibration_attrs(),'last_plan_attempt':iso(self.last_attempt),'last_successful_plan':iso(self.last_success),'changes':self.changes,'last_error':[{k:v for k,v in e.items() if k!='_impact'} for e in self.errors]}
        status=self.db.get('status_plan') if self.db.ok else None
        if plan: status=plan
        if status:a.update(status)
        y=self.yesterday(); a['yesterday']=y
        status_entity=self.c.get('status_entity_id','sensor.home_energy_controller')
        if not self.mqtt.publish_sensor(status_entity,self.health,a):
            await self.ha.publish(status_entity,self.health,a)
    def yesterday(self):
        if not self.db.ok:return {'planned_charge_kwh':0.0,'actual_charge_kwh':0.0,'charge_variance_kwh':0.0,'charge_variance_reason':None,'reached_100':False,'end_offpeak_soc':None,'planned_export_kwh':0.0,'actual_export_kwh':0.0,'export_variance_kwh':0.0,'export_variance_reason':None,'actual_export_end':None}
        d=(self.now().date()-timedelta(days=1)).isoformat(); r=self.db.conn.execute('SELECT * FROM daily_summary WHERE day=?',(d,)).fetchone()
        if not r:return {'planned_charge_kwh':0.0,'actual_charge_kwh':0.0,'charge_variance_kwh':0.0,'charge_variance_reason':None,'reached_100':False,'end_offpeak_soc':None,'planned_export_kwh':0.0,'actual_export_kwh':0.0,'export_variance_kwh':0.0,'export_variance_reason':None,'actual_export_end':None}
        return {'planned_charge_kwh':round(r['planned_charge_kwh'] or 0,1),'actual_charge_kwh':round(r['actual_charge_kwh'] or 0,1),'charge_variance_kwh':round(r['charge_variance_kwh'] or 0,1),'charge_variance_reason':r['charge_variance_reason'],'reached_100':bool(r['reached_100']),'end_offpeak_soc':r['end_offpeak_soc'],'planned_export_kwh':round(r['planned_export_kwh'] or 0,1),'actual_export_kwh':round(r['actual_export_kwh'] or 0,1),'export_variance_kwh':round(r['export_variance_kwh'] or 0,1),'export_variance_reason':r['export_variance_reason'],'actual_export_end':r['actual_export_end']}
    async def num(self,key,persist=None,critical=False):
        st=await self.ha.state(self.c.get(key,'')); x=as_float(st.get('state')) if st else None
        if x is not None:
            if persist:self.db.set(persist,x)
            self.clear('input',key); return x,True
        fb=as_float(self.db.get(persist)) if persist and self.db.ok else None
        if fb is not None:self.err('input',key,'Live value unavailable; using persisted value',70 if critical else 45); return fb,False
        self.err('input',key,'Value unavailable and no persisted fallback' if critical else 'Value unavailable',95 if critical else 45); return None,False
    def valid_forecast(self,s):
        if not s:return False,'Forecast unavailable',None
        a=s.get('attributes',{})
        try:seq=int(a.get('forecast_sequence',0))
        except (TypeError,ValueError):seq=0
        if seq<=0:return False,'Forecast sequence missing/invalid',None
        soc=as_float(a.get('overnight_start_soc_no_slots'))
        ts=parse_dt(a.get('forecast_generated_at') or s.get('last_updated') or s.get('last_changed'))
        no_slots=a.get('forecast_no_slots')
        off=a.get('offpeak')
        if soc is None:return False,'No-slots overnight-start SOC missing/invalid',None
        if not ts:return False,'Forecast timestamp invalid',soc
        ts=ts.astimezone(self.tz); n=self.now()
        if ts<n-timedelta(minutes=int(self.c.get('forecast_stale_minutes',15))):return False,'Forecast stale',soc
        if ts>n+timedelta(minutes=int(self.c.get('future_tolerance_minutes',2))):return False,'Forecast future-dated',soc
        if not isinstance(no_slots,list) or not no_slots:return False,'No-slots forecast missing',soc
        if not isinstance(off,dict):return False,'Forecast off-peak window missing',soc
        a0=parse_dt(off.get('start')); b0=parse_dt(off.get('end')); rate=as_float(off.get('rate_p'))
        if not a0 or not b0 or rate is None or b0<=a0:return False,'Forecast off-peak window invalid',soc
        return True,'',soc
    def intervals(self,s,attr='forecast_no_slots'):
        rows=[]
        for x in s.get('attributes',{}).get(attr,[]):
            st=parse_dt(x.get('start')); rate=as_float(x.get('import_rate_p'))
            if st:rows.append((st.astimezone(self.tz),rate,x))
        rows.sort(key=lambda z:z[0]); out=[]
        for i,(st,r,x) in enumerate(rows):out.append((st,rows[i+1][0] if i+1<len(rows) else st+timedelta(minutes=30),r,x))
        return out
    def offpeak_from_forecast(self,s):
        off=s.get('attributes',{}).get('offpeak')
        if not isinstance(off,dict):return None
        a=parse_dt(off.get('start')); b=parse_dt(off.get('end')); rate=as_float(off.get('rate_p'))
        if not a or not b or rate is None or b<=a:return None
        return {'start':a.astimezone(self.tz),'end':b.astimezone(self.tz),'rate_p':rate}
    def persist_offpeak(self,w):self.db.set('next_offpeak',{'start':iso(w['start']),'end':iso(w['end']),'rate_p':w['rate_p']})
    def fallback_offpeak(self):
        r=self.db.get('next_offpeak') if self.db.ok else None
        if not isinstance(r,dict):return None
        a=parse_dt(r.get('start')); b=parse_dt(r.get('end')); rate=as_float(r.get('rate_p'))
        if not a or not b or rate is None:return None
        a=a.astimezone(self.tz); b=b.astimezone(self.tz); n=self.now()
        while b<=n:
            ad=a.date()+timedelta(days=1); bd=b.date()+timedelta(days=1); a=datetime.combine(ad,a.timetz().replace(tzinfo=None),self.tz); b=datetime.combine(bd,b.timetz().replace(tzinfo=None),self.tz)
        return {'start':a,'end':b,'rate_p':rate}
    def soc_at(self,s,when,attr='forecast_no_slots'):
        best=None
        for a,b,r,x in self.intervals(s,attr):
            v=as_float(x.get('soc'))
            if v is not None and a<=when:best=v
            if v is not None and (a<=when<b or abs((a-when).total_seconds())<=60):return v
        return best
    def forecast_battery_kwh_between(self,s,start,end,attr='forecast_no_slots'):
        """Prorate forecast battery output over an arbitrary interval.

        Positive battery_kwh means discharge, negative means charge.  This is
        used to estimate the *additional* battery depletion caused by a forced
        discharge compared with the no-slots Eco baseline.
        """
        if end<=start:return 0.0
        total=0.0
        for a,b,_rate,x in self.intervals(s,attr):
            ov_start=max(a,start); ov_end=min(b,end)
            if ov_end<=ov_start:continue
            dur=max(1e-9,(b-a).total_seconds())
            frac=(ov_end-ov_start).total_seconds()/dur
            v=as_float(x.get('battery_kwh'))
            if v is not None:total+=v*frac
        return total

    def planned_discharge_soc_adjustment(self,s,ds,de,rate_w,cap,reserve):
        """Extra SOC depletion caused by the controller's forced discharge.

        The no-slots curve already includes normal Eco battery behaviour during
        the same period, so only the *additional* battery output is subtracted.
        The forecaster currently models discharge efficiency at 95%; mirror that
        here so the first controller pass closely predicts the refreshed curve.
        """
        if not ds or not de or de<=ds or rate_w<=0 or cap<=0:return 0.0
        # The current forecast starts from the battery state NOW. If a forced
        # export is already in progress, elapsed discharge must not be counted
        # again when projecting the future charge-start SOC.
        effective_ds=max(ds,self.now())
        if de<=effective_ds:return 0.0
        forced_ac=(de-effective_ds).total_seconds()/3600.0*(rate_w/1000.0)
        baseline_ac=max(0.0,self.forecast_battery_kwh_between(s,effective_ds,de,'forecast_no_slots'))
        extra_ac=max(0.0,forced_ac-baseline_ac)
        stored_kwh=extra_ac/0.95
        max_usable=max(0.0,cap*(100.0-reserve)/100.0)
        stored_kwh=min(stored_kwh,max_usable)
        return 100.0*stored_kwh/cap

    def projected_charge_start_soc(self,s,when,forced_adjustment_pct,reserve):
        base=self.soc_at(s,when,'forecast_no_slots')
        if base is None:return None
        return clamp(base-forced_adjustment_pct,reserve,100.0)

    def latest_charge_start_for_rate(self,s,w,target,rate,cap,reserve,forced_adjustment_pct,earliest=None):
        """Find the latest start that still reaches target by off-peak end.

        SOC is evaluated at the candidate start from forecast_no_slots, adjusted
        for the planned forced discharge.  This naturally includes house demand
        between 23:30 and a delayed charge start such as 02:00.
        """
        margin=float(self.c.get('charge_safety_margin_minutes',10))
        lo=max(w['start'],earliest or w['start'])
        hi=w['end']
        if hi<=lo:return lo,self.projected_charge_start_soc(s,lo,forced_adjustment_pct,reserve),False

        def feasible(at):
            soc=self.projected_charge_start_soc(s,at,forced_adjustment_pct,reserve)
            if soc is None:return False,None
            need=self.charge_minutes(soc,target,rate,cap)+margin
            have=(w['end']-at).total_seconds()/60.0
            return need<=have+1e-6,soc

        ok0,soc0=feasible(lo)
        if not ok0:return lo,soc0,False

        # Binary search the latest feasible instant. One-minute accuracy is
        # finer than the inverter time controls and controller deadband.
        left,right=lo,hi
        best=lo; best_soc=soc0
        for _ in range(24):
            if (right-left).total_seconds()<=30:break
            mid=left+(right-left)/2
            ok,msoc=feasible(mid)
            if ok:
                best=mid; best_soc=msoc; left=mid
            else:
                right=mid
        best=best.replace(second=0,microsecond=0)
        # Rounding down can only add available charge time.
        best_soc=self.projected_charge_start_soc(s,best,forced_adjustment_pct,reserve)
        return best,best_soc,True

    def choose_rate_and_start(self,s,w,target,cap,reserve,forced_adjustment_pct,hw,earliest=None):
        """Choose the lowest allowed charge rate that fits, then latest start."""
        preferred=cap*1000*float(self.c.get('preferred_charge_c_rate',.25))
        maximum=cap*1000*float(self.c.get('max_charge_c_rate',.4))
        if hw:maximum=min(maximum,hw)
        preferred=min(preferred,maximum)
        earliest=max(w['start'],earliest or w['start'])

        def solve(rate):
            return self.latest_charge_start_for_rate(
                s,w,target,rate,cap,reserve,forced_adjustment_pct,earliest
            )

        start,soc,ok=solve(preferred)
        if ok:return int(round(preferred)),start,soc,True
        if maximum<=preferred:
            start,soc,ok=solve(maximum)
            return int(round(maximum)),start,soc,ok

        # If maximum cannot fit, use it from the earliest permitted point and
        # report unattainable via the boolean.
        max_start,max_soc,max_ok=solve(maximum)
        if not max_ok:return int(round(maximum)),earliest,max_soc,False

        lo,hi=preferred,maximum
        best=(maximum,max_start,max_soc)
        for _ in range(24):
            mid=(lo+hi)/2
            st,ss,ok=solve(mid)
            if ok:
                best=(mid,st,ss); hi=mid
            else:
                lo=mid
        return int(round(best[0])),best[1],best[2],True

    def band_factor(self,band):
        if not self.db.ok:return GENERIC[band]
        r=self.db.conn.execute('SELECT * FROM learned_bands WHERE band_lo=? AND band_hi=?',band).fetchone()
        if not r or r['learned_factor'] is None:return GENERIC[band]
        c=clamp(r['confidence'],0,1); return GENERIC[band]*(1-c)+r['learned_factor']*c
    def dwell(self):
        g=float(self.c.get('generic_dwell_minutes',15)); x=as_float(self.db.get('learned_dwell_minutes')) if self.db.ok else None; c=as_float(self.db.get('learned_dwell_confidence')) if self.db.ok else 0
        return g if x is None else g*(1-clamp(c or 0,0,1))+x*clamp(c or 0,0,1)
    def charge_minutes(self,soc,target,rate,cap):
        if rate<=0 or target<=soc:return 0.0
        mins=0
        for lo,hi in SOC_BANDS:
            ov=max(0,min(target,hi)-max(soc,lo))
            if ov:mins+=(cap*(ov/100))/((rate/1000)*max(.05,self.band_factor((lo,hi))))*60
        if target>=100:mins+=self.dwell()
        return mins
    def choose_rate(self,soc,target,cap,w,hw):
        p=cap*1000*float(self.c.get('preferred_charge_c_rate',.25)); mx=cap*1000*float(self.c.get('max_charge_c_rate',.4)); mx=min(mx,hw) if hw else mx; p=min(p,mx); avail=(w['end']-w['start']).total_seconds()/60; margin=float(self.c.get('charge_safety_margin_minutes',10))
        fit=lambda r:self.charge_minutes(soc,target,r,cap)+margin<=avail
        if fit(p):return int(round(p))
        if mx<=p or not fit(mx):return int(round(mx))
        lo,hi=p,mx
        for _ in range(24):
            m=(lo+hi)/2
            if fit(m):hi=m
            else:lo=m
        return int(round(hi))
    def export_start(self,w):
        hh,mm=map(int,str(self.c.get('export_start','20:00')).split(':')[:2]); d=w['start'].date(); x=datetime.combine(d,time(hh,mm),self.tz)
        if x>=w['start']:x-=timedelta(days=1)
        return x
    def tstr(self,dt):
        # GivTCP schedule selectors are minute-resolution. Forecast boundaries
        # may retain seconds (for example a startup forecast beginning at
        # 11:30:27); sending that value to select.select_option is invalid.
        return dt.astimezone(self.tz).replace(second=0,microsecond=0).strftime('%H:%M:%S')
    def daily_slot_active(self,start_value,end_value,when=None):
        """True when a HH:MM[:SS] daily slot has already started and not ended."""
        def parts(v):
            try:
                bits=[int(x) for x in str(v).split(':')[:3]]
                while len(bits)<3:bits.append(0)
                h,m,sec=bits
                if not (0<=h<24 and 0<=m<60 and 0<=sec<60):return None
                return h*3600+m*60+sec
            except:return None
        a=parts(start_value); b=parts(end_value)
        if a is None or b is None or a==b:return False
        n=(when or self.now()).astimezone(self.tz)
        x=n.hour*3600+n.minute*60+n.second
        return a<=x<b if a<b else (x>=a or x<b)
    async def preserve_active_slot_start(self,label,start_entity,end_entity,desired_start):
        """Keep the observed start of an already-running inverter slot.

        Replanning may move a desired start to 'now'. Once the inverter is inside
        the existing slot, rewriting its start is unnecessary and can disturb a
        running charge/discharge/pause session. End time/target/rate remain mutable.
        """
        a=await self.ha.state(start_entity); b=await self.ha.state(end_entity)
        observed_start=str((a or {}).get('state') or '')
        observed_end=str((b or {}).get('state') or '')
        if self.daily_slot_active(observed_start,observed_end):
            keep=self.tstr(datetime.combine(self.now().date(),time.fromisoformat(observed_start),self.tz)) if observed_start else desired_start
            if str(keep)!=str(desired_start):
                LOG.info('Preserving active %s slot start: observed=%s desired=%s end=%s',label,keep,desired_start,observed_end)
            return keep
        return desired_start
    def export_generated_solar_window(self,s,day=None):
        """Return one continuous forecast-only daytime solar window.

        Export Generated prefers PV to reach the grid directly rather than
        cycling it through the battery.  Use only the forecaster's no-slots PV
        curve: instantaneous inverter PV must not make PauseCharge chatter as
        clouds pass.  The threshold is expressed as equivalent average watts so
        it remains consistent if forecast slot resolution changes.  Small/zero
        forecast gaps between the first and last meaningful slots are absorbed
        into one continuous window.
        """
        if not s:return None
        target_day=day or self.now().date()
        threshold_w=max(0.0,float(self.c.get('export_generated_solar_threshold_w',100)))
        meaningful=[]
        for a,b,_rate,x in self.intervals(s,'forecast_no_slots'):
            if a.astimezone(self.tz).date()!=target_day:continue
            pv=as_float(x.get('pv_kwh')) or 0.0
            duration_h=max(0.0,(b-a).total_seconds()/3600.0)
            threshold_kwh=(threshold_w/1000.0)*duration_h
            if pv+1e-9>=threshold_kwh and pv>0:
                meaningful.append((a,b))
        if not meaningful:return None
        return {'start':meaningful[0][0],'end':meaningful[-1][1]}

    def pause_plan(self,w,s=None):
        if not w:return {'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
        mode=self.operation_mode()
        if mode=='maximise_export':
            return {'mode':'PauseCharge','start':self.tstr(w['end']),'end':self.tstr(w['start'])}
        if mode=='minimise_export':
            return {'mode':'PauseDischarge','start':self.tstr(w['start']),'end':self.tstr(w['end'])}
        if mode=='export_generated':
            solar=self.export_generated_solar_window(s)
            if solar:
                return {'mode':'PauseCharge','start':self.tstr(solar['start']),'end':self.tstr(solar['end'])}
        return {'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
    async def readable(self,e):
        s=await self.ha.state(e); v=s.get('state') if s else None; return (v,v not in (None,'','unknown','unavailable'))
    async def rate_diagnostics(self,requested_w,observed_w):
        cap,_=await self.num('battery_capacity_entity','battery_capacity',False)
        req=as_float(requested_w); obs=as_float(observed_w)
        if cap is None or cap<=0:return None
        return {
            'capacity_kwh':cap,
            'requested_w':req,
            'observed_w':obs,
            'requested_c_pct':(req/(cap*1000.0))*100.0 if req is not None else None,
            'observed_c_pct':(obs/(cap*1000.0))*100.0 if obs is not None else None,
            'step_w_per_1pct_c':cap*10.0,
        }
    def match(self,f,o,d):
        if not o:return False
        if f.endswith('rate'):
            a,b=as_float(o),as_float(d)
            if a is None or b is None:return False
            # GivEnergy/GivTCP rate readback can be quantised relative to the
            # battery C-rate rather than reproducing the requested watt value
            # exactly. For testing, accept a small approximation window.
            tol=max(float(self.c.get('rate_match_tolerance_w',100)),
                    abs(b)*float(self.c.get('rate_match_tolerance_pct',5))/100.0)
            return abs(a-b)<=tol
        if f.endswith('target'):
            a,b=as_float(o),as_float(d); return a is not None and b is not None and abs(a-b)<.5
        if f.endswith('start') or f.endswith('end'):return str(o)[:5]==str(d)[:5]
        return str(o).lower()==str(d).lower()
    async def ensure(self,f,e,d,wend=None):
        loop=asyncio.get_running_loop()
        ensure_started=loop.time()
        read_started=loop.time()
        o,rd=await self.readable(e)
        initial_read=loop.time()-read_started
        if not rd:
            LOG.warning('Write timing: field=%s entity=%s initial_read=%.3fs readback=unavailable',f,e,initial_read)
            self.err('input',f,'Readback unavailable; field unverified',65); return False
        if self.match(f,o,d):
            self.clear('write',f)
            return True
        att=int(self.c.get('write_retry_attempts',4)); base_delay=int(self.c.get('write_retry_delay_seconds',10))
        LOG.info('Write needed: field=%s entity=%s observed=%r desired=%r initial_read=%.3fs attempts=%d retry_backoff=%ds',
                 f,e,o,d,initial_read,att,base_delay)
        if f.endswith('rate'):
            diag=await self.rate_diagnostics(d,o)
            if diag:
                LOG.info('Rate diagnostic: field=%s capacity=%.3fkWh requested=%sW (%.2f%%C) observed=%sW (%.2f%%C) theoretical_1pctC_step=%.1fW',
                         f,diag['capacity_kwh'],
                         f"{diag['requested_w']:.0f}" if diag['requested_w'] is not None else 'n/a',
                         diag['requested_c_pct'] if diag['requested_c_pct'] is not None else float('nan'),
                         f"{diag['observed_w']:.0f}" if diag['observed_w'] is not None else 'n/a',
                         diag['observed_c_pct'] if diag['observed_c_pct'] is not None else float('nan'),
                         diag['step_w_per_1pct_c'])
        for i in range(1,att+1):
            attempt_started=loop.time()
            write_started=loop.time()
            write_error=None
            try:await self.ha.write(e,d)
            except Exception as ex:
                write_error=ex
                LOG.warning('Write %s attempt %d: %s',f,i,ex)
            write_elapsed=loop.time()-write_started
            await asyncio.sleep(2)
            confirm_started=loop.time()
            o,rd=await self.readable(e)
            confirm_elapsed=loop.time()-confirm_started
            ok=rd and self.match(f,o,d)
            attempt_elapsed=loop.time()-attempt_started
            LOG.info('Write result: field=%s attempt=%d/%d write=%.3fs confirm_read=%.3fs attempt_total=%.3fs observed=%r desired=%r confirmed=%s%s',
                     f,i,att,write_elapsed,confirm_elapsed,attempt_elapsed,o,d,'yes' if ok else 'no',
                     f' error={write_error}' if write_error else '')
            if f.endswith('rate'):
                diag=await self.rate_diagnostics(d,o)
                if diag:
                    diff=abs((diag['observed_w'] or 0)-(diag['requested_w'] or 0))
                    tol=max(float(self.c.get('rate_match_tolerance_w',100)),
                            abs(diag['requested_w'] or 0)*float(self.c.get('rate_match_tolerance_pct',5))/100.0)
                    LOG.info('Rate diagnostic: field=%s requested=%sW %.2f%%C observed=%sW %.2f%%C diff=%.0fW tolerance=%.0fW confirmed=%s',
                             f,
                             f"{diag['requested_w']:.0f}" if diag['requested_w'] is not None else 'n/a',
                             diag['requested_c_pct'] if diag['requested_c_pct'] is not None else float('nan'),
                             f"{diag['observed_w']:.0f}" if diag['observed_w'] is not None else 'n/a',
                             diag['observed_c_pct'] if diag['observed_c_pct'] is not None else float('nan'),
                             diff,tol,'yes' if ok else 'no')
            if self.db.ok:self.db.conn.execute('INSERT INTO write_audit(ts,field,entity_id,requested,observed,success,retry_count) VALUES(?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),f,e,json.dumps(d),json.dumps(o),1 if ok else 0,i)); self.db.conn.commit()
            if ok:
                self.confirmed_writes_this_apply+=1
                self.clear('write',f)
                LOG.info('Write timing: field=%s completed=%.3fs attempts=%d',f,loop.time()-ensure_started,i)
                return True
            if i<att:
                # Progressive linear backoff between write attempts.  The
                # confirmation read above is deliberately separate from this
                # delay, so a successful but slowly-propagating write is not
                # immediately hammered again.  With the defaults, retries wait
                # 10s, 20s, then 30s after the initial attempt.
                wait=max(0,base_delay*i)
                LOG.warning('Write retry wait: field=%s attempt=%d/%d sleeping=%ds observed=%r desired=%r',
                            f,i,att,wait,o,d)
                await asyncio.sleep(wait)
        total=loop.time()-ensure_started
        LOG.warning('Write timing: field=%s FAILED total=%.3fs attempts=%d',f,total,att)
        self.err('write',f,'Write not confirmed after retries',80)
        if self.db.ok:self.db.conn.execute("INSERT INTO pending_writes(field,entity_id,desired,window_end,retry_count,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(field) DO UPDATE SET entity_id=excluded.entity_id,desired=excluded.desired,window_end=excluded.window_end,retry_count=excluded.retry_count,updated_at=excluded.updated_at",(f,e,json.dumps(d),iso(wend),att,datetime.now(timezone.utc).isoformat())); self.db.conn.commit()
        return False
    async def safe(self,w,cap=None,hw=None):
        n=self.now(); es=self.export_start(w) if w else n.replace(second=0,microsecond=0); pause=self.pause_plan(w)
        pause_start=await self.preserve_active_slot_start('pause',self.c['pause_start_entity'],self.c['pause_end_entity'],pause['start'])
        discharge_start=await self.preserve_active_slot_start('discharge',self.c['discharge_slot_1_start_entity'],self.c['discharge_slot_1_end_entity'],self.tstr(es))
        fields=[('eco',self.c['eco_mode_entity'],'on',None),('charge_enable',self.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',self.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',self.c['pause_mode_entity'],pause['mode'],w['end'] if w else None),('pause_start',self.c['pause_start_entity'],pause_start,w['end'] if w else None),('pause_end',self.c['pause_end_entity'],pause['end'],w['end'] if w else None),('discharge_start',self.c['discharge_slot_1_start_entity'],discharge_start,w['end'] if w else None),('discharge_end',self.c['discharge_slot_1_end_entity'],self.tstr(es),w['end'] if w else None)]
        reserve,_=await self.num('battery_reserve_entity','battery_reserve',False)
        if reserve is not None:fields.append(('discharge_target',self.c['discharge_slot_1_target_entity'],int(round(reserve)),w['end'] if w else None))
        if w and cap:
            charge_start=await self.preserve_active_slot_start('charge',self.c['charge_slot_1_start_entity'],self.c['charge_slot_1_end_entity'],self.tstr(w['start']))
            rate=cap*1000*float(self.c.get('preferred_charge_c_rate',.25)); rate=min(rate,hw) if hw else rate; fields += [('charge_start',self.c['charge_slot_1_start_entity'],charge_start,w['end']),('charge_end',self.c['charge_slot_1_end_entity'],self.tstr(w['end']),w['end']),('charge_target',self.c['charge_slot_1_target_entity'],100,w['end']),('charge_rate',self.c['charge_rate_entity'],int(round(rate)),w['end'])]
        return all([await self.ensure(*x) for x in fields])
    async def plan(self,s,soc,w,fallback=False):
        cap,_=await self.num('battery_capacity_entity','battery_capacity',True); reserve,_=await self.num('battery_reserve_entity','battery_reserve',True); hwc,_=await self.num('inverter_max_charge_rate_entity','max_charge_rate',False); hwd,_=await self.num('inverter_max_discharge_rate_entity','max_discharge_rate',False)
        if cap is None or reserve is None:return None
        start_soc=as_float(s.get('attributes',{}).get('overnight_start_soc_no_slots'))
        if start_soc is None:start_soc=self.soc_at(s,w['start'],'forecast_no_slots')
        if start_soc is None:self.err('planning','charge','Cannot determine no-slots charge-start SOC',95); return None
        # This is the forecaster's predicted SOC at the tariff-derived start of
        # the next regular off-peak window.  Keep the semantic name explicit:
        # confirmed Intelligent charging uses the forecast arrival shortfall,
        # not live SOC, to protect Rule 1 (avoid peak-rate import where possible).
        offpeak_arrival_soc=float(start_soc)

        mode=self.operation_mode(); cal=self.calibration_state(); n=self.now()
        normal_pause=self.pause_plan(w,s)
        solar_pause_window=self.export_generated_solar_window(s) if mode=='export_generated' else None
        intelligent=await self.intelligent_go_info() if not fallback else {'enabled':False,'confirmed':False}
        intelligent_planned=await self.intelligent_planned_slots() if not fallback else []
        min_soc=int(self.c.get('minimise_export_min_soc',25))
        if mode=='export_generated' and not fallback:
            await self.update_effective_export_accounting()
        export_info=self.export_generated_inputs(s) if mode=='export_generated' else None
        if mode=='export_generated' and export_info is None:
            self.err('planning','export_generated','Forecast today.total.pv_kwh unavailable',90)
            return None
        if mode=='export_generated' and export_info is not None and intelligent.get('confirmed'):
            ia=intelligent.get('slot_start'); ib=intelligent.get('slot_end')
            uncreditable_future=self.forecast_export_kwh_between(s,ia,ib,'forecast_no_slots') if ia and ib else 0.0
            if uncreditable_future>0:
                export_info['forecast_natural_export_kwh']=max(
                    0.0,float(export_info.get('forecast_natural_export_kwh') or 0.0)-uncreditable_future
                )
                export_info['remaining_kwh']=max(
                    0.0,float(export_info.get('solar_target_kwh') or 0.0)
                    -float(export_info.get('exported_so_far_kwh') or 0.0)
                    -float(export_info.get('forecast_natural_export_kwh') or 0.0)
                )
                export_info['uncreditable_confirmed_car_slot_forecast_export_kwh']=uncreditable_future

        # Default overnight target policy.
        if fallback:
            target=100
        elif cal in ('top_due','deep_recharge'):
            target=100
        elif mode in ('maximise_export','export_generated'):
            target=100
        elif cal=='awaiting_deep_low':
            target=int(round(reserve))
        elif start_soc<min_soc:
            target=min_soc
        else:
            target=int(round(start_soc))

        # Use a current active cheap window for export-generated catch-up.
        active=self.current_active_offpeak() if mode=='export_generated' else None
        control_w=active or w
        live_soc=await self.live_soc() if active else None

        # Discharge power.
        dr=int(self.c.get('discharge_rate_w',0))
        if dr>0:dr=min(dr,int(hwd)) if hwd else dr
        else:dr=int(hwd or self.db.get('max_discharge_rate') or cap*1000*float(self.c.get('max_charge_c_rate',.4)))
        es=self.export_start(control_w)
        de=es
        discharge_kind='none'
        planned_export=0.0
        discharge_target_soc=int(round(reserve))

        # BottlecapDave Power Down sessions are a high-value override. A joined
        # session before the next cheap period owns discharge slot 1. The target
        # is not the Octopus baseline: export runs at maximum battery power down
        # to a dynamically protected SOC that still carries the house through to
        # cheap rate without forecast peak-price import.
        power_down=await self.power_down_info(s,w,cap,reserve,dr) if not fallback else None

        # If a joined session is after tonight's cheap window, fill to 100%
        # overnight regardless of normal mode so the next planning cycle has the
        # maximum energy available for the paid session.
        if power_down and power_down.get('after_next_offpeak'):
            target=100

        # Deep calibration deliberately drives to reserve with a long window.
        # The BMS is expected to stop discharge and idle once reserve is reached.
        if power_down and power_down.get('before_next_offpeak'):
            es=max(power_down['start'],n).replace(second=0,microsecond=0)
            discharge_kind='power_down'
            safe_kwh=max(0.0,power_down.get('max_safe_battery_output_kwh') or 0.0)
            # Never manipulate the inverter discharge target. It stays at the
            # configured battery reserve; exported energy is controlled solely
            # by how long the forced-discharge slot is active.
            discharge_target_soc=int(round(reserve))
            max_end=power_down['end'].replace(second=0,microsecond=0)
            if dr>0 and safe_kwh>0:
                minutes=safe_kwh/(dr/1000.0)*60.0
                de=min(max_end,es+timedelta(minutes=minutes))
                if de>es:
                    de=de.replace(second=0,microsecond=0)
                    if de<=es:de=es+timedelta(minutes=1)
            else:
                de=es
            planned_export=min(
                safe_kwh,
                max(0.0,(de-es).total_seconds()/3600.0*(dr/1000.0))
            )
        elif not fallback and cal=='awaiting_deep_low':
            es=min(es,control_w['start'])
            de=control_w['end'].replace(second=0,microsecond=0)
            discharge_kind='to_reserve'
            planned_export=max(0.0,cap*(max(start_soc,reserve)-reserve)/100.0)
        elif fallback or mode=='minimise_export':
            de=es
        elif mode=='maximise_export':
            buffer=int(self.c.get('safety_buffer_soc',20))
            surplus_soc=max(0.0,soc-buffer); exportable_kwh=cap*(surplus_soc/100.0)
            wanted=(exportable_kwh/(dr/1000))*60 if dr else 0
            wanted=min(max(0,wanted),max(0,(control_w['start']-es).total_seconds()/60))
            de=(es+timedelta(minutes=wanted)).replace(second=0,microsecond=0)
            planned_export=max(0.0,(de-es).total_seconds()/3600*(dr/1000))
        elif mode=='export_generated':
            remaining=export_info['remaining_kwh']
            export_solve_start=None
            export_solve_reachable=True
            export_solve_forecast_kwh=0.0

            if active:
                # Never pre-export today's forecast solar generation during the
                # regular overnight cheap window.
                es=self.disabled_discharge_time(w)
                de=es
                discharge_kind='none'
                planned_export=0.0
                export_solve_reachable=False if remaining>0 else True
            else:
                # Pre-cheap phase: solve the full generation-matched export,
                # then cap its END time by the SOC guardrail.  The guardrail is
                # applied to predicted battery depletion, not exported kWh.
                if remaining>0 and dr>0:
                    solve_start=max(n,es)
                    export_solve_start=solve_start
                    target_end,target_export,target_reachable=self.export_generated_end(
                        s,solve_start,control_w['start'],remaining,dr
                    )
                    guardrail_end,guardrail_export=self.export_generated_guardrail_end(
                        s,solve_start,control_w['start'],dr,soc,cap,reserve
                    )
                    de=min(target_end,guardrail_end)
                    forecast_incremental=self.forced_incremental_grid_export_kwh(
                        s,solve_start,de,dr
                    ) if de>solve_start else 0.0
                    export_solve_forecast_kwh=forecast_incremental
                    export_solve_reachable=bool(target_reachable and target_end<=guardrail_end)
                    discharge_kind='timed' if de>solve_start else 'none'
                    planned_export=min(remaining,forecast_incremental)
                else:
                    de=es
                    discharge_kind='none'
                    planned_export=0.0

        # Planned Intelligent dispatches are advisory only, but they block the
        # START of a new normal export. This avoids beginning an export shortly
        # before a likely car-charging slot and then exporting into the car.
        #
        # Important distinctions:
        #   * an already-active export is NOT stopped for a merely planned slot;
        #   * a confirmed Intelligent slot still pre-empts active export;
        #   * Power Down export is not blocked by a merely planned Intelligent
        #     slot because Power Down is a known paid event.
        planned_block=None
        normal_export_kind=discharge_kind not in ('none','power_down','intelligent_export_suspended')
        export_active_now=(de>es and es<=n<de)
        if normal_export_kind and de>es and not export_active_now:
            for pa,pb,_pe in intelligent_planned:
                if es<pb and de>pa:
                    planned_block=(pa,pb)
                    break
            if planned_block:
                # Defer this export until after the planned dispatch. Do not
                # assume the dispatch is real; simply avoid starting export now.
                # The controller will replan at/after the boundary from fresh
                # export totals and SOC.
                LOG.info('Deferring export %s-%s because Intelligent dispatch is planned %s-%s',
                         es.strftime('%H:%M'),de.strftime('%H:%M'),
                         planned_block[0].strftime('%H:%M'),planned_block[1].strftime('%H:%M'))
                es=planned_block[1].replace(second=0,microsecond=0)
                de=es
                planned_export=0.0
                discharge_kind='intelligent_planned_export_deferred'

        # Canonical no-discharge representation.
        #
        # `control_w` may switch to the currently-active cheap window once
        # off-peak begins. That must not turn a disabled evening slot from
        # 20:00-20:00 into a wrapped 23:30-20:00 slot. For kind=none, always
        # use one stable zero-duration slot based on the normal off-peak window.
        if discharge_kind=='none':
            disabled_at=self.disabled_discharge_time(w)
            if es!=disabled_at or de!=disabled_at:
                LOG.info('Discharge disabled: kind=none slot=%s-%s reason=no_discharge_required',
                         disabled_at.strftime('%H:%M'),disabled_at.strftime('%H:%M'))
            es=disabled_at
            de=disabled_at
            planned_export=0.0

        # Charge planning. During a deep-low discharge or full-to-reserve
        # export-generated catch-up, do not start charging until reserve has
        # actually been observed. This avoids relying on predicted time-to-floor.
        low_reached=parse_dt(self.db.get('calibration_low_reached_at')) if self.db.ok else None
        at_reserve=(live_soc is not None and live_soc<=reserve+0.5)
        waiting_for_floor=(cal=='awaiting_deep_low')
        export_waiting_floor=(mode=='export_generated' and active and discharge_kind=='to_reserve' and not at_reserve)

        # The no-slots curve includes normal Eco behaviour but not the evening
        # forced discharge we are about to write. Estimate the extra SOC loss
        # caused by that discharge, then use the no-slots SOC at the *actual
        # candidate charge start*. This avoids assuming that SOC remains fixed
        # from 23:30 until a delayed charge start.
        forced_soc_adjustment=0.0
        if not fallback and de>es and dr>0:
            forced_soc_adjustment=self.planned_discharge_soc_adjustment(
                s,es,de,dr,cap,reserve
            )

        estimated_charge_start_soc=start_soc
        charge_can_reach_target=True
        if waiting_for_floor or export_waiting_floor:
            needs_charge=False
            rate=int(min(hwc,cap*1000*float(self.c.get('preferred_charge_c_rate',.25))) if hwc else cap*1000*float(self.c.get('preferred_charge_c_rate',.25)))
            cstart=control_w['start']; cend=cstart; planned_charge=0.0
        else:
            earliest=control_w['start']
            # A timed cheap-rate catch-up must finish before charging begins.
            if mode=='export_generated' and active and discharge_kind=='timed':
                earliest=max(earliest,de)
            if cal=='deep_recharge' and low_reached:
                dwell_until=low_reached.astimezone(self.tz)+timedelta(minutes=int(self.c.get('reserve_dwell_minutes',30)))
                earliest=max(earliest,dwell_until)

            initial_soc=live_soc if active and live_soc is not None else self.projected_charge_start_soc(
                s,earliest,forced_soc_adjustment,reserve
            )
            if initial_soc is None:initial_soc=start_soc
            needs_charge=target>initial_soc+0.5

            if needs_charge:
                if fallback:
                    # With no valid no-slots curve use the conservative full
                    # window behaviour retained from the existing safe fallback.
                    rate=self.choose_rate(initial_soc,target,cap,control_w,hwc)
                    cstart=earliest
                    estimated_charge_start_soc=initial_soc
                elif active and live_soc is not None:
                    # During the current cheap window, base planning on live SOC
                    # and the remaining window. House-use projection before a
                    # delayed start is unavailable once the no-slots horizon no
                    # longer represents the pre-catchup state, so favour starting
                    # from the earliest permitted point.
                    rem={'start':earliest,'end':control_w['end'],'rate_p':control_w['rate_p']}
                    rate=self.choose_rate(live_soc,target,cap,rem,hwc)
                    mins=self.charge_minutes(live_soc,target,rate,cap)+float(self.c.get('charge_safety_margin_minutes',10))
                    cstart=max(earliest,control_w['end']-timedelta(minutes=mins))
                    estimated_charge_start_soc=live_soc
                else:
                    rate,cstart,estimated_charge_start_soc,charge_can_reach_target=self.choose_rate_and_start(
                        s,control_w,target,cap,reserve,forced_soc_adjustment,hwc,earliest
                    )
                    if not charge_can_reach_target:
                        self.err('planning','charge','Target SOC cannot be reached within off-peak window even at maximum charge rate',75)
                cend=control_w['end'].replace(second=0,microsecond=0)
                planned_charge=cap*max(0,target-(estimated_charge_start_soc if estimated_charge_start_soc is not None else initial_soc))/100
            else:
                rate=int(min(hwc,cap*1000*float(self.c.get('preferred_charge_c_rate',.25))) if hwc else cap*1000*float(self.c.get('preferred_charge_c_rate',.25)))
                cstart=control_w['start']; cend=cstart; planned_charge=0.0
                estimated_charge_start_soc=initial_soc

        # Preserve an already-active charge start/rate.
        if self.confirmed:
            cs=parse_dt(self.confirmed.get('charge_start')); ce=parse_dt(self.confirmed.get('charge_end'))
            if cs and ce and cs.astimezone(self.tz)<=n<ce.astimezone(self.tz) and not waiting_for_floor and not export_waiting_floor:
                cstart=cs.astimezone(self.tz); cend=control_w['end'].replace(second=0,microsecond=0); rate=int(self.confirmed.get('charge_rate_w',rate)); target=int(self.confirmed.get('charge_target_soc',target)); needs_charge=True

        # Confirmed Intelligent Go is a hard current-slot override.
        #
        charge_slot_target=int(target)

        # A confirmed slot is cheap for the whole enclosing 30-minute settlement
        # period. Never force export during it.
        #
        # Intelligent Go overlays the normal plan. If no extra charge is useful,
        # preserve the normal future charge slot and just PauseDischarge. If the
        # current confirmed half-hour is genuinely useful for cheap charging,
        # temporarily use slot 1 for that half-hour; the next control pass
        # restores/replans the normal overnight charge.
        intelligent_pause_mode='Disabled'
        intelligent_charge_reason=None
        intelligent_charge_target=None
        intelligent_export_topup=None
        if intelligent.get('confirmed'):
            ia=intelligent['slot_start']; ib=intelligent['slot_end']

            if de>es and es<ib and de>ia:
                if es<ia:
                    de=ia
                else:
                    de=es
                planned_export=max(0.0,(de-es).total_seconds()/3600.0*(dr/1000.0))
                discharge_kind='intelligent_export_suspended'

            ils=await self.live_soc()
            if ils is None:ils=start_soc

            buffer_soc=max(float(reserve),float(self.c.get('safety_buffer_soc',20)))
            required_delta_pct=0.0
            inside_regular_offpeak=bool(self.current_active_offpeak())

            # A confirmed Intelligent half-hour outside the regular cheap
            # window is useful when the no-slots forecast would arrive at the
            # next tariff-derived off-peak start below the safety buffer.  Add
            # exactly that forecast-arrival shortfall.  Do not duplicate the
            # forecaster's ASHP/load/PV/battery simulation in the controller.
            if not inside_regular_offpeak and offpeak_arrival_soc < buffer_soc-0.5:
                required_delta_pct=max(required_delta_pct,buffer_soc-offpeak_arrival_soc)
                intelligent_charge_reason='protect_offpeak_arrival'

            if power_down and power_down.get('before_next_offpeak'):
                pd_start_soc=power_down.get('forecast_session_start_soc')
                pd_protected=power_down.get('protected_soc')
                if pd_start_soc is not None and pd_protected is not None and pd_start_soc < pd_protected-0.5:
                    required_delta_pct=max(required_delta_pct,pd_protected-pd_start_soc)
                    intelligent_charge_reason='prepare_power_down'

            if mode=='export_generated' and not inside_regular_offpeak and export_info is not None:
                intelligent_export_topup=self.export_generated_topup_need(
                    s,export_info,soc,cap,reserve,dr,w
                )
                needed_stored=float(intelligent_export_topup.get('needed_stored_kwh') or 0.0)
                if needed_stored>0.05:
                    required_delta_pct=max(required_delta_pct,needed_stored/cap*100.0)
                    intelligent_charge_reason='export_generated_topup'

            if not charge_can_reach_target:
                required_delta_pct=max(required_delta_pct,max(0.0,target-ils))
                intelligent_charge_reason='overnight_target_unreachable'

            if cal=='deep_recharge':
                required_delta_pct=max(required_delta_pct,max(0.0,target-ils))
                intelligent_charge_reason='calibration_deep_recharge'

            if required_delta_pct>0.5 and n<ib:
                intelligent_pause_mode='Disabled'
                intelligent_charge_target=int(round(clamp(ils+required_delta_pct,reserve,100)))
                remaining_window={'start':max(n,ia),'end':ib,'rate_p':control_w['rate_p']}
                rate=self.choose_rate(ils,intelligent_charge_target,cap,remaining_window,hwc)
                cstart=ia
                cend=ib
                charge_slot_target=intelligent_charge_target

                available_minutes=max(0.0,(ib-n).total_seconds()/60.0)
                possible_stored=(rate/1000.0)*(available_minutes/60.0)*0.95
                required_stored=cap*max(0.0,intelligent_charge_target-ils)/100.0
                planned_charge=min(required_stored,possible_stored)
                needs_charge=True
                estimated_charge_start_soc=ils
            else:
                # Normally a confirmed cheap half-hour uses PauseDischarge so
                # the house consumes cheap grid rather than stored energy.  In
                # Export Generated's forecast solar window, however, preserving
                # PauseCharge is more valuable: surplus PV can go directly to
                # grid instead of being stored and exported later with losses.
                # A genuinely required Intelligent battery charge above still
                # overrides this and disables PauseCharge for the half-hour.
                solar_pause_active=bool(
                    mode=='export_generated' and solar_pause_window and
                    ia < solar_pause_window['end'] and ib > solar_pause_window['start']
                )
                if solar_pause_active:
                    intelligent_pause_mode='PauseCharge'
                    intelligent_charge_reason='preserve_direct_solar_export'
                else:
                    intelligent_pause_mode='PauseDischarge'
                    intelligent_charge_reason='battery_energy_sufficient'
                intelligent_charge_target=None

        result={
            'offpeak':{'start':iso(control_w['start']),'end':iso(control_w['end']),'rate_p':round(control_w['rate_p'],4)},
            'pause':normal_pause,
            'charge':{'start':iso(cstart.replace(second=0,microsecond=0)),'end':iso(cend),'rate_w':rate,'target_soc':int(charge_slot_target),'planned_kwh':round(planned_charge,3)},
            'discharge':{'start':iso(es.replace(second=0,microsecond=0)),'end':iso(de),'rate_w':dr,'target_soc':int(discharge_target_soc),'planned_kwh':round(planned_export,3),'kind':discharge_kind},
            'forecast':{
                'overnight_start_soc_no_slots':int(round(soc)),
                'planned_discharge_soc_adjustment_pct':round(forced_soc_adjustment,1),
                'estimated_charge_start_soc':int(round(estimated_charge_start_soc)) if estimated_charge_start_soc is not None else None,
                'charge_target_reachable':bool(charge_can_reach_target),
            },
            'operation':{'mode':mode},
            'calibration':self.calibration_attrs(),
            'intelligent_go':{
                'enabled':bool(intelligent.get('enabled')),
                'dispatch_entity':str(self.c.get('intelligent_dispatch_entity','')).strip() or None,
                'planned_dispatches':[{'start':iso(a),'end':iso(b)} for a,b,_e in intelligent_planned],
                'export_deferred_for_planned':bool(planned_block),
                'planned_block_start':iso(planned_block[0]) if planned_block else None,
                'planned_block_end':iso(planned_block[1]) if planned_block else None,
                'confirmation_entity':intelligent.get('entity_id'),
                'sensor_state':intelligent.get('sensor_state'),
                'sensor_on':bool(intelligent.get('sensor_on')),
                'confirmed':bool(intelligent.get('confirmed')),
                'slot_start':iso(intelligent.get('slot_start')),
                'slot_end':iso(intelligent.get('slot_end')),
                'whole_half_hour_cheap':bool(intelligent.get('whole_half_hour_cheap')),
                'export_suspended':bool(intelligent.get('export_suspended')),
                'pause_mode':intelligent_pause_mode if intelligent.get('confirmed') else None,
                'charge_reason':intelligent_charge_reason if intelligent.get('confirmed') else None,
                'intelligent_charge_target_soc':intelligent_charge_target if intelligent.get('confirmed') else None,
                'export_generated_topup_needed_stored_kwh':round((intelligent_export_topup or {}).get('needed_stored_kwh') or 0.0,3) if intelligent.get('confirmed') else None,
                'export_generated_safe_export_without_topup_kwh':round((intelligent_export_topup or {}).get('safe_export_kwh') or 0.0,3) if intelligent.get('confirmed') else None,
                'export_generated_export_shortfall_kwh':round((intelligent_export_topup or {}).get('shortfall_kwh') or 0.0,3) if intelligent.get('confirmed') else None,
                'offpeak_arrival_safety_soc':int(round(max(float(reserve),float(self.c.get('safety_buffer_soc',20))))) if intelligent.get('confirmed') else None,
                'strategy':'confirmed_overlay_preserve_normal_plan',
            }
        }
        if power_down is not None:
            result['power_down']={
                'event_entity':power_down.get('entity_id'),
                'event_id':power_down.get('event_id'),
                'start':iso(power_down.get('start')),
                'end':iso(power_down.get('end')),
                'active':bool(power_down.get('active')),
                'reward_octopoints_per_kwh':power_down.get('reward_octopoints_per_kwh'),
                'import_baseline_total_kwh':round(power_down['import_baseline_total_kwh'],3) if power_down.get('import_baseline_total_kwh') is not None else None,
                'export_baseline_total_kwh':round(power_down['export_baseline_total_kwh'],3) if power_down.get('export_baseline_total_kwh') is not None else None,
                'net_baseline_total_kwh':round(power_down['net_baseline_total_kwh'],3) if power_down.get('net_baseline_total_kwh') is not None else None,
                'import_baseline_incomplete':power_down.get('import_baseline_incomplete'),
                'export_baseline_incomplete':power_down.get('export_baseline_incomplete'),
                'forecast_session_start_soc':int(round(power_down['forecast_session_start_soc'])) if power_down.get('forecast_session_start_soc') is not None else None,
                'protected_soc':int(math.ceil(power_down['protected_soc'])) if power_down.get('protected_soc') is not None else None,
                'discharge_control':'duration_only',
                'post_session_required_kwh':round(power_down['post_session_required_kwh'],3) if power_down.get('post_session_required_kwh') is not None else None,
                'post_session_peak_import_avoidable':bool(power_down.get('post_session_peak_import_avoidable')),
                'max_safe_battery_output_kwh':round(power_down.get('max_safe_battery_output_kwh') or 0.0,3),
                'strategy':'max_safe_export',
                'baseline_caps_export':False,
            }
        if export_info is not None:
            result['export_generated']={
                'export_meter_source':export_info.get('export_meter_source'),
                'export_meter_entity':export_info.get('export_meter_entity'),
                'solar_target_kwh':round(export_info['solar_target_kwh'],2),
                'exported_so_far_kwh':round(export_info['exported_so_far_kwh'],2),
                'raw_house_export_so_far_kwh':round(export_info.get('raw_house_export_so_far_kwh') or 0.0,2),
                'excluded_while_car_charging_kwh':round(export_info.get('excluded_while_car_charging_kwh') or 0.0,2),
                'accounting_started_at':export_info.get('accounting_started_at'),
                'uncreditable_confirmed_car_slot_forecast_export_kwh':round(export_info.get('uncreditable_confirmed_car_slot_forecast_export_kwh') or 0.0,3),
                'forecast_natural_export_kwh':round(export_info['forecast_natural_export_kwh'],2),
                'remaining_target_kwh':round(export_info['remaining_kwh'],2),
                'phase':'overnight_no_export' if active else 'pre_offpeak',
                'planned_forced_export_kwh':round(planned_export,2),
                'discharge_to_reserve':discharge_kind=='to_reserve',
                'solve_from':iso(export_solve_start) if export_solve_start else None,
                'forecast_incremental_export_kwh':round(export_solve_forecast_kwh,2),
                'target_reachable_in_window':bool(export_solve_reachable),
                'solar_pause_start':iso(solar_pause_window['start']) if solar_pause_window else None,
                'solar_pause_end':iso(solar_pause_window['end']) if solar_pause_window else None,
                'solar_pause_threshold_w':int(round(float(self.c.get('export_generated_solar_threshold_w',100)))),
                'solar_pause_source':'forecast_no_slots',
            }
        return result
    def material(self,f,o,n):
        if o is None:return True
        if f.endswith('.start') or f.endswith('.end'):
            a,b=parse_dt(o),parse_dt(n); return abs((b-a).total_seconds())>=int(self.c.get('time_deadband_minutes',3))*60 if a and b else o!=n
        if f.endswith('.rate_w'):return abs((as_float(n) or 0)-(as_float(o) or 0))>=int(self.c.get('rate_deadband_w',100))
        if f.endswith('.planned_kwh'):return abs((as_float(n) or 0)-(as_float(o) or 0))>=float(self.c.get('energy_deadband_kwh',.1))
        return o!=n
    def calc_changes(self,p):
        old=self.db.get('status_plan') if self.db.ok else {}; out=[]
        for g in ('offpeak','pause','charge','discharge','forecast','operation','calibration','intelligent_go','power_down','export_generated'):
            if g not in p:continue
            for k,n in p[g].items():
                o=(old or {}).get(g,{}).get(k)
                if self.material(g+'.'+k,o,n):out.append({'field':g+'.'+k,'old':o,'new':n,'reason':'Off-peak window changed' if g=='offpeak' else 'Forecast changed'})
        if self.db.ok:
            for c in out:self.db.conn.execute('INSERT INTO plan_changes(ts,field,old_value,new_value,reason_code) VALUES(?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),c['field'],json.dumps(c['old']),json.dumps(c['new']),'forecast_updated'))
            self.db.conn.commit()
        return out
    async def apply(self,p):
        apply_started=asyncio.get_running_loop().time()
        self.confirmed_writes_this_apply=0

        # Final safety net: a no-discharge plan must never reach the inverter as
        # a wrapped/non-zero slot. If planning ever regresses, repair it here and
        # make the correction explicit in the logs.
        discharge=p.get('discharge') or {}
        if discharge.get('kind')=='none':
            ds=parse_dt(discharge.get('start'))
            de=parse_dt(discharge.get('end'))
            if ds is None or de is None or ds!=de:
                offpeak_for_disable={
                    'start':parse_dt(p['offpeak']['start']).astimezone(self.tz),
                    'end':parse_dt(p['offpeak']['end']).astimezone(self.tz),
                    'rate_p':p['offpeak']['rate_p']
                }
                disabled_at=self.disabled_discharge_time(offpeak_for_disable)
                LOG.warning('Correcting invalid disabled discharge slot before apply: start=%s end=%s -> %s-%s',
                            discharge.get('start'),discharge.get('end'),
                            disabled_at.strftime('%H:%M'),disabled_at.strftime('%H:%M'))
                p['discharge']['start']=iso(disabled_at)
                p['discharge']['end']=iso(disabled_at)
                p['discharge']['planned_kwh']=0.0

        wend=parse_dt(p['offpeak']['end']); w={'start':parse_dt(p['offpeak']['start']).astimezone(self.tz),'end':wend.astimezone(self.tz),'rate_p':p['offpeak']['rate_p']}; pause=p.get('pause') or self.pause_plan(w)
        if p.get('intelligent_go',{}).get('confirmed'):
            ig=p['intelligent_go']
            ia=parse_dt(ig.get('slot_start')); ib=parse_dt(ig.get('slot_end'))
            mode=ig.get('pause_mode') or 'Disabled'
            if mode=='PauseDischarge' and ia and ib:
                pause={'mode':'PauseDischarge','start':self.tstr(ia),'end':self.tstr(ib)}
            elif mode=='PauseCharge':
                # Preserve the normal forecast-defined Export Generated solar
                # window rather than shrinking PauseCharge to this half-hour.
                pause=p.get('pause') or pause
            else:
                pause={'mode':'Disabled','start':'00:00:00','end':'00:00:00'}
        pause_start=await self.preserve_active_slot_start('pause',self.c['pause_start_entity'],self.c['pause_end_entity'],pause['start'])
        charge_start=await self.preserve_active_slot_start('charge',self.c['charge_slot_1_start_entity'],self.c['charge_slot_1_end_entity'],self.tstr(parse_dt(p['charge']['start'])))
        discharge_start=await self.preserve_active_slot_start('discharge',self.c['discharge_slot_1_start_entity'],self.c['discharge_slot_1_end_entity'],self.tstr(parse_dt(p['discharge']['start'])))
        vals=[('eco',self.c['eco_mode_entity'],'on',None),('charge_enable',self.c['charge_schedule_enable_entity'],'on',None),('discharge_enable',self.c['discharge_schedule_enable_entity'],'on',None),('pause_mode',self.c['pause_mode_entity'],pause['mode'],wend),('pause_start',self.c['pause_start_entity'],pause_start,wend),('pause_end',self.c['pause_end_entity'],pause['end'],wend),('charge_start',self.c['charge_slot_1_start_entity'],charge_start,wend),('charge_end',self.c['charge_slot_1_end_entity'],self.tstr(parse_dt(p['charge']['end'])),wend),('charge_target',self.c['charge_slot_1_target_entity'],p['charge']['target_soc'],wend),('charge_rate',self.c['charge_rate_entity'],p['charge']['rate_w'],wend),('discharge_start',self.c['discharge_slot_1_start_entity'],discharge_start,parse_dt(p['offpeak']['start'])),('discharge_end',self.c['discharge_slot_1_end_entity'],self.tstr(parse_dt(p['discharge']['end'])),parse_dt(p['offpeak']['start'])),('discharge_rate',self.c['discharge_rate_entity'],p['discharge']['rate_w'],parse_dt(p['offpeak']['start']))]
        # The discharge target is invariant: always the configured inverter
        # reserve. All export/discharge quantity control is done by slot timing.
        reserve,_=await self.num('battery_reserve_entity','battery_reserve',False)
        if reserve is not None:
            vals.append(('discharge_target',self.c['discharge_slot_1_target_entity'],int(round(reserve)),parse_dt(p['offpeak']['start'])))
        results=[]
        for item in vals:
            field_started=asyncio.get_running_loop().time()
            result=await self.ensure(*item)
            results.append(result)
            elapsed=asyncio.get_running_loop().time()-field_started
            if elapsed>=1.0:
                LOG.info('Apply timing: field=%s elapsed=%.3fs result=%s',item[0],elapsed,'ok' if result else 'failed')
        LOG.info('Apply timing: total=%.3fs fields=%d writes_confirmed=%d success=%s',
                 asyncio.get_running_loop().time()-apply_started,len(vals),self.confirmed_writes_this_apply,
                 'yes' if all(results) else 'no')
        return all(results)
    async def process(self,s):
        async with self.lock:
            loop=asyncio.get_running_loop(); process_started=loop.time()
            self.errors=[e for e in self.errors if e['type']=='database']; valid,why,soc=self.valid_forecast(s); seq=self.forecast_sequence(s); live=self.offpeak_from_forecast(s) if valid else None
            LOG.info('Forecast source: sequence=%d trigger=%s generated_at=%s waiting_refresh=%s',
                     seq,self.forecast_trigger(s) or 'unknown',s.get('attributes',{}).get('forecast_generated_at'),
                     'yes' if self.waiting_for_controller_refresh else 'no')
            if valid:
                self.last_source_forecast_sequence=seq
                if self.db.ok:self.db.set('last_source_forecast_sequence',seq)
            if live:self.persist_offpeak(live)
            w=live or self.fallback_offpeak()
            if not valid:
                self.err('planning','forecast',why,85); cap,_=await self.num('battery_capacity_entity','battery_capacity',False); hw,_=await self.num('inverter_max_charge_rate_entity','max_charge_rate',False); ok=await self.safe(w,cap,hw); self.health='degraded' if ok else 'error'; await self.publish(); return
            self.last_attempt=self.now()
            phase=loop.time(); await self.learn_pending()
            LOG.info('Process timing: sequence=%d phase=learn_pending elapsed=%.3fs',seq,loop.time()-phase)
            if not w:
                self.err('tariff','offpeak','No complete live morning tariff window and no persisted fallback',95); ok=await self.safe(None); self.health='degraded' if ok else 'error'; await self.publish(); return
            phase=loop.time(); p=await self.plan(s,soc,w)
            LOG.info('Process timing: sequence=%d phase=plan elapsed=%.3fs',seq,loop.time()-phase)
            if not p:
                cap,_=await self.num('battery_capacity_entity','battery_capacity',False); hw,_=await self.num('inverter_max_charge_rate_entity','max_charge_rate',False); ok=await self.safe(w,cap,hw); self.health='degraded' if ok else 'error'; await self.publish(); return
            phase=loop.time(); self.changes=self.calc_changes(p)
            LOG.info('Process timing: sequence=%d phase=calc_changes elapsed=%.3fs changes=%d',seq,loop.time()-phase,len(self.changes))
            phase=loop.time(); ok=await self.apply(p)
            LOG.info('Process timing: sequence=%d phase=apply elapsed=%.3fs success=%s',seq,loop.time()-phase,'yes' if ok else 'no')
            self.health='ok' if ok and not self.errors else 'degraded'
            if ok:
                self.last_success=self.now(); self.confirmed={'charge_start':p['charge']['start'],'charge_end':p['charge']['end'],'charge_rate_w':p['charge']['rate_w'],'charge_target_soc':p['charge']['target_soc'],'discharge_start':p['discharge']['start'],'discharge_end':p['discharge']['end'],'discharge_rate_w':p['discharge']['rate_w'],'discharge_target_soc':p['discharge'].get('target_soc')}; self.db.set('confirmed_plan',self.confirmed); self.db.set('status_plan',p)
            if self.confirmed_writes_this_apply>0:
                phase=loop.time(); await self.request_forecast_refresh(seq)
                LOG.info('Process timing: sequence=%d phase=request_refresh elapsed=%.3fs',seq,loop.time()-phase)
            if p['operation']['mode']=='export_generated':
                eg=p.get('export_generated') or {}
                LOG.info('Export generated: source=%s entity=%s target=%.2fkWh credited_actual=%.2fkWh raw_export_sensor=%.2fkWh excluded_car=%.2fkWh natural_future=%.2fkWh remaining=%.2fkWh solve_from=%s forecast_incremental=%.2fkWh reachable=%s',
                         eg.get('export_meter_source') or 'battery',
                         eg.get('export_meter_entity') or 'unknown',
                         eg.get('solar_target_kwh') or 0.0,
                         eg.get('exported_so_far_kwh') or 0.0,
                         eg.get('raw_house_export_so_far_kwh') or 0.0,
                         eg.get('excluded_while_car_charging_kwh') or 0.0,
                         eg.get('forecast_natural_export_kwh') or 0.0,
                         eg.get('remaining_target_kwh') or 0.0,
                         eg.get('solve_from'),
                         eg.get('forecast_incremental_export_kwh') or 0.0,
                         eg.get('target_reachable_in_window'))
            LOG.info('Plan: mode=%s calibration=%s offpeak=%s-%s charge=%s-%s @ %dW target=%d%% est_charge_start_soc=%s%% discharge=%s-%s @ %dW kind=%s no_slots_offpeak_soc=%d%% forced_soc_adjust=%.1f%% reachable=%s plan_changed=%s inverter_writes=%d',p['operation']['mode'],p['calibration']['state'],parse_dt(p['offpeak']['start']).strftime('%H:%M'),parse_dt(p['offpeak']['end']).strftime('%H:%M'),parse_dt(p['charge']['start']).strftime('%H:%M'),parse_dt(p['charge']['end']).strftime('%H:%M'),p['charge']['rate_w'],p['charge']['target_soc'],str(p['forecast'].get('estimated_charge_start_soc')),parse_dt(p['discharge']['start']).strftime('%H:%M'),parse_dt(p['discharge']['end']).strftime('%H:%M'),p['discharge']['rate_w'],p['discharge'].get('kind','timed'),p['forecast']['overnight_start_soc_no_slots'],float(p['forecast'].get('planned_discharge_soc_adjustment_pct') or 0),str(p['forecast'].get('charge_target_reachable')),'yes' if self.changes else 'no',self.confirmed_writes_this_apply)
            if p.get('intelligent_go',{}).get('confirmed'):
                ig=p['intelligent_go']
                LOG.info('Intelligent Go: confirmed slot=%s-%s sensor=%s state=%s export_suspended=yes pause=%s charge_reason=%s charge_target=%s export_topup_needed=%.2fkWh export_shortfall=%.2fkWh',
                         parse_dt(ig['slot_start']).strftime('%H:%M'),
                         parse_dt(ig['slot_end']).strftime('%H:%M'),
                         ig.get('confirmation_entity'),ig.get('sensor_state'),
                         ig.get('pause_mode'),ig.get('charge_reason'),
                         ig.get('intelligent_charge_target_soc'),
                         float(ig.get('export_generated_topup_needed_stored_kwh') or 0.0),
                         float(ig.get('export_generated_export_shortfall_kwh') or 0.0))
            if p.get('power_down'):
                pd=p['power_down']
                LOG.info('Power Down: %s-%s active=%s reward=%s octopoints/kWh import_baseline=%s export_baseline=%s net_baseline=%s protected_soc=%s%% safe_output=%s kWh peak_import_avoidable=%s',
                         parse_dt(pd['start']).strftime('%H:%M'),parse_dt(pd['end']).strftime('%H:%M'),
                         pd.get('active'),pd.get('reward_octopoints_per_kwh'),
                         pd.get('import_baseline_total_kwh'),pd.get('export_baseline_total_kwh'),
                         pd.get('net_baseline_total_kwh'),pd.get('protected_soc'),
                         pd.get('max_safe_battery_output_kwh'),
                         pd.get('post_session_peak_import_avoidable'))
            phase=loop.time(); await self.publish(p)
            LOG.info('Process timing: sequence=%d phase=publish elapsed=%.3fs',seq,loop.time()-phase)
            self.db.prune(self.now())
            LOG.info('Process timing: sequence=%d TOTAL=%.3fs',seq,loop.time()-process_started)
    async def queue(self,s,force=False):
        if not self.discover_from_forecast(s):
            self.health='error'; await self.publish(); return
        seq=self.forecast_sequence(s)
        if not force and seq and seq<=self.last_source_forecast_sequence:
            LOG.debug('Ignoring duplicate/old forecast sequence=%d last=%d',seq,self.last_source_forecast_sequence)
            await self.publish()
            return
        if self.lock.locked():
            self.queued=(s,force)
            return
        while True:
            await self.process(s)
            q=self.queued; self.queued=None
            if not q:break
            if isinstance(q,tuple):s,qforce=q
            else:s,qforce=q,False
            if not self.discover_from_forecast(s):self.health='error'; await self.publish(); break
            qseq=self.forecast_sequence(s)
            if not qforce and qseq and qseq<=self.last_source_forecast_sequence:
                LOG.debug('Discarding duplicate queued forecast sequence=%d',qseq); continue
            if not self.valid_forecast(s)[0]:LOG.info('Discarding stale queued forecast'); break
            force=qforce
    async def resume_pending(self):
        if not self.db.ok:return
        rows=self.db.conn.execute('SELECT * FROM pending_writes ORDER BY updated_at').fetchall(); n=self.now()
        for r in rows:
            we=parse_dt(r['window_end'])
            if we and we.astimezone(self.tz)<n:self.db.conn.execute('DELETE FROM pending_writes WHERE field=?',(r['field'],)); continue
            d=json.loads(r['desired']); o,rd=await self.readable(r['entity_id'])
            if rd and self.match(r['field'],o,d):self.db.conn.execute('DELETE FROM pending_writes WHERE field=?',(r['field'],)); continue
            if rd: await self.ensure(r['field'],r['entity_id'],d,we)
        self.db.conn.commit()
    async def sample_loop(self):
        while True:
            try: await self.sample()
            except asyncio.CancelledError:raise
            except Exception as e:LOG.debug('sample: %s',e)
            await asyncio.sleep(int(self.c.get('sample_interval_seconds',30)))
    async def sample(self):
        if not self.confirmed or not self.discovery_ready:return
        n=self.now()
        await self.update_effective_export_accounting()
        ss=await self.ha.state(self.c['battery_soc_entity'])
        es=await self.ha.state(self.c['grid_export_energy_total_entity'])
        soc=as_float(ss.get('state')) if ss else None
        export_total=as_float(es.get('state')) if es else None
        if soc is None:return

        if self.db.ok and self.c.get('calibration_enabled',True):
            now_iso=iso(n)
            floor=float(self.c.get('deep_cycle_floor_soc',4))
            if soc>=100:
                self.db.set('last_full_soc_at',now_iso)
                if self.db.get('calibration_low_reached_at'):
                    self.db.set('last_deep_calibration_at',now_iso)
                    self.db.set('calibration_low_reached_at',None)
            elif self.calibration_state()=='awaiting_deep_low' and soc<=floor:
                self.db.set('calibration_low_reached_at',now_iso)

        cs=parse_dt(self.confirmed.get('charge_start')); ce=parse_dt(self.confirmed.get('charge_end'))
        ds=parse_dt(self.confirmed.get('discharge_start')); de=parse_dt(self.confirmed.get('discharge_end'))
        day=n.date().isoformat()
        dr=self.daily.setdefault(day,{'actual_charge_kwh':0.0,'actual_export_kwh':0.0,'reached_100':False,'end_offpeak_soc':None,'actual_export_end':None})

        in_charge=bool(cs and ce and cs.astimezone(self.tz)<=n<=ce.astimezone(self.tz))
        in_discharge=bool(ds and de and ds.astimezone(self.tz)<=n<=de.astimezone(self.tz))

        if in_charge:
            if self.active is None:
                self.active={'started':n,'end':ce.astimezone(self.tz),'start_soc':soc,'rate':self.confirmed.get('charge_rate_w',0),'reached':False,'first100':None}
                self.samples=[]
            self.samples.append((n,soc))
            self.active['reached']|=soc>=100
            if soc>=100 and self.active['first100'] is None:self.active['first100']=n
        elif self.active and n>self.active['end']:
            await self.finish_session(n,soc)

        cap=as_float(self.db.get('battery_capacity')) if self.db.ok else None
        if self.last_sample:
            prev_n,prev_soc,prev_export=self.last_sample
            if in_charge and cap is not None:
                dsoc=max(0.0,soc-prev_soc)
                dr['actual_charge_kwh']+=cap*(dsoc/100.0)
                dr['reached_100']|=soc>=100
            if in_discharge and export_total is not None and prev_export is not None:
                dexp=export_total-prev_export
                # Cumulative sensors can reset/restart; ignore negative or implausibly large jumps.
                if 0<=dexp<=10:
                    dr['actual_export_kwh']+=dexp
                    if dexp>0.0001:dr['actual_export_end']=iso(n)
            if ce and abs((n-ce.astimezone(self.tz)).total_seconds())<int(self.c.get('sample_interval_seconds',30))*2:
                dr['end_offpeak_soc']=int(round(soc))
        self.last_sample=(n,soc,export_total)
        await self.save_daily(day)

    async def finish_session(self,n,end_soc):
        a=self.active; sm=self.samples; self.active=None; self.samples=[]
        if not self.db.ok or not a:return
        eligible=len(sm)>=3 and float(a.get('rate') or 0)>0
        reason=None if eligible else 'insufficient_soc_samples'
        dwell=max(0.0,(a['end']-a['first100']).total_seconds()/60) if a.get('first100') else None
        cap=as_float(self.db.get('battery_capacity')) or 0
        charged=max(0.0,cap*(end_soc-a['start_soc'])/100.0) if cap else 0.0
        cur=self.db.conn.execute(
            'INSERT INTO charge_sessions(started_at,ended_at,start_soc,end_soc,requested_rate_w,charged_kwh,reached_100,dwell_minutes,eligible,rejection_reason,processed) VALUES(?,?,?,?,?,?,?,?,?,?,0)',
            (iso(a['started']),iso(n),a['start_soc'],end_soc,a['rate'],charged,1 if a['reached'] else 0,dwell,1 if eligible else 0,reason)
        )
        sid=cur.lastrowid
        if eligible:self.derive_obs(sid,a,sm)
        self.db.conn.commit()

    def derive_obs(self,sid,a,sm):
        # Learning deliberately uses only commanded charge rate, SOC change and
        # elapsed time. Instantaneous battery power is not required.
        cap=as_float(self.db.get('battery_capacity')) or 0
        req=max(1,float(a['rate'] or 0))
        for lo,hi in SOC_BANDS:
            x=[z for z in sm if lo<=z[1]<=hi]
            if len(x)<2:continue
            t0,s0=x[0]; t1,s1=x[-1]
            ds=max(0.0,s1-s0)
            cov=clamp(ds/(hi-lo),0,1)
            dt=max(1e-6,(t1-t0).total_seconds()/3600)
            if cov>0 and cap>0:
                effective_kw=cap*(ds/100.0)/dt
                self.db.conn.execute(
                    'INSERT INTO band_observations(session_id,observed_at,band_lo,band_hi,coverage,effective_factor) VALUES(?,?,?,?,?,?)',
                    (sid,iso(t1),lo,hi,cov,clamp(effective_kw/(req/1000.0),.05,1.5))
                )

    async def learn_pending(self):
        if not self.db.ok:return
        for s in self.db.conn.execute('SELECT * FROM charge_sessions WHERE eligible=1 AND processed=0 ORDER BY ended_at').fetchall():
            try:
                self.db.conn.execute('BEGIN'); now=self.now(); obs=self.db.conn.execute('SELECT * FROM band_observations WHERE session_id=?',(s['id'],)).fetchall()
                for o in obs:
                    band=(o['band_lo'],o['band_hi']); allobs=self.db.conn.execute('SELECT observed_at,coverage,effective_factor FROM band_observations WHERE band_lo=? AND band_hi=? ORDER BY observed_at',band).fetchall(); vals=[]; wts=[]; cover=0
                    for z in allobs:
                        ts=parse_dt(z['observed_at']).astimezone(self.tz)
                        if ts<now-timedelta(days=365):continue
                        vals.append(z['effective_factor']); wts.append(recency_weight(ts,now)*z['coverage']); cover+=z['coverage']
                    if not vals:continue
                    p10=p90=None; clip=vals[:]
                    if len(vals)>=5:p10=weighted_quantile(vals,wts,.1); p90=weighted_quantile(vals,wts,.9); clip=[clamp(v,p10,p90) for v in vals]
                    learned=sum(v*w for v,w in zip(clip,wts))/sum(wts); conf=min(1,cover/10)
                    old=self.db.conn.execute('SELECT confidence FROM learned_bands WHERE band_lo=? AND band_hi=?',band).fetchone(); conf=max(conf,old['confidence'] if old else 0)
                    self.db.conn.execute('INSERT INTO learned_bands(band_lo,band_hi,learned_factor,confidence,full_equiv,obs_count,p10,p90,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(band_lo,band_hi) DO UPDATE SET learned_factor=excluded.learned_factor,confidence=excluded.confidence,full_equiv=excluded.full_equiv,obs_count=excluded.obs_count,p10=excluded.p10,p90=excluded.p90,updated_at=excluded.updated_at',(band[0],band[1],learned,conf,cover,len(vals),p10,p90,datetime.now(timezone.utc).isoformat()))
                self.db.conn.execute('UPDATE charge_sessions SET processed=1 WHERE id=?',(s['id'],)); self.db.conn.commit()
            except Exception:self.db.conn.rollback(); LOG.exception('Learning session failed'); break
    async def save_daily(self,day):
        if not self.db.ok:return
        dr=self.daily[day]; p=self.db.get('status_plan') or {}; pc=as_float(p.get('charge',{}).get('planned_kwh')) or 0; pe=as_float(p.get('discharge',{}).get('planned_kwh')) or 0; ac=dr['actual_charge_kwh']; ae=dr['actual_export_kwh']; cv=ac-pc; ev=ae-pe; cr=('Charge higher than planned' if cv>0 else 'Charge lower than planned') if abs(cv)>=float(self.c.get('charge_variance_threshold_kwh',.5)) else None; er=('Export higher than planned' if ev>0 else 'Export lower than planned') if abs(ev)>=float(self.c.get('export_variance_threshold_kwh',.2)) else None
        self.db.conn.execute('INSERT INTO daily_summary(day,planned_charge_kwh,actual_charge_kwh,charge_variance_kwh,charge_variance_reason,reached_100,end_offpeak_soc,planned_export_kwh,actual_export_kwh,export_variance_kwh,export_variance_reason,actual_export_end,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET planned_charge_kwh=excluded.planned_charge_kwh,actual_charge_kwh=excluded.actual_charge_kwh,charge_variance_kwh=excluded.charge_variance_kwh,charge_variance_reason=excluded.charge_variance_reason,reached_100=excluded.reached_100,end_offpeak_soc=excluded.end_offpeak_soc,planned_export_kwh=excluded.planned_export_kwh,actual_export_kwh=excluded.actual_export_kwh,export_variance_kwh=excluded.export_variance_kwh,export_variance_reason=excluded.export_variance_reason,actual_export_end=excluded.actual_export_end,updated_at=excluded.updated_at',(day,pc,ac,cv,cr,1 if dr['reached_100'] else 0,dr['end_offpeak_soc'],pe,ae,ev,er,dr['actual_export_end'],datetime.now(timezone.utc).isoformat())); self.db.conn.commit()
    async def startup(self):
        forecast_entity=str(self.c.get('home_energy_forecast_entity','sensor.home_energy_forecast')).strip()
        if not forecast_entity:
            forecast_entity='sensor.home_energy_forecast'; self.c['home_energy_forecast_entity']=forecast_entity

        LOG.info('Home Energy Controller v%s starting',VERSION)
        LOG.info('Startup: database=%s forecast=%s','ok' if self.db.ok else 'unavailable',forecast_entity)

        if not self.db.ok:
            self.err('database','sqlite','Database integrity/open check failed; learning/history disabled',75)
        elif self.c.get('calibration_enabled',True):
            now_iso=iso(self.now())
            if self.db.get('last_full_soc_at') is None:self.db.set('last_full_soc_at',now_iso)
            if self.db.get('last_deep_calibration_at') is None:self.db.set('last_deep_calibration_at',now_iso)

        LOG.info('Startup: reading current forecast')
        current=await self.ha.state(forecast_entity)
        if not current:
            self.health='degraded'
            self.err('input','forecast','Current forecast entity is unavailable; waiting for forecast update',90)
            off=self.fallback_offpeak()
            LOG.warning('Startup: current forecast unavailable; offpeak=%s',f"{iso(off['start'])}->{iso(off['end'])}" if off else 'none')
            await self.publish()
            return

        if not self.discover_from_forecast(current):
            self.health='degraded'
            self.err('input','controller_inputs','Waiting for a Home Energy Forecaster v0.2.8+ forecast containing controller_inputs',90)
            LOG.warning('Startup discovery: forecast=%s controller_inputs=not-ready; waiting for forecast update',forecast_entity)
            await self.publish()
            return

        LOG.info('Startup discovery: forecast=%s controller_inputs=ready sequence=%d trigger=%s',
                 forecast_entity,self.forecast_sequence(current),self.forecast_trigger(current) or 'unknown')

        # A fresh plan supersedes any persisted retry intents from an older plan.
        # Do not block startup replaying them before the current forecast is considered.
        if self.db.ok:
            pending=self.db.conn.execute('SELECT COUNT(*) FROM pending_writes').fetchone()[0]
            if pending:
                self.db.conn.execute('DELETE FROM pending_writes')
                self.db.conn.commit()
                LOG.info('Startup: discarded %d stale pending write(s); current forecast will supersede them',pending)

        valid,why,_=self.valid_forecast(current)
        age_info=current.get('attributes',{}).get('forecast_generated_at')
        trigger=self.forecast_trigger(current)
        if self.controller_triggered_forecast(current):
            # The immediate controller refresh exists to make the published
            # forecast reflect inverter writes. It must never feed straight back
            # into control. Wait for the next scheduled forecast instead.
            LOG.info('Startup: ignoring controller-triggered forecast sequence=%d trigger=%s generated_at=%s; waiting for next scheduled forecast',
                     self.forecast_sequence(current),trigger,age_info)
        else:
            if valid:
                LOG.info('Startup: processing current forecast immediately sequence=%d trigger=%s generated_at=%s',
                         self.forecast_sequence(current),trigger or 'unknown',age_info)
            else:
                LOG.warning('Startup: current forecast is not valid (%s); processing fallback safety path',why)

            # Process even if this sequence was seen before a restart. The current
            # non-controller forecast is authoritative and process() converges
            # against actual inverter readback.
            await self.process(current)

        off=self.fallback_offpeak()
        LOG.info('Startup complete: db=%s confirmed_plan=%s offpeak=%s',
                 'ok' if self.db.ok else 'unavailable',
                 'yes' if self.confirmed else 'no',
                 f"{iso(off['start'])}->{iso(off['end'])}" if off else 'none')
    async def shutdown(self):
        LOG.info('Controller shutdown starting')
        if self.db.ok:self.db.conn.commit()
        LOG.info('Shutdown: confirmed_plan=%s','yes' if self.confirmed else 'no')
        LOG.info('Controller shutdown complete')

async def resolve_forecast_entity(ha,cfg):
    configured=str(cfg.get('home_energy_forecast_entity','sensor.home_energy_forecast') or 'sensor.home_energy_forecast').strip()
    current=await ha.state(configured)
    if current and isinstance(current.get('attributes',{}).get('controller_inputs'),dict):
        return configured

    candidates=[]
    for state in await ha.states():
        if not isinstance(state,dict):
            continue
        entity_id=str(state.get('entity_id') or '')
        attrs=state.get('attributes',{}) if isinstance(state.get('attributes'),dict) else {}
        controller_inputs=attrs.get('controller_inputs')
        if not entity_id.startswith('sensor.') or not isinstance(controller_inputs,dict):
            continue
        if str(attrs.get('friendly_name') or '') == 'Home Energy Forecast' or attrs.get('forecast_sequence') is not None:
            candidates.append(entity_id)

    if candidates:
        # Prefer the canonical id, otherwise a deterministic MQTT-discovered match.
        resolved='sensor.home_energy_forecast' if 'sensor.home_energy_forecast' in candidates else sorted(candidates)[0]
        LOG.warning('Configured forecast entity %s unavailable; auto-discovered %s',configured,resolved)
        cfg['home_energy_forecast_entity']=resolved
        return resolved
    return configured

async def main():
    cfg=json.loads(OPTIONS_PATH.read_text())
    shutdown_event=asyncio.Event()
    LOG.info('Home Energy Controller v%s process starting',VERSION)
    db=DB(DB_PATH)
    try:db.open()
    except Exception:LOG.exception('DB migration/open failed; preserving DB')

    ha=HA()
    await ha.open()
    c=Controller(cfg,ha,db)

    loop=asyncio.get_running_loop()
    def _request_shutdown(sig_name):
        LOG.info('Shutdown requested by Supervisor (%s)',sig_name)
        shutdown_event.set()
    for _sig in (signal.SIGTERM,signal.SIGINT):
        try:
            loop.add_signal_handler(_sig,_request_shutdown,_sig.name)
        except (NotImplementedError,RuntimeError):
            pass

    forecast_entity=await resolve_forecast_entity(ha,cfg)
    intelligent_entity=str(cfg.get('intelligent_car_charging_entity','binary_sensor.octopus_slot_actually_charging')).strip()
    intelligent_dispatch_entity=str(cfg.get('intelligent_dispatch_entity','')).strip()
    # Forecast events are wake-up signals, not forecast snapshots. Keep at most
    # one pending wake-up and always read the CURRENT HA forecast when handling
    # it. This prevents a FIFO backlog of obsolete forecast objects.
    #
    # A force wake-up (Intelligent confirmation/planned-dispatch change) is
    # sticky when coalescing so it cannot be lost behind a scheduled forecast.
    event_queue=asyncio.Queue(maxsize=1)
    subscription_ready=asyncio.Event()

    async def enqueue_latest(force=False):
        combined_force=bool(force)
        if event_queue.full():
            try:
                _old_force=event_queue.get_nowait()
                combined_force=combined_force or bool(_old_force)
            except asyncio.QueueEmpty:
                pass
        try:
            event_queue.put_nowait(combined_force)
        except asyncio.QueueFull:
            # Another producer won the race; replace it and preserve force.
            try:
                _old_force=event_queue.get_nowait()
                combined_force=combined_force or bool(_old_force)
            except asyncio.QueueEmpty:
                pass
            event_queue.put_nowait(combined_force)

    async def enqueue_current(force=False):
        # Kept as a semantic helper for non-forecast events. The actual forecast
        # is deliberately fetched later by the main consumer.
        await enqueue_latest(force)

    async def event_pump():
        async for e in ha.events(subscription_ready):
            entity=e.get('entity_id')
            if entity==forecast_entity and e.get('new_state'):
                new_state=e['new_state']
                trigger=c.forecast_trigger(new_state)
                if c.controller_triggered_forecast(new_state):
                    # This is the post-write display forecast. Its publication
                    # closes the transaction: scheduled control can resume, but
                    # this forecast itself never feeds back into control.
                    was_waiting=c.waiting_for_controller_refresh
                    c.waiting_for_controller_refresh=False
                    c.waiting_for_controller_refresh_since=None
                    LOG.info('Controller refresh published: sequence=%d trigger=%s; waiting_refresh %s->no; forecast ignored for control',
                             c.forecast_sequence(new_state),trigger,'yes' if was_waiting else 'no')
                    continue
                if c.waiting_for_controller_refresh:
                    # Normally ignore scheduled/startup forecasts until the
                    # explicit controller_refresh arrives. As a failsafe, never
                    # let a missed/misclassified refresh block control forever.
                    timeout_s=float(c.c.get('controller_refresh_timeout_seconds',90))
                    waited=(c.now()-c.waiting_for_controller_refresh_since).total_seconds() if c.waiting_for_controller_refresh_since else 0.0
                    if waited < timeout_s:
                        LOG.info('Ignoring scheduled forecast while waiting for controller refresh: sequence=%d trigger=%s generated_at=%s waited=%.0fs timeout=%.0fs',
                                 c.forecast_sequence(new_state),trigger or 'unknown',
                                 new_state.get('attributes',{}).get('forecast_generated_at'),waited,timeout_s)
                        continue
                    LOG.warning('Controller refresh wait timed out after %.0fs; accepting fresh forecast sequence=%d trigger=%s and clearing barrier',
                                waited,c.forecast_sequence(new_state),trigger or 'unknown')
                    c.waiting_for_controller_refresh=False
                    c.waiting_for_controller_refresh_since=None
                # Scheduled/startup/legacy forecast: wake the controller. The
                # consumer will fetch the latest current HA state before acting.
                await enqueue_latest(False)
            elif intelligent_entity and entity==intelligent_entity:
                # Confirmation or loss of the live car-charging signal must be
                # evaluated immediately rather than waiting for the next 5-min
                # forecast. Once a slot has been confirmed, OFF does not revoke
                # it before the half-hour boundary.
                await enqueue_current(True)
            elif intelligent_dispatch_entity and entity==intelligent_dispatch_entity:
                # Planned dispatches are advisory, but their appearance/removal
                # can change whether a normal export is allowed to begin.
                await enqueue_current(True)

    async def intelligent_boundary_pump():
        last_slot_key=None
        while True:
            try:
                st=await ha.state(intelligent_entity) if intelligent_entity else None
                sensor_on=bool(st and str(st.get('state','')).lower()=='on')
                a,b=c.half_hour_slot()
                current_key=iso(a) if sensor_on else None
                persisted=c.persisted_intelligent_slot()
                persisted_key=iso(persisted['start']) if persisted else None
                key=current_key or persisted_key
                if key!=last_slot_key:
                    if last_slot_key is not None or key is not None:
                        await enqueue_current(True)
                    last_slot_key=key
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.debug('Intelligent boundary watch: %s',exc)
            await asyncio.sleep(5)

    pump=asyncio.create_task(event_pump())
    intelligent_pump=asyncio.create_task(intelligent_boundary_pump())
    sampler=None
    try:
        # Establish the state-change subscription before reading the current
        # forecast. Events arriving during startup are queued, not lost.
        try:
            await asyncio.wait_for(subscription_ready.wait(),timeout=20)
            LOG.info('Forecast event subscription ready: %s',forecast_entity)
        except asyncio.TimeoutError:
            LOG.warning('Forecast event subscription not confirmed within 20s; continuing with current-state startup read')

        await c.startup()
        sampler=asyncio.create_task(c.sample_loop())

        while not shutdown_event.is_set():
            event_task=asyncio.create_task(event_queue.get())
            stop_task=asyncio.create_task(shutdown_event.wait())
            done,pending=await asyncio.wait(
                (event_task,stop_task),
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

            if stop_task in done and shutdown_event.is_set():
                if not event_task.done():
                    event_task.cancel()
                break

            force=bool(event_task.result())
            s=await ha.state(forecast_entity)
            if not s:
                LOG.warning('Forecast wake-up received but current forecast entity is unavailable')
                continue

            if not force and c.waiting_for_controller_refresh:
                timeout_s=float(c.c.get('controller_refresh_timeout_seconds',90))
                waited=(c.now()-c.waiting_for_controller_refresh_since).total_seconds() if c.waiting_for_controller_refresh_since else 0.0
                if waited < timeout_s:
                    LOG.info('Skipping queued scheduled wake-up while waiting for controller refresh: current sequence=%d trigger=%s waited=%.0fs timeout=%.0fs',
                             c.forecast_sequence(s),c.forecast_trigger(s) or 'unknown',waited,timeout_s)
                    continue
                LOG.warning('Queued refresh barrier timed out after %.0fs; clearing barrier and processing current forecast',waited)
                c.waiting_for_controller_refresh=False
                c.waiting_for_controller_refresh_since=None

            if not force and c.controller_triggered_forecast(s):
                # A scheduled wake-up may have been queued just before the
                # controller-triggered refresh published. Never turn that
                # display refresh into another control pass.
                LOG.info('Skipping superseded scheduled wake-up: current forecast sequence=%d trigger=%s',
                         c.forecast_sequence(s),c.forecast_trigger(s))
                continue

            LOG.debug('Processing latest forecast state sequence=%d trigger=%s force=%s',
                      c.forecast_sequence(s),c.forecast_trigger(s) or 'unknown',force)
            await c.queue(s,force=force)
    finally:
        if sampler:
            sampler.cancel()
            try:await sampler
            except asyncio.CancelledError:pass
        intelligent_pump.cancel()
        try:await intelligent_pump
        except asyncio.CancelledError:pass
        pump.cancel()
        try:await pump
        except asyncio.CancelledError:pass
        for _sig in (signal.SIGTERM,signal.SIGINT):
            try:loop.remove_signal_handler(_sig)
            except Exception:pass
        await c.shutdown()
        await ha.close()
        db.close()

if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
