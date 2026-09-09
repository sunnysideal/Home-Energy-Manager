#!/usr/bin/env python3
"""Passive two-sensor DHW data collection for the ASHP forecaster.

This process deliberately does not publish forecasts or influence DHW/CH calculations.
It only records observations into the ASHP forecaster database so the thermal model can
be trained and validated before it becomes authoritative.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dhw_draw_detector import ThermalSample, detect_draw
from dhw_model import ensure_dhw_model_schema
from dhw_passive_learner import learn_passive_parameters, persist_passive_fit

LOG = logging.getLogger("ashp_dhw_collector")
HA_API = "http://supervisor/core/api"
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


def _finite_state(token: str, entity_id: str) -> float | None:
    if not entity_id:
        return None
    req = Request(
        f"{HA_API}/states/{quote(entity_id, safe='.')}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as response:
            value = float(json.loads(response.read())["state"])
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return value if math.isfinite(value) else None


def _valid_tank_temp(value: float | None) -> bool:
    return value is not None and 5.0 <= value <= 80.0


def _next_boundary(now: datetime, minutes: int) -> datetime:
    step = max(1, int(minutes))
    base = now.replace(second=0, microsecond=0)
    remainder = base.minute % step
    if remainder == 0 and now.second == 0 and now.microsecond == 0:
        return now
    return base + timedelta(minutes=(step - remainder if remainder else step))


def _ensure_metadata(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _metadata_float(db: sqlite3.Connection, key: str) -> float | None:
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _set_metadata(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _previous_sample(db: sqlite3.Connection, before_ts: str) -> ThermalSample | None:
    row = db.execute(
        "SELECT timestamp,upper_temp_c,lower_temp_c,dhw_heating,valid "
        "FROM dhw_thermal_samples WHERE timestamp<? AND upper_temp_c IS NOT NULL "
        "AND lower_temp_c IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
        (before_ts,),
    ).fetchone()
    if not row:
        return None
    try:
        return ThermalSample(
            timestamp=datetime.fromisoformat(str(row[0])),
            upper_temp_c=float(row[1]),
            lower_temp_c=float(row[2]),
            heating=bool(row[3]),
            valid=bool(row[4]),
        )
    except (TypeError, ValueError):
        return None


def record_sample(db: sqlite3.Connection, token: str, cfg: dict) -> bool:
    upper_entity = str(cfg.get("dhw_tank_upper_temperature_entity") or cfg.get("dhw_tank_temperature_entity") or "")
    lower_entity = str(cfg.get("dhw_tank_lower_temperature_entity") or "")
    if not upper_entity or not lower_entity:
        return False

    upper = _finite_state(token, upper_entity)
    lower = _finite_state(token, lower_entity)
    valid = _valid_tank_temp(upper) and _valid_tank_temp(lower)

    energy_entity = str(cfg.get("dhw_energy_entity") or "")
    energy_total = _finite_state(token, energy_entity)
    previous_total = _metadata_float(db, "dhw_collector_last_energy_total_kwh")
    energy_delta = None
    if energy_total is not None and previous_total is not None:
        delta = energy_total - previous_total
        if 0.0 <= delta <= 10.0:
            energy_delta = delta

    ambient_entity = str(cfg.get("dhw_ambient_temperature_entity") or "")
    ambient = _finite_state(token, ambient_entity) if ambient_entity else None
    outdoor = _finite_state(token, str(cfg.get("outdoor_temperature_entity") or ""))
    activity_threshold = max(0.0, float(cfg.get("dhw_activity_threshold_kwh", 0.2)))
    heating = int(energy_delta is not None and energy_delta >= min(activity_threshold, 0.05))
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    previous_sample = _previous_sample(db, now)

    draw_event = None
    if valid and upper is not None and lower is not None and previous_sample is not None:
        draw_event = detect_draw(
            previous_sample,
            ThermalSample(now_dt, upper, lower, bool(heating), True),
            volume_l=float(cfg.get("dhw_tank_volume_l", 250)),
        )

    with db:
        db.execute(
            "INSERT INTO dhw_thermal_samples("
            "timestamp,upper_temp_c,lower_temp_c,dhw_heating,immersion_heating,"
            "dhw_energy_delta_kwh,dhw_energy_total_kwh,ambient_temp_c,outdoor_temp_c,valid"
            ") VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(timestamp) DO UPDATE SET "
            "upper_temp_c=excluded.upper_temp_c,lower_temp_c=excluded.lower_temp_c,"
            "dhw_heating=excluded.dhw_heating,immersion_heating=excluded.immersion_heating,"
            "dhw_energy_delta_kwh=excluded.dhw_energy_delta_kwh,"
            "dhw_energy_total_kwh=excluded.dhw_energy_total_kwh,"
            "ambient_temp_c=excluded.ambient_temp_c,outdoor_temp_c=excluded.outdoor_temp_c,"
            "valid=excluded.valid",
            (now, upper, lower, heating, 0, energy_delta, energy_total, ambient, outdoor, int(valid)),
        )
        if draw_event is not None:
            db.execute(
                "INSERT INTO dhw_draw_events("
                "timestamp,estimated_thermal_kwh,confidence,upper_before_c,lower_before_c,"
                "upper_after_c,lower_after_c) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(timestamp) DO UPDATE SET "
                "estimated_thermal_kwh=excluded.estimated_thermal_kwh,confidence=excluded.confidence,"
                "upper_before_c=excluded.upper_before_c,lower_before_c=excluded.lower_before_c,"
                "upper_after_c=excluded.upper_after_c,lower_after_c=excluded.lower_after_c",
                (
                    draw_event.timestamp.isoformat(), draw_event.estimated_thermal_kwh,
                    draw_event.confidence, draw_event.upper_before_c, draw_event.lower_before_c,
                    draw_event.upper_after_c, draw_event.lower_after_c,
                ),
            )
        if energy_total is not None:
            _set_metadata(db, "dhw_collector_last_energy_total_kwh", str(energy_total))
            _set_metadata(db, "dhw_collector_last_energy_timestamp", now)

    LOG.info(
        "DHW thermal sample: upper=%s lower=%s delta_kwh=%s heating=%s draw=%s ambient=%s valid=%s",
        f"{upper:.2f}" if upper is not None else "unavailable",
        f"{lower:.2f}" if lower is not None else "unavailable",
        f"{energy_delta:.4f}" if energy_delta is not None else "n/a",
        bool(heating),
        f"{draw_event.estimated_thermal_kwh:.3f}kWh" if draw_event is not None else "none",
        f"{ambient:.2f}" if ambient is not None else "default-later",
        valid,
    )
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(OPTIONS_PATH.read_text())
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")

    db = sqlite3.connect(DB_PATH, timeout=30)
    _ensure_metadata(db)
    ensure_dhw_model_schema(db)
    db.commit()

    upper = str(cfg.get("dhw_tank_upper_temperature_entity") or cfg.get("dhw_tank_temperature_entity") or "")
    lower = str(cfg.get("dhw_tank_lower_temperature_entity") or "")
    if not lower:
        LOG.info(
            "DHW thermal sampling inactive: configure dhw_tank_lower_temperature_entity; "
            "legacy DHW forecast remains authoritative"
        )
    else:
        LOG.info(
            "DHW thermal sampling active: upper=%s lower=%s; legacy DHW forecast remains authoritative",
            upper, lower,
        )

    sample_minutes = max(1, int(cfg.get("dhw_thermal_sample_minutes", 5)))
    last_fit_monotonic = -3600.0
    while True:
        now = datetime.now(timezone.utc)
        boundary = _next_boundary(now, sample_minutes)
        delay = max(0.0, (boundary - now).total_seconds())
        if delay:
            time.sleep(delay)
        try:
            if lower:
                record_sample(db, token, cfg)
                if time.monotonic() - last_fit_monotonic >= 3600.0:
                    fit = learn_passive_parameters(
                        db,
                        volume_l=float(cfg.get("dhw_tank_volume_l", 250)),
                    )
                    last_fit_monotonic = time.monotonic()
                    if fit is not None:
                        persist_passive_fit(db, fit)
                        LOG.info(
                            "DHW passive model learned: upper_loss=%.3fW/K lower_loss=%.3fW/K "
                            "coupling=%.3fW/K intervals=%d rmse=%.3fC",
                            fit.upper_loss_w_per_k,
                            fit.lower_loss_w_per_k,
                            fit.coupling_w_per_k,
                            fit.sample_count,
                            fit.rmse_c,
                        )
                    else:
                        LOG.info("DHW passive model not ready: insufficient/unsuitable quiet samples")
        except Exception:
            LOG.exception("DHW thermal sample/learning failed")
        time.sleep(0.25)


if __name__ == "__main__":
    main()
