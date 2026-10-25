"""Capture actual half-hour CH schedule states and learn separate kWh/DD rates.

The weekly EcoMAX schedule is not historical data: only an observation made
*during* a slot may label that slot. Missing or conflicting observations must
never turn into assumed setback periods.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone, timedelta

LOG = logging.getLogger("ashp_forecast")
DEFAULT_PREFIX = "number.heatpump_heatpump_schedule_"
MIN_LEARNED_DD = 0.25
MIN_LEARNED_SLOTS = 4
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS ch_schedule_observations (
            start_utc TEXT PRIMARY KEY,
            state INTEGER,
            observed_utc TEXT NOT NULL
        )
    """)
    fields = {row[1] for row in db.execute("PRAGMA table_info(slot_training)")}
    if "schedule_state" not in fields:
        db.execute("ALTER TABLE slot_training ADD COLUMN schedule_state INTEGER")
    db.commit()


def ensure_prefix(db: sqlite3.Connection, prefix: str) -> None:
    """Do not reuse classifications collected from another schedule."""
    key = "ch_schedule_observation_prefix"
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if row is not None and row[0] == prefix:
        return
    with db:
        db.execute("DELETE FROM ch_schedule_observations")
        db.execute("UPDATE slot_training SET schedule_state=NULL")
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, prefix),
        )


def read_schedule(client, prefix: str) -> dict[str, int] | None:
    """Fail the complete read if ANY of the 14 entities is unavailable."""
    if not prefix:
        return None
    bits = {}
    for day in WEEKDAYS:
        for half in ("am", "pm"):
            key = f"{day}_{half}"
            try:
                raw = float(client.get_state(f"{prefix}{key}")["state"])
                if not math.isfinite(raw) or raw != int(raw) or not (0 <= raw <= 0xFFFFFFFF):
                    return None
                bits[key] = int(raw)
            except (KeyError, TypeError, ValueError, RuntimeError, OverflowError):
                return None
    return bits


def state_for(bits: dict[str, int] | None, local_dt: datetime) -> int | None:
    if bits is None:
        return None
    # Same EcoMAX half-hour bit order used by the existing DHW forecast.
    import main as legacy
    key = f"{WEEKDAYS[local_dt.weekday()]}_{'am' if local_dt.hour < 12 else 'pm'}"
    return int(legacy.schedule_bit_enabled(bits[key], local_dt))


def capture(client, db: sqlite3.Connection, prefix: str, tz, now: datetime | None = None) -> dict[str, int] | None:
    """Sample the schedule while the corresponding real slot is in progress."""
    ensure_schema(db)
    ensure_prefix(db, prefix)
    now = now or datetime.now(tz)
    bits = read_schedule(client, prefix)
    client.ch_schedule_bits = bits
    if bits is None:
        if prefix:
            LOG.warning("CH schedule unavailable/incomplete; using overall kWh/DD fallback")
        return None
    utc = now.astimezone(timezone.utc)
    start = utc.replace(minute=(utc.minute // 30) * 30, second=0, microsecond=0)
    state = state_for(bits, now.astimezone(tz))
    with db:
        db.execute("""
            INSERT INTO ch_schedule_observations(start_utc,state,observed_utc)
            VALUES(?,?,?)
            ON CONFLICT(start_utc) DO UPDATE SET
                state=CASE WHEN ch_schedule_observations.state=excluded.state
                           THEN excluded.state ELSE NULL END,
                observed_utc=excluded.observed_utc
        """, (start.isoformat(), state, utc.isoformat()))
        # Purge only old snapshots, preserving all observations in the training window.
        db.execute("DELETE FROM ch_schedule_observations WHERE start_utc<?",
                   ((utc - timedelta(days=190)).isoformat(),))
    return bits


def observations(db: sqlite3.Connection, begin: datetime, end: datetime) -> dict[str, int | None]:
    return {
        ts: state for ts, state in db.execute(
            "SELECT start_utc,state FROM ch_schedule_observations WHERE start_utc>=? AND start_utc<?",
            (begin.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        )
    }


def learned_models(db: sqlite3.Connection, cutoff_day: str, fallback: float) -> dict[str, dict]:
    """Compute heating-active DD-weighted rates for each *observed* schedule class."""
    rows = db.execute("""
        SELECT schedule_state, COUNT(*), COUNT(DISTINCT day),
               COALESCE(SUM(degree_days),0), COALESCE(SUM(ch_kwh),0)
        FROM slot_training
        WHERE valid=1 AND day>=? AND degree_days>0 AND schedule_state IN (0,1)
        GROUP BY schedule_state
    """, (cutoff_day,)).fetchall()
    by_state = {int(state): (n, days, dd, kwh) for state, n, days, dd, kwh in rows}
    result = {}
    for key, state in (("normal", 1), ("setback", 0)):
        count, days, dd, kwh = by_state.get(state, (0, 0, 0.0, 0.0))
        trained = count >= MIN_LEARNED_SLOTS and dd >= MIN_LEARNED_DD
        result[key] = {
            "coefficient": float(kwh / dd if trained else fallback),
            "learned": bool(trained),
            "slots": int(count),
            "days": int(days),
            "degree_days": float(dd),
            "ch_kwh": float(kwh),
        }
    return result


def publish_models(client, models: dict[str, dict], fallback: float) -> None:
    if not callable(getattr(client, "set_sensor", None)):
        return
    for state, info in models.items():
        client.set_sensor(
            f"sensor.ashp_forecast_{state}_kwh_per_degree_day",
            round(info["coefficient"], 4),
            {
                "friendly_name": f"ASHP CH {state.title()} kWh per Degree Day",
                "unit_of_measurement": "kWh/DD",
                "learned": info["learned"],
                "source": "learned" if info["learned"] else "overall_fallback",
                "training_slots": info["slots"],
                "training_days": info["days"],
                "training_degree_days": round(info["degree_days"], 4),
                "training_ch_kwh": round(info["ch_kwh"], 4),
                "overall_kwh_per_degree_day": round(fallback, 4),
            },
        )
