#!/usr/bin/env python3
"""Shadow battery charge-model learner for migration issue #50.

The Controller remains authoritative for production charge planning.  This
runtime independently observes the inverter charge schedule and SOC history,
learns the same physical SOC-band representation, and publishes the v1 battery
model contract for comparison.  It never writes inverter settings and the
forecast does not consume this model yet.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import health_runtime as health
from battery_model_contract import (
    BATTERY_MODEL_ENTITY,
    GENERIC_FACTORS,
    SOC_BANDS,
    build_battery_model_attributes,
)

base = health.base

SAMPLE_WINDOW_MINUTES = 5
MAX_HISTORY_DAYS = 365

_wrapped_make_forecast = base.make_forecast


def _ensure_schema(store) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS battery_charge_observations (
            observed_at TEXT NOT NULL,
            band_lo REAL NOT NULL,
            band_hi REAL NOT NULL,
            coverage REAL NOT NULL,
            effective_factor REAL NOT NULL,
            requested_rate_w REAL NOT NULL,
            PRIMARY KEY(observed_at, band_lo, band_hi)
        );
        CREATE TABLE IF NOT EXISTS battery_charge_bands (
            band_lo REAL NOT NULL,
            band_hi REAL NOT NULL,
            learned_factor REAL,
            confidence REAL NOT NULL DEFAULT 0,
            full_equiv REAL NOT NULL DEFAULT 0,
            obs_count INTEGER NOT NULL DEFAULT 0,
            p10 REAL,
            p90 REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(band_lo, band_hi)
        );
        """
    )
    store.db.commit()


def _weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return pairs[-1][0]
    threshold = total * q
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= threshold:
            return value
    return pairs[-1][0]


def _recency_weight(observed_at: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    return math.exp(-math.log(2.0) * age_days / 30.0)


def _active_charge_window(client, cfg, now: datetime) -> tuple[datetime, datetime, float] | None:
    battery = cfg.section("battery")
    enabled = base.bool_state(client.state_optional(str(battery.get("charge_schedule_enabled") or "")), False)
    if not enabled:
        return None
    rate = base.numeric_state(client.state_optional(str(battery.get("charge_rate") or "")))
    if rate is None or rate <= 0:
        return None
    for index in (1, 2):
        start_state = client.state_optional(str(battery.get(f"charge_start_{index}") or ""))
        end_state = client.state_optional(str(battery.get(f"charge_end_{index}") or ""))
        start_s = str(start_state.get("state", "00:00")) if start_state else "00:00"
        end_s = str(end_state.get("state", "00:00")) if end_state else "00:00"
        if not base.in_daily_window(now, start_s, end_s):
            continue
        start = base.parse_hhmm(start_s)
        end = base.parse_hhmm(end_s)
        if not start or not end:
            continue
        window_start = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        window_end = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
        if window_end <= window_start:
            if now < window_end:
                window_start -= timedelta(days=1)
            else:
                window_end += timedelta(days=1)
        return window_start, window_end, float(rate)
    return None


def _soc_points(states: list[dict[str, Any]], start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    initial = base.latest_before(states, start)
    if initial is not None:
        out.append((start, float(initial)))
    for item in states:
        stamp = item.get("last_changed") or item.get("last_updated")
        try:
            when = base.parse_dt(stamp)
            value = float(item.get("state"))
        except (TypeError, ValueError, OverflowError):
            continue
        if start < when <= end:
            out.append((when, value))
    out.sort(key=lambda item: item[0])
    return out


def _derive_observations(points: list[tuple[datetime, float]], rate_w: float, capacity_kwh: float) -> list[dict[str, float | str]]:
    observations: list[dict[str, float | str]] = []
    if capacity_kwh <= 0 or rate_w <= 0:
        return observations
    for lo, hi in SOC_BANDS:
        band_points = [point for point in points if lo <= point[1] <= hi]
        if len(band_points) < 2:
            continue
        t0, s0 = band_points[0]
        t1, s1 = band_points[-1]
        delta_soc = max(0.0, s1 - s0)
        coverage = min(1.0, max(0.0, delta_soc / (hi - lo)))
        hours = max(1e-6, (t1 - t0).total_seconds() / 3600.0)
        if coverage <= 0:
            continue
        effective_kw = capacity_kwh * (delta_soc / 100.0) / hours
        factor = min(1.5, max(0.05, effective_kw / (rate_w / 1000.0)))
        observations.append({
            "observed_at": t1.astimezone(timezone.utc).isoformat(),
            "band_lo": lo,
            "band_hi": hi,
            "coverage": coverage,
            "effective_factor": factor,
            "requested_rate_w": rate_w,
        })
    return observations


def _learn_bands(store, now: datetime) -> None:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=MAX_HISTORY_DAYS)
    store.db.execute("DELETE FROM battery_charge_observations WHERE observed_at < ?", (cutoff.isoformat(),))
    for lo, hi in SOC_BANDS:
        rows = store.db.execute(
            "SELECT observed_at,coverage,effective_factor FROM battery_charge_observations "
            "WHERE band_lo=? AND band_hi=? ORDER BY observed_at",
            (lo, hi),
        ).fetchall()
        if not rows:
            continue
        values: list[float] = []
        weights: list[float] = []
        coverage = 0.0
        for row in rows:
            observed = base.parse_dt(row["observed_at"]).astimezone(timezone.utc)
            cov = float(row["coverage"])
            values.append(float(row["effective_factor"]))
            weights.append(_recency_weight(observed, now.astimezone(timezone.utc)) * cov)
            coverage += cov
        p10 = p90 = None
        clipped = list(values)
        if len(values) >= 5:
            p10 = _weighted_quantile(values, weights, .1)
            p90 = _weighted_quantile(values, weights, .9)
            clipped = [min(p90, max(p10, value)) for value in values]
        learned = sum(value * weight for value, weight in zip(clipped, weights)) / sum(weights)
        confidence = min(1.0, coverage / 10.0)
        old = store.db.execute(
            "SELECT confidence FROM battery_charge_bands WHERE band_lo=? AND band_hi=?", (lo, hi)
        ).fetchone()
        if old:
            confidence = max(confidence, float(old["confidence"] or 0.0))
        store.db.execute(
            "INSERT INTO battery_charge_bands(band_lo,band_hi,learned_factor,confidence,full_equiv,obs_count,p10,p90,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(band_lo,band_hi) DO UPDATE SET "
            "learned_factor=excluded.learned_factor,confidence=excluded.confidence,full_equiv=excluded.full_equiv,"
            "obs_count=excluded.obs_count,p10=excluded.p10,p90=excluded.p90,updated_at=excluded.updated_at",
            (lo, hi, learned, confidence, coverage, len(values), p10, p90, now.astimezone(timezone.utc).isoformat()),
        )
    store.db.commit()


def _observe(client, store, cfg, now: datetime) -> None:
    window = _active_charge_window(client, cfg, now)
    if window is None:
        return
    window_start, _, rate_w = window
    sample_start = max(window_start, now - timedelta(minutes=SAMPLE_WINDOW_MINUTES))
    battery = cfg.section("battery")
    soc_entity = str(battery.get("soc") or "").strip()
    capacity_entity = str(battery.get("capacity_kwh") or "").strip()
    if not soc_entity or not capacity_entity:
        return
    capacity = base.numeric_state(client.state_optional(capacity_entity))
    if capacity is None or capacity <= 0:
        return
    hist = client.history([soc_entity], sample_start - timedelta(minutes=5), now, timeout=45)
    points = _soc_points(hist.get(soc_entity, []), sample_start, now)
    current_soc = base.numeric_state(client.state_optional(soc_entity))
    if current_soc is not None:
        points.append((now, float(current_soc)))
    for obs in _derive_observations(points, rate_w, float(capacity)):
        store.db.execute(
            "INSERT OR IGNORE INTO battery_charge_observations(observed_at,band_lo,band_hi,coverage,effective_factor,requested_rate_w) "
            "VALUES(?,?,?,?,?,?)",
            (obs["observed_at"], obs["band_lo"], obs["band_hi"], obs["coverage"], obs["effective_factor"], obs["requested_rate_w"]),
        )
    store.db.commit()
    _learn_bands(store, now)


def _top_completion(store, cfg) -> tuple[float, dict[str, Any]]:
    generic = float(cfg.section("controller").get("generic_dwell_minutes", 15) or 15)
    raw = store.meta_get("battery_model_top_completion_allowance_minutes")
    try:
        learned = float(raw) if raw is not None else generic
    except (TypeError, ValueError):
        learned = generic
    return max(generic, learned), {
        "attempts": int(store.meta_get("battery_model_top_completion_attempts", "0") or 0),
        "successes": int(store.meta_get("battery_model_top_completion_successes", "0") or 0),
        "misses": int(store.meta_get("battery_model_top_completion_misses", "0") or 0),
        "migration_state": store.meta_get("battery_model_migration_state", "awaiting_seed"),
    }


def _model_attributes(store, cfg, now: datetime) -> dict[str, Any]:
    bands: list[dict[str, Any]] = []
    for lo, hi in SOC_BANDS:
        row = store.db.execute(
            "SELECT * FROM battery_charge_bands WHERE band_lo=? AND band_hi=?", (lo, hi)
        ).fetchone()
        learned = float(row["learned_factor"]) if row and row["learned_factor"] is not None else None
        confidence = float(row["confidence"] or 0.0) if row else 0.0
        generic = GENERIC_FACTORS[(lo, hi)]
        effective = generic * (1.0 - confidence) + learned * confidence if learned is not None else generic
        bands.append({
            "soc_lo": lo, "soc_hi": hi, "generic_factor": generic,
            "learned_factor": learned, "confidence": confidence,
            "effective_factor": effective,
            "samples": int(row["obs_count"] or 0) if row else 0,
            "full_equiv": float(row["full_equiv"] or 0.0) if row else 0.0,
            "p10": float(row["p10"]) if row and row["p10"] is not None else None,
            "p90": float(row["p90"]) if row and row["p90"] is not None else None,
            "updated_at": row["updated_at"] if row else None,
        })
    top, top_diag = _top_completion(store, cfg)
    attrs = build_battery_model_attributes(
        generated_at=now.astimezone(timezone.utc).isoformat(),
        bands=bands,
        top_completion_allowance_minutes=top,
        generic_top_completion_minutes=float(cfg.section("controller").get("generic_dwell_minutes", 15) or 15),
        diagnostics={
            "mode": "shadow",
            "controller_consumes_model": False,
            "forecast_consumes_model": False,
            "observation_count": sum(int(b["samples"]) for b in bands),
            "learned_band_count": sum(1 for b in bands if b["learned_factor"] is not None),
            "top_completion": top_diag,
        },
    )
    attrs.update({
        "friendly_name": "Home Energy Manager Battery Model",
        "icon": "mdi:battery-sync-outline",
    })
    return attrs


def publish_shadow_model(client, store, cfg, now: datetime) -> dict[str, Any]:
    _ensure_schema(store)
    _observe(client, store, cfg, now)
    attrs = _model_attributes(store, cfg, now)
    client.publish(BATTERY_MODEL_ENTITY, "shadow", attrs)
    return attrs


def make_forecast_with_shadow_battery_model(client, store, cfg, now):
    try:
        publish_shadow_model(client, store, cfg, now)
    except Exception as exc:
        base.LOG.warning("Shadow battery model unavailable; production behaviour unchanged: %s", exc)
    return _wrapped_make_forecast(client, store, cfg, now)


base.make_forecast = make_forecast_with_shadow_battery_model

if __name__ == "__main__":
    base.main()
