# Controller-owned persistence for Home Energy Manager.
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
