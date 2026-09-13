import sqlite3
from datetime import datetime, timedelta, timezone

from components.controller.controller_battery import (
    bootstrap_top_completion,
    charge_minutes,
    dwell,
    learn_top_completion,
)


class FakeLog:
    def info(self, *args, **kwargs):
        pass


class FakeDB:
    def __init__(self):
        self.ok = True
        self.values = {}
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE charge_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL, start_soc REAL, end_soc REAL,
            requested_rate_w REAL, charged_kwh REAL, reached_100 INTEGER NOT NULL,
            dwell_minutes REAL, eligible INTEGER NOT NULL, rejection_reason TEXT,
            processed INTEGER NOT NULL DEFAULT 0)''')
        self.conn.execute('''CREATE TABLE learned_bands(
            band_lo REAL NOT NULL, band_hi REAL NOT NULL, learned_factor REAL,
            confidence REAL NOT NULL DEFAULT 0)''')

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeController:
    def __init__(self):
        self.db = FakeDB()
        self.c = {'generic_dwell_minutes': 15}
        self.LOG = FakeLog()
        self._now = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)

    def now(self):
        return self._now


def test_live_99_percent_miss_adds_ten_minutes():
    c = FakeController()
    end = c.now()
    active = {'end': end, 'first100': None}
    learn_top_completion(c, end, 99.0, active, 100)
    assert c.db.get('learned_dwell_minutes') == 25.0
    assert c.db.get('top_completion_misses') == 1


def test_non_full_target_does_not_change_top_learning():
    c = FakeController()
    learn_top_completion(c, c.now(), 80.0, {'end': c.now(), 'first100': None}, 80)
    assert c.db.get('learned_dwell_minutes') is None
    assert c.db.get('top_completion_attempts') is None


def test_success_reduces_elevated_allowance_slowly_and_not_below_generic():
    c = FakeController()
    c.db.set('learned_dwell_minutes', 25.0)
    end = c.now()
    learn_top_completion(c, end, 100.0, {'end': end, 'first100': end-timedelta(minutes=30)}, 100)
    assert c.db.get('learned_dwell_minutes') == 23.0
    c.db.set('learned_dwell_minutes', 15.5)
    learn_top_completion(c, end, 100.0, {'end': end, 'first100': end-timedelta(minutes=30)}, 100)
    assert c.db.get('learned_dwell_minutes') == 15.0
    assert dwell(c) == 15.0


def test_charge_minutes_uses_learned_top_allowance():
    c = FakeController()
    generic = charge_minutes(c, 99, 100, 3000, 13.5)
    c.db.set('learned_dwell_minutes', 35.0)
    learned = charge_minutes(c, 99, 100, 3000, 13.5)
    assert learned - generic == 20.0


def test_bootstrap_replays_historic_success_and_99_percent_miss_once():
    c = FakeController()
    end1 = c.now()-timedelta(days=2)
    end2 = c.now()-timedelta(days=1)
    c.db.conn.execute(
        'INSERT INTO charge_sessions(started_at,ended_at,start_soc,end_soc,requested_rate_w,charged_kwh,reached_100,dwell_minutes,eligible) VALUES(?,?,?,?,?,?,?,?,?)',
        ((end1-timedelta(hours=3)).isoformat(), end1.isoformat(), 20, 100, 3400, 10, 1, 20, 1),
    )
    c.db.conn.execute(
        'INSERT INTO charge_sessions(started_at,ended_at,start_soc,end_soc,requested_rate_w,charged_kwh,reached_100,dwell_minutes,eligible) VALUES(?,?,?,?,?,?,?,?,?)',
        ((end2-timedelta(hours=3)).isoformat(), end2.isoformat(), 20, 99, 3400, 10, 0, None, 1),
    )
    c.db.conn.commit()
    bootstrap_top_completion(c)
    assert c.db.get('top_completion_bootstrap_sessions') == 2
    assert c.db.get('top_completion_attempts') == 2
    assert c.db.get('top_completion_successes') == 1
    assert c.db.get('top_completion_misses') == 1
    assert c.db.get('learned_dwell_minutes') == 25.0
    bootstrap_top_completion(c)
    assert c.db.get('top_completion_attempts') == 2


def test_bootstrap_ignores_historic_partial_charge_without_full_evidence():
    c = FakeController()
    end = c.now()-timedelta(days=1)
    c.db.conn.execute(
        'INSERT INTO charge_sessions(started_at,ended_at,start_soc,end_soc,requested_rate_w,charged_kwh,reached_100,dwell_minutes,eligible) VALUES(?,?,?,?,?,?,?,?,?)',
        ((end-timedelta(hours=2)).isoformat(), end.isoformat(), 20, 80, 3400, 8, 0, None, 1),
    )
    c.db.conn.commit()
    bootstrap_top_completion(c)
    assert c.db.get('top_completion_bootstrap_sessions') == 0
    assert c.db.get('top_completion_attempts') is None
