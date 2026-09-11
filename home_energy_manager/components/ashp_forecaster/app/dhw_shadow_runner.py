#!/usr/bin/env python3
"""Publish and validate the two-sensor DHW thermal forecast alongside production DHW."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from common.mqtt import MQTTPublisher
from dhw_model import ensure_dhw_model_schema
from dhw_shadow_forecast import build_shadow_forecast, load_shadow_model, persist_shadow_validation

LOG = logging.getLogger("ashp_dhw_shadow")
HA_API = "http://supervisor/core/api"
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LEGACY_FORECAST_ENTITY = "sensor.ashp_forecast_next_48h"
THERMAL_FORECAST_ENTITY = "sensor.ashp_dhw_thermal_forecast_next_48h"
COMPARISON_ENTITY = "sensor.ashp_dhw_forecast_comparison"
PRODUCTION_SOURCE_ENTITY = "sensor.ashp_dhw_production_source"
PRODUCTION_INTERVAL_MINUTES = 30
PRODUCTION_HOURS = 48
PRODUCTION_SLOTS = PRODUCTION_HOURS * 60 // PRODUCTION_INTERVAL_MINUTES
THERMAL_STEP_MINUTES = 5
DRAW_REFRESH_POLL_SECONDS = 5.0


class Publisher:
    def __init__(self, token: str) -> None:
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.mqtt = MQTTPublisher(
            "ashp_dhw_thermal_forecast",
            "Home Energy Manager – DHW Thermal Forecast",
            "DHW Thermal Forecast",
            os.environ.get("HOME_ENERGY_MANAGER_VERSION", "unknown"),
            token,
        )

    def _rest(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
        payload = json.dumps({"state": str(state), "attributes": attributes}, default=str).encode()
        req = Request(f"{HA_API}/states/{entity_id}", data=payload, headers=self.headers, method="POST")
        try:
            with urlopen(req, timeout=15):
                pass
        except (HTTPError, URLError, TimeoutError) as exc:
            LOG.warning("Could not publish %s through HA REST: %s", entity_id, exc)

    def sensor(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
        if self.mqtt.publish_sensor(entity_id, state, attributes):
            return
        self._rest(entity_id, state, attributes)


def _ha_json(token: str, path: str) -> dict | None:
    req = Request(f"{HA_API}{path}", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="GET")
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


def _legacy_forecast(token: str) -> tuple[dict[str, float], float]:
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
    return out, sum(out.values())


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


def _target_sensor_from_config(cfg: dict) -> str:
    """Map the configured controller tank-temperature entity to its thermal zone.

    dhw_tank_temperature_entity is the authoritative sensor used by the real DHW
    controller for target/hysteresis decisions. The two thermal entities describe its
    physical position. Refuse to guess if the configured control sensor is neither.
    """
    control = str(cfg.get("dhw_tank_temperature_entity") or "").strip()
    upper = str(cfg.get("dhw_tank_upper_temperature_entity") or "").strip()
    lower = str(cfg.get("dhw_tank_lower_temperature_entity") or "").strip()
    if control and lower and control == lower:
        return "lower"
    if control and upper and control == upper:
        return "upper"
    raise ValueError(
        "dhw_tank_temperature_entity must match either dhw_tank_upper_temperature_entity "
        "or dhw_tank_lower_temperature_entity so the DHW target sensor is unambiguous"
    )


def _latest_draw_event(db: sqlite3.Connection) -> tuple[str, float, float] | None:
    row = db.execute("SELECT timestamp,estimated_thermal_kwh,confidence FROM dhw_draw_events ORDER BY timestamp DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return str(row[0]), float(row[1]), float(row[2])
    except (TypeError, ValueError):
        return None


def _ceil_local(dt: datetime, minutes: int) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    remainder = base.minute % minutes
    if remainder == 0 and dt.second == 0 and dt.microsecond == 0:
        return base
    if remainder == 0:
        return base + timedelta(minutes=minutes)
    return base + timedelta(minutes=minutes - remainder)


def _production_starts(now: datetime) -> list[datetime]:
    start = _ceil_local(now, PRODUCTION_INTERVAL_MINUTES)
    return [start + timedelta(minutes=PRODUCTION_INTERVAL_MINUTES * i) for i in range(PRODUCTION_SLOTS)]


def _required_simulation_hours(simulation_start: datetime, production_starts: list[datetime]) -> float:
    if not production_starts:
        return float(PRODUCTION_HOURS)
    required_end = production_starts[-1] + timedelta(minutes=PRODUCTION_INTERVAL_MINUTES)
    seconds = max(0.0, (required_end - simulation_start).total_seconds())
    steps = math.ceil(seconds / (THERMAL_STEP_MINUTES * 60.0))
    return steps * THERMAL_STEP_MINUTES / 60.0


def _published_half_hours(slots: list, production_starts: list[datetime], *, step_minutes: int = THERMAL_STEP_MINUTES) -> tuple[list[dict[str, Any]], bool]:
    if not slots or not production_starts:
        return [], False
    by_start = {slot.start.astimezone(timezone.utc): slot for slot in slots}
    rows: list[dict[str, Any]] = []
    for cursor in production_starts:
        chunk = [by_start.get((cursor + timedelta(minutes=i)).astimezone(timezone.utc)) for i in range(0, PRODUCTION_INTERVAL_MINUTES, step_minutes)]
        if any(slot is None for slot in chunk):
            return rows, False
        complete = [slot for slot in chunk if slot is not None]
        endpoint = complete[-1]
        dhw_kwh = sum(float(slot.dhw_kwh) for slot in complete)
        draw_kwh = sum(float(slot.draw_kwh) for slot in complete)
        rows.append({
            "start": cursor.isoformat(), "dhw_active": dhw_kwh > 0.0, "dhw_kwh": round(dhw_kwh, 4),
            "predicted_draw_kwh": round(draw_kwh, 4), "upper_temperature_c": round(float(endpoint.upper_temp_c), 2),
            "lower_temperature_c": round(float(endpoint.lower_temp_c), 2),
        })
    return rows, len(rows) == len(production_starts) == PRODUCTION_SLOTS


def _publish_not_ready(publisher: Publisher, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    publisher.sensor(THERMAL_FORECAST_ENTITY, "unknown", {
        "friendly_name": "ASHP DHW Thermal Forecast Next 48h", "model": "two_zone_thermal_shadow",
        "status": "learning", "reason": reason, "forecast": [], "forecast_slots": 0,
        "horizon_complete": False, "last_updated": now,
    })
    publisher.sensor(COMPARISON_ENTITY, "learning", {
        "friendly_name": "ASHP DHW Forecast Comparison", "thermal_ready": False,
        "reason": reason, "last_updated": now,
    })


def run_shadow_once(db: sqlite3.Connection, token: str, cfg: dict, timezone_name: str, publisher: Publisher | None = None, *, trigger: str = "scheduled") -> int:
    model = load_shadow_model(db)
    if model is None:
        reason = "cycle_response_model_unavailable"
        LOG.info("DHW shadow forecast not ready: cycle response model unavailable")
        if publisher is not None:
            _publish_not_ready(publisher, reason)
        return 0
    current = _latest_tank_state(db)
    if current is None:
        reason = "no_valid_dual_temperature_sample"
        LOG.info("DHW shadow forecast not ready: no valid dual-temperature sample")
        if publisher is not None:
            _publish_not_ready(publisher, reason)
        return 0
    upper, lower, ambient = current
    try:
        target_sensor = _target_sensor_from_config(cfg)
    except ValueError as exc:
        reason = "dhw_target_sensor_configuration_invalid"
        LOG.error("DHW shadow forecast not ready: %s", exc)
        if publisher is not None:
            _publish_not_ready(publisher, reason)
        return 0

    target = _state_float(token, str(cfg.get("dhw_target_temperature_entity") or ""))
    hysteresis = _state_float(token, str(cfg.get("dhw_hysteresis_entity") or ""))
    mode = _state_text(token, str(cfg.get("dhw_mode_entity") or ""))
    if target is None or hysteresis is None or mode is None:
        reason = "target_hysteresis_or_mode_unavailable"
        LOG.info("DHW shadow forecast not ready: target/hysteresis/mode unavailable")
        if publisher is not None:
            _publish_not_ready(publisher, reason)
        return 0

    prefix = str(cfg.get("dhw_schedule_prefix") or "")
    schedule = _schedule_bits(token, prefix) if prefix else {}
    tz = ZoneInfo(timezone_name)
    now_local = datetime.now(tz)
    simulation_start = _ceil_local(now_local, THERMAL_STEP_MINUTES)
    production_starts = _production_starts(now_local)
    simulation_hours = _required_simulation_hours(simulation_start, production_starts)

    slots = build_shadow_forecast(
        start=simulation_start, initial_upper_c=upper, initial_lower_c=lower,
        target_temp_c=target, hysteresis_c=max(0.0, hysteresis), mode=mode,
        schedule_bits=schedule, model=model, target_sensor=target_sensor,
        tank_volume_l=float(cfg.get("dhw_tank_volume_l", 250)),
        ambient_temp_c=ambient if ambient is not None else 20.0,
        minimum_useful_temperature_c=float(cfg.get("dhw_min_usable_temperature_c", 40.0)),
        horizon_hours=simulation_hours, step_minutes=THERMAL_STEP_MINUTES,
    )
    forecast_ts = datetime.now(timezone.utc)
    legacy, legacy_total = _legacy_forecast(token)
    legacy_available = bool(legacy)
    if not legacy_available:
        LOG.info("DHW legacy comparison deferred: production forecast not yet published")
    checkpoints = persist_shadow_validation(db, slots, forecast_ts=forecast_ts, legacy_dhw_by_start=legacy if legacy_available else {})
    published, horizon_complete = _published_half_hours(slots, production_starts)
    total_dhw = sum(float(row["dhw_kwh"]) for row in published)
    production_source = (_state_text(token, PRODUCTION_SOURCE_ENTITY) or "legacy").strip().lower()
    if production_source not in {"legacy", "thermal"}:
        production_source = "legacy"
    thermal_authoritative = production_source == "thermal"
    production_start = production_starts[0].isoformat() if production_starts else None
    production_end = production_starts[-1] + timedelta(minutes=PRODUCTION_INTERVAL_MINUTES) if production_starts else None

    if publisher is not None:
        now = forecast_ts.isoformat()
        publisher.sensor(THERMAL_FORECAST_ENTITY, round(total_dhw, 3), {
            "friendly_name": "ASHP DHW Thermal Forecast Next 48h", "unit_of_measurement": "kWh", "device_class": "energy",
            "model": "two_zone_thermal_shadow", "status": "published_shadow", "authoritative": thermal_authoritative,
            "forecast": published, "forecast_slots": len(published), "expected_forecast_slots": PRODUCTION_SLOTS,
            "horizon_complete": horizon_complete, "production_start": production_start,
            "production_end": production_end.isoformat() if production_end else None, "simulation_start": simulation_start.isoformat(),
            "simulation_slots": len(slots), "start_upper_temperature_c": round(upper, 2), "start_lower_temperature_c": round(lower, 2),
            "target_temperature_c": round(target, 2), "target_sensor": target_sensor,
            "target_sensor_entity": str(cfg.get("dhw_tank_temperature_entity") or ""), "mode": mode, "trigger": trigger, "last_updated": now,
        })
        comparison_attrs = {
            "friendly_name": "ASHP DHW Forecast Comparison", "unit_of_measurement": "kWh", "thermal_dhw_kwh": round(total_dhw, 3),
            "thermal_forecast_entity": THERMAL_FORECAST_ENTITY, "legacy_forecast_entity": LEGACY_FORECAST_ENTITY,
            "production_source_entity": PRODUCTION_SOURCE_ENTITY, "authoritative_forecast": production_source,
            "thermal_forecast_published": True, "horizon_complete": horizon_complete,
            "legacy_comparison_available": legacy_available, "trigger": trigger, "last_updated": now,
        }
        if legacy_available:
            comparison_attrs.update({"legacy_dhw_kwh": round(legacy_total, 3), "difference_kwh": round(total_dhw - legacy_total, 3)})
            comparison_state: Any = round(total_dhw - legacy_total, 3)
        else:
            comparison_attrs["reason"] = "legacy_forecast_not_yet_published"
            comparison_state = "waiting"
        publisher.sensor(COMPARISON_ENTITY, comparison_state, comparison_attrs)

    LOG.info(
        "DHW shadow forecast published: trigger=%s simulation_slots=%d published_slots=%d horizon_complete=%s "
        "checkpoints=%d thermal_next_48h=%.2fkWh legacy_snapshot=%s legacy_slots=%d production_source=%s "
        "production_start=%s start_upper=%.1fC start_lower=%.1fC target=%.1fC target_sensor=%s mode=%s",
        trigger, len(slots), len(published), horizon_complete, checkpoints, total_dhw,
        f"{legacy_total:.2f}kWh" if legacy_available else "deferred", len(legacy), production_source,
        production_start, upper, lower, target, target_sensor, mode,
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
    publisher = Publisher(token)
    update_minutes = max(5, int(cfg.get("update_minutes", 15)))
    update_seconds = update_minutes * 60.0
    initial_draw = _latest_draw_event(db)
    last_draw_timestamp = initial_draw[0] if initial_draw is not None else None
    next_scheduled_run = 0.0
    while True:
        now_monotonic = time.monotonic()
        trigger: str | None = None
        latest_draw = _latest_draw_event(db)
        if latest_draw is not None and latest_draw[0] != last_draw_timestamp:
            last_draw_timestamp = latest_draw[0]
            trigger = "dhw_draw"
            LOG.info("DHW draw detected; refreshing thermal forecast immediately: timestamp=%s estimated=%.3fkWh confidence=%.0f%%", latest_draw[0], latest_draw[1], latest_draw[2] * 100.0)
        elif now_monotonic >= next_scheduled_run:
            trigger = "startup" if next_scheduled_run == 0.0 else "scheduled"
        if trigger is not None:
            try:
                run_shadow_once(db, token, cfg, timezone_name, publisher, trigger=trigger)
            except Exception:
                LOG.exception("DHW shadow forecast failed")
            next_scheduled_run = time.monotonic() + update_seconds
        sleep_for = min(DRAW_REFRESH_POLL_SECONDS, max(0.1, next_scheduled_run - time.monotonic()))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
