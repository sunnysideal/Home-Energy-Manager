#!/usr/bin/env python3
"""Production ASHP forecaster entrypoint with guaranteed configured horizon.

The learned thermal DHW forecast is authoritative for production. Legacy DHW remains
available internally as a validation comparator, but is never used as a production
fallback. If the thermal horizon is unavailable or invalid, the forecast run fails
rather than publishing a contradictory DHW estimate.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import main as legacy
from active_dd_training import build_training as build_active_dd_training
from dhw_production_selector import SelectionResult, select_dhw_forecast
from weather_observations import (
    complete_pending_actuals,
    ensure_schema,
    raw_forecast_scores,
    record_forecast_snapshot,
)

LOG = logging.getLogger("ashp_forecast")
SOURCE_ENTITY = "sensor.ashp_dhw_production_source"
WEATHER_SCORE_ENTITY = "sensor.ashp_weather_raw_mae"
THERMAL_REFRESH_WAIT_SECONDS = 8.0
THERMAL_REFRESH_POLL_SECONDS = 0.25


def _fallback_timezone_name() -> str:
    candidate = str(os.environ.get("TZ") or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        LOG.warning("Invalid TZ environment value %r; falling back to UTC", candidate)
        return "UTC"
    return candidate


def _timezone_name(client: legacy.HAClient) -> str:
    fallback = _fallback_timezone_name()
    try:
        ha_cfg = client.get_config()
    except Exception as exc:
        LOG.warning("Could not read Home Assistant timezone during startup; using add-on timezone %s: %s", fallback, exc)
        return fallback
    candidate = str((ha_cfg or {}).get("time_zone") or fallback).strip() or fallback
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        LOG.warning("Home Assistant returned invalid timezone %r; using %s", candidate, fallback)
        return fallback
    return candidate


class HorizonHAClient(legacy.HAClient):
    def __init__(self, token: str, *, forecast_hours: int, timezone_name: str):
        super().__init__(token)
        self.forecast_hours = max(1, int(forecast_hours))
        self.tz = ZoneInfo(timezone_name)
        self.weather_observation_db = None

    def get_hourly_weather(self, entity_id: str):
        raw = super().get_hourly_weather(entity_id)
        parsed: list[tuple[datetime, float]] = []
        for row in raw:
            try:
                parsed.append((legacy.parse_dt(str(row["datetime"])).astimezone(self.tz), float(row["temperature"])))
            except (KeyError, TypeError, ValueError):
                continue
        observation_db = getattr(self, "weather_observation_db", None)
        if observation_db is not None and parsed:
            try:
                stored = record_forecast_snapshot(observation_db, issued_at=datetime.now(self.tz), source_entity=entity_id, points=parsed)
                if stored:
                    LOG.debug("Stored %d provider weather forecast observation rows", stored)
            except Exception as exc:
                LOG.warning("Could not store weather forecast observation snapshot: %s", exc)
        if len(parsed) < 2:
            return raw
        parsed.sort(key=lambda item: item[0])
        last_dt, last_temp = parsed[-1]
        required_end = datetime.now(self.tz) + timedelta(hours=self.forecast_hours + 2)
        if last_dt >= required_end:
            return raw
        extended = list(raw)
        cursor = last_dt
        appended = 0
        while cursor < required_end:
            cursor += timedelta(hours=1)
            extended.append({"datetime": cursor.isoformat(), "temperature": last_temp})
            appended += 1
        LOG.info("Weather horizon extended by %d hourly points using last temperature %.1f C to guarantee %dh ASHP forecast", appended, last_temp, self.forecast_hours)
        return extended


def _select_dhw_with_refresh_wait(client: legacy.HAClient, store: legacy.Store, cfg, starts, legacy_values: list[float]) -> SelectionResult:
    max_age_minutes = max(25.0, float(cfg.update_minutes) * 2.0)
    deadline = time.monotonic() + THERMAL_REFRESH_WAIT_SECONDS
    last_error: RuntimeError | None = None
    while True:
        try:
            result = select_dhw_forecast(store.db, starts, legacy_values, client.get_state, max_age_minutes=max_age_minutes)
            if last_error is not None:
                LOG.info("DHW thermal forecast arrived during refresh wait; using authoritative thermal horizon")
            return result
        except RuntimeError as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"DHW thermal production forecast unavailable after {THERMAL_REFRESH_WAIT_SECONDS:.1f}s; no fallback is permitted") from exc
            time.sleep(min(THERMAL_REFRESH_POLL_SECONDS, remaining))


def _install_dhw_selector(client: legacy.HAClient, store: legacy.Store) -> None:
    legacy_builder = legacy.build_dhw_forecast
    last_logged: tuple[str, str] | None = None
    def selected_builder(client_arg, store_arg, cfg_arg, starts):
        nonlocal last_logged
        legacy_values = legacy_builder(client_arg, store_arg, cfg_arg, starts)
        result = _select_dhw_with_refresh_wait(client, store, cfg_arg, starts, legacy_values)
        marker = (result.source, result.reason)
        if marker != last_logged:
            LOG.info("DHW production forecast source=%s reason=%s", result.source, result.reason)
            last_logged = marker
        try:
            client.set_sensor(SOURCE_ENTITY, result.source, {"friendly_name": "ASHP DHW Production Forecast Source", "source": result.source, "reason": result.reason, "thermal_selected": True, "fallback_allowed": False, "last_updated": datetime.now(client.tz).isoformat() if hasattr(client, "tz") else datetime.now().isoformat()})
        except Exception as exc:
            LOG.debug("Could not publish DHW production source diagnostic: %s", exc)
        return result.values
    legacy.build_dhw_forecast = selected_builder


def _publish_weather_scores(client: HorizonHAClient, store: legacy.Store, tz: ZoneInfo) -> None:
    """Publish Phase 2 raw-weather baseline diagnostics; never alter forecasting."""
    try:
        scores = raw_forecast_scores(store.db)
        overall = scores["overall"]
        state = overall["mae_c"] if overall["mae_c"] is not None else 0.0
        client.set_sensor(
            WEATHER_SCORE_ENTITY,
            round(float(state), 3),
            {
                "friendly_name": "ASHP Weather Raw Forecast MAE",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "phase": "baseline_observation_only",
                "samples": overall["samples"],
                "mean_bias_c": overall["mean_bias_c"],
                "mae_c": overall["mae_c"],
                "rmse_c": overall["rmse_c"],
                "horizons": scores["horizons"],
                "max_horizon_hours": scores["max_horizon_hours"],
                "error_definition": "actual_minus_forecast",
                "calibration_applied": False,
                "last_updated": datetime.now(tz).isoformat(),
            },
        )
        if overall["samples"]:
            LOG.info(
                "Weather raw baseline: samples=%d bias=%.3fC MAE=%.3fC RMSE=%.3fC",
                overall["samples"], overall["mean_bias_c"], overall["mae_c"], overall["rmse_c"],
            )
    except Exception as exc:
        LOG.warning("Could not publish raw weather forecast baseline diagnostics: %s", exc)


def _complete_weather_observations(client: HorizonHAClient, store: legacy.Store, cfg, tz: ZoneInfo) -> None:
    try:
        completed, unmatched = complete_pending_actuals(store.db, get_history=client.get_history, actual_entity=cfg.outdoor_temperature_entity, now=datetime.now(tz))
    except Exception as exc:
        LOG.warning("Could not complete weather forecast observations: %s", exc)
        return
    if completed or unmatched:
        LOG.debug("Weather observation backfill: completed_rows=%d unmatched_targets=%d", completed, unmatched)
    _publish_weather_scores(client, store, tz)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = legacy.Config.load()
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    client = HorizonHAClient(token, forecast_hours=cfg.forecast_hours, timezone_name=_fallback_timezone_name())
    timezone_name = _timezone_name(client)
    client.tz = ZoneInfo(timezone_name)
    tz = client.tz
    store = legacy.Store(legacy.DB_PATH)
    ensure_schema(store.db)
    client.weather_observation_db = store.db
    _install_dhw_selector(client, store)
    legacy.build_training = build_active_dd_training
    LOG.info("ASHP Energy Forecaster starting in timezone %s", tz.key)
    LOG.info("CH=%s temperature=%s weather=%s", cfg.ch_energy_entity, cfg.outdoor_temperature_entity, cfg.weather_entity)
    while True:
        try:
            _complete_weather_observations(client, store, cfg, tz)
            legacy.run_once(client, store, cfg, tz)
        except Exception:
            LOG.exception("Forecast update failed")
        time.sleep(cfg.update_minutes * 60)


if __name__ == "__main__":
    main()
