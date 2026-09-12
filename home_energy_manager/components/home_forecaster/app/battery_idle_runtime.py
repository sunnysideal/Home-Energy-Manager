#!/usr/bin/env python3
"""Learn battery/inverter standby loss from clean PauseBoth periods.

This runtime layer is deliberately limited to Home Forecaster telemetry and
physical forecast modelling.  It never writes inverter settings and it does not
introduce controller policy.  Axle pricing remains the next layer down in the
runtime stack.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import axle_pricing_core as pricing

base = pricing.base

IDLE_ENTITY = "sensor.home_energy_manager_battery_standby_loss"
AXLE_ENTITY = "sensor.home_energy_manager_axle"
SETTLE_MINUTES = 10
SAMPLE_WINDOW_MINUTES = 5
FLOW_EPSILON_KWH = 0.0005
ENERGY_ACTIVITY_EPSILON_KWH = 0.01

_active_standby_w = 0.0
_last_model: dict[str, Any] = {}
_original_simulate_battery = base.simulate_battery
_wrapped_make_forecast = base.make_forecast


def _ensure_schema(store) -> None:
    store.db.execute(
        """
        CREATE TABLE IF NOT EXISTS battery_idle_samples (
            observed_at TEXT PRIMARY KEY,
            watts REAL NOT NULL,
            soc REAL NOT NULL,
            pause_age_minutes REAL NOT NULL
        )
        """
    )
    store.db.execute(
        "CREATE INDEX IF NOT EXISTS idx_battery_idle_samples_observed_at "
        "ON battery_idle_samples(observed_at)"
    )
    store.db.commit()


def _settings(cfg) -> dict[str, Any]:
    battery = cfg.section("battery")
    return {
        "threshold_w": max(0.0, float(battery.get("battery_idle_threshold_w", 75))),
        "history_days": max(1, int(battery.get("battery_idle_history_days", 14))),
        "min_samples": max(1, int(battery.get("battery_idle_min_samples", 12))),
        "soc_delta_pct": max(0.0, float(battery.get("battery_idle_soc_delta_pct", 0.15))),
    }


def _state_time(state: dict[str, Any] | None) -> datetime | None:
    if not state:
        return None
    stamp = state.get("last_changed") or state.get("last_updated")
    try:
        return base.parse_dt(stamp).astimezone(timezone.utc) if stamp else None
    except Exception:
        return None


def _current_window_start(now: datetime, start_s: str, end_s: str) -> datetime | None:
    start = base.parse_hhmm(start_s)
    end = base.parse_hhmm(end_s)
    if not start or not end or start == end or not base.in_daily_window(now, start_s, end_s):
        return None
    local = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    if start_minutes > end_minutes and (now.hour * 60 + now.minute) < end_minutes:
        local -= timedelta(days=1)
    return local


def _numeric_history_values(states: list[dict[str, Any]], start: datetime, end: datetime) -> list[float]:
    values: list[float] = []
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    initial = base.latest_before(states, start)
    if initial is not None:
        values.append(float(initial))
    for item in states:
        stamp = item.get("last_changed") or item.get("last_updated")
        try:
            when = base.parse_dt(stamp).astimezone(timezone.utc)
            value = float(item.get("state"))
        except (TypeError, ValueError, OverflowError):
            continue
        if start_utc < when <= end_utc:
            values.append(value)
    return values


def _active_schedule(client, battery: dict[str, Any], now: datetime, kind: str) -> bool:
    enabled_key = f"{kind}_schedule_enabled"
    if not base.bool_state(client.state_optional(str(battery.get(enabled_key) or "")), False):
        return False
    for index in (1, 2):
        start_state = client.state_optional(str(battery.get(f"{kind}_start_{index}") or ""))
        end_state = client.state_optional(str(battery.get(f"{kind}_end_{index}") or ""))
        start_s = str(start_state.get("state", "00:00")) if start_state else "00:00"
        end_s = str(end_state.get("state", "00:00")) if end_state else "00:00"
        if base.in_daily_window(now, start_s, end_s):
            return True
    return False


def _axle_active(client) -> bool:
    state = client.state_optional(AXLE_ENTITY)
    if not isinstance(state, dict):
        return False
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    return bool(attrs.get("active"))


def _reject(store, reason: str, now: datetime) -> None:
    previous = store.meta_get("battery_idle_last_rejection", "")
    payload = json.dumps({"at": now.isoformat(), "reason": reason}, separators=(",", ":"))
    store.meta_set("battery_idle_last_rejection", payload)
    if reason != previous:
        base.LOG.debug("Battery idle sample rejected: %s", reason)


def _last_rejection(store) -> dict[str, Any] | None:
    raw = store.meta_get("battery_idle_last_rejection", "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _model(store, cfg, now: datetime) -> dict[str, Any]:
    settings = _settings(cfg)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=settings["history_days"])
    store.db.execute("DELETE FROM battery_idle_samples WHERE observed_at < ?", (cutoff.isoformat(),))
    store.db.commit()
    rows = store.db.execute(
        "SELECT observed_at,watts,soc,pause_age_minutes FROM battery_idle_samples "
        "ORDER BY observed_at"
    ).fetchall()
    watts = [float(row["watts"]) for row in rows]
    count = len(watts)
    learned_w = statistics.median(watts) if watts else None
    ready = count >= settings["min_samples"]
    last = rows[-1]["observed_at"] if rows else None
    return {
        "learned_w": round(learned_w, 1) if learned_w is not None else None,
        "sample_count": count,
        "minimum_samples": settings["min_samples"],
        "history_days": settings["history_days"],
        "idle_threshold_w": settings["threshold_w"],
        "soc_delta_tolerance_pct": settings["soc_delta_pct"],
        "settle_minutes": SETTLE_MINUTES,
        "sample_window_minutes": SAMPLE_WINDOW_MINUTES,
        "confidence": round(min(1.0, count / settings["min_samples"]), 2),
        "readiness": "ready" if ready else "collecting",
        "ready": ready,
        "forecast_applied": ready and learned_w is not None,
        "last_accepted_at": last,
        "last_rejection": _last_rejection(store),
        "source": "PauseBoth clean idle samples",
    }


def learn_idle_loss(client, store, cfg, now: datetime) -> dict[str, Any]:
    """Accept at most one clean idle observation for this forecast cycle."""
    _ensure_schema(store)
    settings = _settings(cfg)
    battery = cfg.section("battery")

    power_entity = str(battery.get("battery_power_w") or "").strip()
    soc_entity = str(battery.get("soc") or "").strip()
    pause_entity = str(battery.get("pause_mode") or "").strip()
    if not power_entity or not soc_entity or not pause_entity:
        _reject(store, "required battery power/SOC/pause entity not configured", now)
        return _model(store, cfg, now)

    pause_state = client.state_optional(pause_entity)
    if not pause_state or str(pause_state.get("state") or "") != "PauseBoth":
        _reject(store, "pause mode is not PauseBoth", now)
        return _model(store, cfg, now)

    pause_start_state = client.state_optional(str(battery.get("pause_start") or ""))
    pause_end_state = client.state_optional(str(battery.get("pause_end") or ""))
    pause_start_s = str(pause_start_state.get("state", "00:00")) if pause_start_state else "00:00"
    pause_end_s = str(pause_end_state.get("state", "00:00")) if pause_end_state else "00:00"
    window_start = _current_window_start(now, pause_start_s, pause_end_s)
    if window_start is None:
        _reject(store, "PauseBoth is not active in the configured pause window", now)
        return _model(store, cfg, now)

    pause_changed = _state_time(pause_state)
    effective_start_utc = window_start.astimezone(timezone.utc)
    if pause_changed is not None:
        effective_start_utc = max(effective_start_utc, pause_changed)
    pause_age = (now.astimezone(timezone.utc) - effective_start_utc).total_seconds() / 60.0
    if pause_age < SETTLE_MINUTES:
        _reject(store, f"PauseBoth settling ({pause_age:.1f}/{SETTLE_MINUTES} min)", now)
        return _model(store, cfg, now)

    if _active_schedule(client, battery, now, "charge") or _active_schedule(client, battery, now, "discharge"):
        _reject(store, "intentional charge/discharge schedule is active", now)
        return _model(store, cfg, now)
    if _axle_active(client):
        _reject(store, "Axle event is active", now)
        return _model(store, cfg, now)

    sample_start = now - timedelta(minutes=SAMPLE_WINDOW_MINUTES)
    solar_entity = str(cfg.section("solar").get("energy_total_kwh") or "").strip()
    history_entities = [power_entity, soc_entity]
    if solar_entity:
        history_entities.append(solar_entity)
    for key in ("battery_charge_energy_total_kwh", "battery_discharge_energy_total_kwh"):
        entity = str(battery.get(key) or "").strip()
        if entity:
            history_entities.append(entity)

    hist = client.history(history_entities, sample_start - timedelta(minutes=5), now, timeout=45)
    power_values = _numeric_history_values(hist.get(power_entity, []), sample_start, now)
    current_power = base.numeric_state(client.state_optional(power_entity))
    if current_power is not None:
        power_values.append(float(current_power))
    if not power_values:
        _reject(store, "battery power history unavailable", now)
        return _model(store, cfg, now)
    if max(abs(v) for v in power_values) > settings["threshold_w"]:
        _reject(store, f"battery power exceeded idle threshold ({max(abs(v) for v in power_values):.1f} W)", now)
        return _model(store, cfg, now)

    soc_values = _numeric_history_values(hist.get(soc_entity, []), sample_start, now)
    current_soc = base.numeric_state(client.state_optional(soc_entity))
    if current_soc is not None:
        soc_values.append(float(current_soc))
    if not soc_values:
        _reject(store, "battery SOC history unavailable", now)
        return _model(store, cfg, now)
    soc_delta = max(soc_values) - min(soc_values)
    if soc_delta > settings["soc_delta_pct"]:
        _reject(store, f"battery SOC moved {soc_delta:.3f}%", now)
        return _model(store, cfg, now)

    if solar_entity:
        pv_kwh = base.diff_cumulative(hist.get(solar_entity, []), sample_start, now)
        if pv_kwh is None:
            _reject(store, "PV history unavailable", now)
            return _model(store, cfg, now)
        pv_w = max(0.0, float(pv_kwh)) * 1000.0 / (SAMPLE_WINDOW_MINUTES / 60.0)
        if pv_w > settings["threshold_w"]:
            _reject(store, f"PV not negligible ({pv_w:.1f} W average)", now)
            return _model(store, cfg, now)

    for key, label in (
        ("battery_charge_energy_total_kwh", "charge"),
        ("battery_discharge_energy_total_kwh", "discharge"),
    ):
        entity = str(battery.get(key) or "").strip()
        if not entity:
            continue
        delta = base.diff_cumulative(hist.get(entity, []), sample_start, now)
        if delta is not None and float(delta) > ENERGY_ACTIVITY_EPSILON_KWH:
            _reject(store, f"battery {label} energy moved {float(delta):.3f} kWh", now)
            return _model(store, cfg, now)

    sample_w = statistics.median(abs(v) for v in power_values)
    store.db.execute(
        "INSERT OR REPLACE INTO battery_idle_samples(observed_at,watts,soc,pause_age_minutes) VALUES(?,?,?,?)",
        (now.astimezone(timezone.utc).isoformat(), float(sample_w), float(statistics.median(soc_values)), float(pause_age)),
    )
    store.db.commit()
    store.meta_set("battery_idle_last_accepted", now.isoformat())
    base.LOG.info("Battery idle sample accepted: %.1f W (PauseBoth %.1f min, SOC delta %.3f%%)", sample_w, pause_age, soc_delta)
    return _model(store, cfg, now)


def publish_idle_loss(client, model: dict[str, Any]) -> None:
    learned = model.get("learned_w")
    state: Any = round(float(learned), 1) if learned is not None else "unknown"
    attrs = dict(model)
    attrs.update({
        "friendly_name": "Home Energy Manager Battery Standby Loss",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:battery-clock-outline",
    })
    client.publish(IDLE_ENTITY, state, attrs)


def simulate_battery_with_idle_loss(slot, batt, soc_kwh, charge_eff, discharge_eff):
    """Apply learned standby as an internal stored-energy loss when otherwise idle."""
    soc, battery_kwh, import_kwh, export_kwh = _original_simulate_battery(
        slot, batt, soc_kwh, charge_eff, discharge_eff
    )
    if _active_standby_w <= 0.0 or abs(battery_kwh) > FLOW_EPSILON_KWH:
        return soc, battery_kwh, import_kwh, export_kwh
    hours = max(0.0, float(slot.get("duration_h") or 0.0))
    standby_kwh = _active_standby_w / 1000.0 * hours
    reserve_kwh = float(batt["capacity"]) * float(batt["reserve"]) / 100.0
    soc = max(reserve_kwh, soc - standby_kwh)
    return soc, battery_kwh, import_kwh, export_kwh


def make_forecast_with_idle_learning(client, store, cfg, now):
    global _active_standby_w, _last_model
    learner_error: str | None = None
    try:
        _last_model = learn_idle_loss(client, store, cfg, now)
        _active_standby_w = float(_last_model.get("learned_w") or 0.0) if _last_model.get("ready") else 0.0
        publish_idle_loss(client, _last_model)
    except Exception as exc:
        _active_standby_w = 0.0
        learner_error = str(exc)
        base.LOG.warning("Battery idle standby learning unavailable; forecast fallback is 0 W: %s", exc)
        _last_model = {
            "learned_w": None,
            "sample_count": 0,
            "readiness": "error",
            "ready": False,
            "forecast_applied": False,
            "error": learner_error,
        }

    payload, reasons = _wrapped_make_forecast(client, store, cfg, now)
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if isinstance(attrs, dict):
        attrs["battery_idle_loss"] = dict(_last_model)
        attrs["battery_idle_loss"]["forecast_standby_w"] = round(_active_standby_w, 1)
    if learner_error:
        reasons.append(f"Battery idle standby learning unavailable: {learner_error}")
    return payload, reasons


base.simulate_battery = simulate_battery_with_idle_loss
base.make_forecast = make_forecast_with_idle_learning

if __name__ == "__main__":
    base.main()
