from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "components" / "controller"
APP = CONTROLLER / "app.py"
DOCKERFILE = ROOT / "Dockerfile"
ARCH_TEST = ROOT / "test_architecture.py"

UTILS = '''"""Pure utility helpers for the Home Energy Manager controller."""
import math
from datetime import datetime


def as_float(v):
    try:
        if v in (None, '', 'unknown', 'unavailable'):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except Exception:
        return None


def clamp(v, a, b):
    return max(a, min(b, v))


def iso(v):
    return v.isoformat() if v else None


def weighted_quantile(vals, wts, q):
    pts = sorted((float(v), float(w)) for v, w in zip(vals, wts) if w > 0)
    if not pts:
        return None
    tgt = sum(w for _, w in pts) * q
    acc = 0
    for v, w in pts:
        acc += w
        if acc >= tgt:
            return v
    return pts[-1][0]


def recency_weight(ts, now, half=45.0):
    age = max(0, (now - ts).total_seconds() / 86400)
    return .5 ** (age / half)
'''

DB = '''"""Controller-owned persistence for Home Energy Manager."""
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

DB_SCHEMA_VERSION = 1


class DB:
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.ok = False

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        if existed:
            row = self.conn.execute('PRAGMA integrity_check').fetchone()
            if not row or row[0] != 'ok':
                self.conn.close()
                self.conn = None
                return
        self.conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        cur = int(row[0]) if row else 0
        if cur and cur < DB_SCHEMA_VERSION:
            self.conn.commit()
            self.conn.close()
            stamp = datetime.now().strftime('%Y%m%d%H%M%S')
            shutil.copy2(self.path, self.path.with_name(self.path.name + '.bak.' + stamp))
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            backups = sorted(self.path.parent.glob(self.path.name + '.bak.*'), reverse=True)
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
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(DB_SCHEMA_VERSION),),
        )
        self.conn.commit()
        self.ok = True

    def get(self, k, d=None):
        if not self.ok:
            return d
        r = self.conn.execute('SELECT value FROM kv WHERE key=?', (k,)).fetchone()
        if not r:
            return d
        try:
            return json.loads(r[0])
        except Exception:
            return r[0]

    def set(self, k, v):
        if not self.ok:
            return
        self.conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (k, json.dumps(v), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def prune(self, now):
        if not self.ok:
            return
        c90 = (now - timedelta(days=90)).isoformat()
        c30 = (now - timedelta(days=30)).isoformat()
        c365 = (now - timedelta(days=365)).isoformat()
        self.conn.execute('DELETE FROM write_audit WHERE ts<?', (c90,))
        self.conn.execute('DELETE FROM plan_changes WHERE ts<?', (c90,))
        self.conn.execute('DELETE FROM daily_summary WHERE updated_at<?', (c90,))
        self.conn.execute('DELETE FROM charge_sessions WHERE eligible=1 AND ended_at<?', (c90,))
        self.conn.execute('DELETE FROM charge_sessions WHERE eligible=0 AND ended_at<?', (c30,))
        self.conn.execute('DELETE FROM band_observations WHERE observed_at<?', (c365,))
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.commit()
            self.conn.close()
'''

HA = '''"""Home Assistant transport used by the controller.

This module contains transport only; it deliberately owns no battery or tariff policy.
"""
import asyncio
import json
import logging
import os

import aiohttp

LOG = logging.getLogger('home_energy_controller')


class HA:
    def __init__(self):
        t = os.environ.get('SUPERVISOR_TOKEN')
        if not t:
            raise RuntimeError('SUPERVISOR_TOKEN missing')
        self.t = t
        self.s = None
        self.base = 'http://supervisor/core/api'
        self.ws = 'ws://supervisor/core/websocket'

    async def open(self):
        self.s = aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + self.t})

    async def close(self):
        await self.s.close()

    async def state(self, e):
        if not e:
            return None
        try:
            async with self.s.get(f'{self.base}/states/{e}', timeout=15) as r:
                return await r.json() if r.status == 200 else None
        except Exception:
            return None

    async def states(self):
        try:
            async with self.s.get(f'{self.base}/states', timeout=20) as r:
                return await r.json() if r.status == 200 else []
        except Exception:
            return []

    async def publish(self, e, state, attrs):
        async with self.s.post(
            f'{self.base}/states/{e}', json={'state': state, 'attributes': attrs}, timeout=15
        ) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f'publish HTTP {r.status}')

    async def write(self, e, v):
        d = e.split('.', 1)[0]
        data = {'entity_id': e}
        if d == 'switch':
            service = 'turn_on' if str(v).lower() in ('on', 'true', '1') else 'turn_off'
        elif d in ('number', 'input_number'):
            service = 'set_value'
            data['value'] = float(v)
        elif d in ('select', 'input_select'):
            service = 'select_option'
            data['option'] = str(v)
        elif d == 'time':
            service = 'set_value'
            data['time'] = str(v)
        elif d == 'input_datetime':
            service = 'set_datetime'
            data['time'] = str(v)
        else:
            raise ValueError('Unsupported writable entity domain ' + d)
        async with self.s.post(f'{self.base}/services/{d}/{service}', json=data, timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f'{d}.{service} HTTP {r.status}')

    async def events(self, ready=None):
        while True:
            try:
                async with self.s.ws_connect(self.ws, heartbeat=30) as ws:
                    m = await ws.receive_json()
                    if m.get('type') == 'auth_required':
                        await ws.send_json({'type': 'auth', 'access_token': self.t})
                        m = await ws.receive_json()
                    if m.get('type') != 'auth_ok':
                        raise RuntimeError('WS auth failed')
                    await ws.send_json({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'})
                    ack = await ws.receive_json()
                    if ack.get('type') != 'result' or not ack.get('success', False):
                        raise RuntimeError('WS event subscription failed')
                    if ready is not None:
                        ready.set()
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            j = json.loads(m.data)
                            if j.get('type') == 'event':
                                yield j['event']['data']
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning('WebSocket reconnect: %s', e)
                await asyncio.sleep(5)
'''


def require_once(text, old, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, '', 1)


app = APP.read_text(encoding='utf-8')
import_anchor = 'from common.mqtt import MQTTPublisher\n'
if import_anchor not in app:
    raise RuntimeError('app import anchor missing')
app = app.replace(
    import_anchor,
    import_anchor
    + 'from controller_db import DB\n'
    + 'from controller_ha import HA\n'
    + 'from controller_utils import as_float, parse_dt, clamp, iso, weighted_quantile, recency_weight\n',
    1,
)
start = app.index('def as_float(v):')
end = app.index('class Controller:')
removed = app[start:end]
for expected in ('class DB:', 'class HA:', 'def weighted_quantile', 'def recency_weight'):
    if expected not in removed:
        raise RuntimeError(f'expected infrastructure block marker missing: {expected}')
app = app[:start] + app[end:]
APP.write_text(app, encoding='utf-8')
(CONTROLLER / 'controller_utils.py').write_text(UTILS, encoding='utf-8')
(CONTROLLER / 'controller_db.py').write_text(DB, encoding='utf-8')
(CONTROLLER / 'controller_ha.py').write_text(HA, encoding='utf-8')


docker = DOCKERFILE.read_text(encoding='utf-8')
docker_anchor = 'COPY components/controller/app.py /app/runtime/controller/app_core.py\n'
if docker_anchor not in docker:
    raise RuntimeError('Docker controller core copy anchor missing')
docker = docker.replace(
    docker_anchor,
    docker_anchor
    + 'COPY components/controller/controller_utils.py /app/runtime/controller/controller_utils.py\n'
    + 'COPY components/controller/controller_db.py /app/runtime/controller/controller_db.py\n'
    + 'COPY components/controller/controller_ha.py /app/runtime/controller/controller_ha.py\n',
    1,
)
DOCKERFILE.write_text(docker, encoding='utf-8')


test = ARCH_TEST.read_text(encoding='utf-8')
expected_anchor = '        "components/controller/app.py": "/app/runtime/controller/app_core.py",\n'
if expected_anchor not in test:
    raise RuntimeError('architecture controller expected anchor missing')
test = test.replace(
    expected_anchor,
    expected_anchor
    + '        "components/controller/controller_utils.py": "/app/runtime/controller/controller_utils.py",\n'
    + '        "components/controller/controller_db.py": "/app/runtime/controller/controller_db.py",\n'
    + '        "components/controller/controller_ha.py": "/app/runtime/controller/controller_ha.py",\n',
    1,
)
append = '''\n\ndef test_controller_infrastructure_is_extracted_from_core():
    controller_dir = ROOT / "components" / "controller"
    core_text = (controller_dir / "app.py").read_text(encoding="utf-8")
    core_tree = ast.parse(core_text, filename=str(controller_dir / "app.py"))
    top_level_classes = {node.name for node in core_tree.body if isinstance(node, ast.ClassDef)}
    top_level_functions = {node.name for node in core_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "DB" not in top_level_classes
    assert "HA" not in top_level_classes
    for name in ("as_float", "parse_dt", "clamp", "iso", "weighted_quantile", "recency_weight"):
        assert name not in top_level_functions
    assert "from controller_db import DB" in core_text
    assert "from controller_ha import HA" in core_text
    assert "from controller_utils import as_float, parse_dt, clamp, iso, weighted_quantile, recency_weight" in core_text
'''
if 'def test_controller_infrastructure_is_extracted_from_core()' not in test:
    test += append
ARCH_TEST.write_text(test, encoding='utf-8')

print('Controller infrastructure extraction applied.')
