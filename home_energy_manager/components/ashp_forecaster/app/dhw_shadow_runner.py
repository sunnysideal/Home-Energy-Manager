#!/usr/bin/env python3
"""Hourly runner for passive DHW shadow forecasts and validation checkpoints."""
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
from zoneinfo import ZoneInfo

from dhw_model import ensure_dhw_model_schema
from dhw_shadow_forecast import build_shadow_forecast, load_shadow_model, persist_shadow_validation

LOG = logging.getLogger("ashp_dhw_shadow")
HA_API = "http://supervisor/core/api"
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LEGACY_FORECAST_ENTITY = "sensor.ashp_forecast_next_48h"


def _ha_json(token: str, path: str) -> dict | None:
    req = Request(
        f"{HA_API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as response:
            data = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _state_text(token: str, entity_id: str) -> str | None:
    if not entity_id:
        return None
    data = _ha_json(token, f"/states/{quote(entity_id, safe='.')}")
    if not data:
        return None
    value = data.get("state")
    return str(value) if value is not None else None


def _state_float(token: str, entity_id: str) -> float | None:
    raw = _state_text(token, entity_id)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _legacy_dhw_by_start(token: str) -> dict[str, float]:
    state = _ha_json(token, f"/states/{quote(LEGACY_FORECAST_ENTITY, safe='.')}") or {}
    attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    forecast = attributes.get("forecast") if isinstance(attributes.get("forecast"), list) else []
    out: dict[str, float] = {}
    for row in forecast:
        if not isinstance(row, dict):
            continue
        try:
            start = datetime.fromisoformat(str(row["start"])).astimezone(timezone.utc).isoformat()
            dhw = max(0.0, float(row.get("dhw_kwh", 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
        out[start] = dhw
    return out


def _schedule_bits(token: str, prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for day in WEEKDAYS:
        for half in ("am", "pm"):
            key = f"{day}_{half}"
            raw = _state_float(token, f"{prefix}{key}")
            out[key] = int(raw) if raw is not None else 0
    return out


def _latest_tank_state(db: sqlite3.Connection) -> tuple[float, float, float | None] | None:
    row = db.execute(
        "SELECT upper_temp_c,lower_temp_c,ambient_temp_c FROM dhw_thermal_samples "
        "WHERE valid=1 AND upper_temp_c IS NOT NULL AND lower_temp_c IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return float(row[0]), float(row[1]), (float(row[2]) if row[2] is not None else None)


def _ceil_local(dt: datetime, minutes: int = 5) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    remainder = base.minute % minutes
    if remainder == 0:
        return base
    return base + timedelta(minutes=minutes - remainder)


def run_shadow_once(db: sqlite3.Connection, token: str, cfg: dict, timezone_name: str) -> int:
    model = load_shadow_model(db)
    if model is None:
        LOG.info("DHW shadow forecast not ready: cycle response model unavailable")
        return 0
    current = _latest_tank_state(db)
    if current is None:
        LOG.info("DHW shadow forecast not ready: no valid dual-temperature sample")
        return 0
    upper, lower, ambient = current

    target = _state_float(token, str(cfg.get("dhw_target_temperature_entity") or ""))
    hysteresis = _state_float(token, str(cfg.get("dhw_hysteresis_entity") or ""))
    mode = _state_text(token, str(cfg.get("dhw_mode_entity") or ""))
    if target is None or hysteresis is None or mode is None:
        LOG.info("DHW shadow forecast not ready: target/hysteresis/mode unavailable")
        return 0

    prefix = str(cfg.get("dhw_schedule_prefix") or "")
    schedule = _schedule_bits(token, prefix) if prefix else {}
    tz = ZoneInfo(timezone_name)
    start = _ceil_local(datetime.now(tz), 5)
    slots = build_shadow_forecast(
        start=start,
        initial_upper_c=upper,
        initial_lower_c=lower,
        target_temp_c=target,
        hysteresis_c=max(0.0, hysteresis),
        mode=mode,
        schedule_bits=schedule,
        model=model,
        tank_volume_l=float(cfg.get("dhw_tank_volume_l", 250)),
        ambient_temp_c=ambient if ambient is not None else 20.0,
        minimum_useful_temperature_c=float(cfg.get("dhw_min_usable_temperature_c", 40.0)),
        horizon_hours=48,
        step_minutes=5,
    )
    forecast_ts = datetime.now(timezone.utc)
    legacy = _legacy_dhw_by_start(token)
    checkpoints = persist_shadow_validation(
        db,
        slots,
        forecast_ts=forecast_ts,
        legacy_dhw_by_start=legacy,
    )
    total_dhw = sum(slot.dhw_kwh for slot in slots)
    legacy_total = sum(legacy.values())
    LOG.info(
        "DHW shadow forecast stored: slots=%d checkpoints=%d next_48h_dhw=%.2fkWh "
        "legacy_snapshot=%.2fkWh legacy_slots=%d start_upper=%.1fC start_lower=%.1fC "
        "target=%.1fC mode=%s",
        len(slots), checkpoints, total_dhw, legacy_total, len(legacy), upper, lower, target, mode,
    )
    return checkpoints


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(OPTIONS_PATH.read_text())
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")
    ha_cfg = _ha_json(token, "/config") or {}
    timezone_name = str(ha_cfg.get("time_zone") or "UTC")

    db = sqlite3.connect(DB_PATH, timeout=30)
    ensure_dhw_model_schema(db)

    while True:
        try:
            run_shadow_once(db, token, cfg, timezone_name)
        except Exception:
            LOG.exception("DHW shadow forecast failed")
        time.sleep(3600)


if __name__ == "__main__":
    main()
