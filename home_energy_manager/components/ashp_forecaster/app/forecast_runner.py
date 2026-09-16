#!/usr/bin/env python3
"""Production ASHP forecaster entrypoint with guaranteed configured horizon.

The learned thermal DHW forecast is the only DHW production path. If its horizon is
unavailable or invalid, CH still publishes from a degraded run while DHW is marked
explicitly unavailable rather than replaced by a fabricated or legacy value.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import main as legacy
from active_dd_training import build_training as build_active_dd_training
from common.forecast_slots import production_slot_start
from dhw_production_selector import SelectionResult, select_dhw_forecast
from weather_observations import (
    complete_pending_actuals,
    ensure_schema,
    raw_forecast_scores,
    record_forecast_snapshot,
    shadow_weather_calibration,
)

LOG = logging.getLogger("ashp_forecast")
SOURCE_ENTITY = "sensor.ashp_dhw_production_source"
HEALTH_ENTITY = "sensor.ashp_forecast_health"
WEATHER_SCORE_ENTITY = "sensor.ashp_weather_raw_mae"
THERMAL_REFRESH_WAIT_SECONDS = 8.0
THERMAL_REFRESH_POLL_SECONDS = 0.25


class DHWProductionUnavailable(RuntimeError):
    """Raised when authoritative thermal DHW cannot provide the production horizon."""


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


def _select_dhw_with_refresh_wait(client: legacy.HAClient, store: legacy.Store, cfg, starts) -> SelectionResult:
    max_age_minutes = max(25.0, float(cfg.update_minutes) * 2.0)
    deadline = time.monotonic() + THERMAL_REFRESH_WAIT_SECONDS
    last_error: RuntimeError | None = None
    last_signature: str | None = None
    attempts = 0
    while True:
        attempts += 1
        try:
            result = select_dhw_forecast(store.db, starts, client.get_state, max_age_minutes=max_age_minutes)
            if last_error is not None:
                LOG.info(
                    "DHW thermal alignment recovered: attempts=%d requested_start=%s requested_end=%s selected_slots=%d",
                    attempts,
                    starts[0].isoformat() if starts and hasattr(starts[0], "isoformat") else (str(starts[0]) if starts else "none"),
                    starts[-1].isoformat() if starts and hasattr(starts[-1], "isoformat") else (str(starts[-1]) if starts else "none"),
                    len(result.values),
                )
            else:
                LOG.info(
                    "DHW thermal alignment OK: requested_start=%s requested_end=%s selected_slots=%d",
                    starts[0].isoformat() if starts and hasattr(starts[0], "isoformat") else (str(starts[0]) if starts else "none"),
                    starts[-1].isoformat() if starts and hasattr(starts[-1], "isoformat") else (str(starts[-1]) if starts else "none"),
                    len(result.values),
                )
            return result
        except RuntimeError as exc:
            last_error = exc
            signature = str(exc)
            if signature != last_signature:
                LOG.warning("DHW thermal alignment wait: attempt=%d error=%s", attempts, signature)
                last_signature = signature
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.error("DHW thermal alignment wait expired: attempts=%d last_error=%s", attempts, last_signature or "unknown")
                raise DHWProductionUnavailable(
                    f"DHW thermal production forecast unavailable after {THERMAL_REFRESH_WAIT_SECONDS:.1f}s; "
                    f"attempts={attempts}; last_error={last_signature or 'unknown'}"
                ) from exc
            time.sleep(min(THERMAL_REFRESH_POLL_SECONDS, remaining))


def _publish_dhw_source(client: legacy.HAClient, source: str, reason: str, *, thermal_selected: bool) -> None:
    try:
        client.set_sensor(
            SOURCE_ENTITY,
            source,
            {
                "friendly_name": "ASHP DHW Production Forecast Source",
                "source": source,
                "reason": reason,
                "thermal_selected": thermal_selected,
                "fallback_available": False,
                "last_updated": datetime.now(client.tz).isoformat() if hasattr(client, "tz") else datetime.now().isoformat(),
            },
        )
    except Exception as exc:
        LOG.debug("Could not publish DHW production source diagnostic: %s", exc)


def _install_dhw_selector(client: legacy.HAClient, store: legacy.Store) -> None:
    last_logged: tuple[str, str] | None = None

    def thermal_builder(client_arg, store_arg, cfg_arg, starts):
        del client_arg, store_arg
        nonlocal last_logged
        try:
            result = _select_dhw_with_refresh_wait(client, store, cfg_arg, starts)
        except DHWProductionUnavailable as exc:
            reason = str(exc)
            marker = ("unavailable", reason)
            if marker != last_logged:
                LOG.error("DHW production forecast source=unavailable reason=%s", reason)
                last_logged = marker
            _publish_dhw_source(client, "unavailable", reason, thermal_selected=False)
            raise
        marker = (result.source, result.reason)
        if marker != last_logged:
            LOG.info("DHW production forecast source=%s reason=%s", result.source, result.reason)
            last_logged = marker
        _publish_dhw_source(client, result.source, result.reason, thermal_selected=True)
        return result.values

    legacy.build_dhw_forecast = thermal_builder
    # main.py still contains the pre-thermal historical DHW learner for migration
    # compatibility, but it is no longer part of runtime production.
    legacy.build_dhw_training = lambda *args, **kwargs: 0


def _build_ch_only_forecast(
    client: legacy.HAClient,
    store: legacy.Store,
    cfg,
    coefficient: float,
    tz: ZoneInfo,
) -> list[dict]:
    """Reuse the canonical forecast path while publishing no DHW claim.

    A zero-valued builder is installed only for the duration of the internal CH
    calculation so the existing CH maths runs without DHW priority suppression.
    Those placeholder DHW values are never published: every returned row is
    rewritten with ``dhw_kwh=None`` and ``energy_kwh=None``.
    """
    production_dhw_builder = legacy.build_dhw_forecast
    legacy.build_dhw_forecast = lambda client_arg, store_arg, cfg_arg, starts: [0.0] * len(starts)
    try:
        forecast = legacy.build_forecast(client, store, cfg, coefficient, tz)
    finally:
        legacy.build_dhw_forecast = production_dhw_builder

    for row in forecast:
        row["dhw_active"] = None
        row["dhw_kwh"] = None
        row["energy_kwh"] = None
        row["dhw_status"] = "unavailable"
        row["ch_priority_applied"] = False
    return forecast


def _publish_health(client: legacy.HAClient, state: str, *, ch_status: str, dhw_status: str, reason: str = "") -> None:
    try:
        client.set_sensor(
            HEALTH_ENTITY,
            state,
            {
                "friendly_name": "ASHP Forecast Health",
                "ch_status": ch_status,
                "dhw_status": dhw_status,
                "reason": reason,
                "last_updated": datetime.now(client.tz).isoformat() if hasattr(client, "tz") else datetime.now().isoformat(),
            },
        )
    except Exception as exc:
        LOG.debug("Could not publish ASHP forecast health diagnostic: %s", exc)


def _publish_degraded_ch_forecast(
    client: legacy.HAClient,
    store: legacy.Store,
    cfg,
    tz: ZoneInfo,
    reason: str,
) -> None:
    cfg = legacy.resolve_dynamic_thresholds(client, cfg)
    store.ensure_training_signature(cfg)
    coefficient, training_days, training_slots = legacy.build_training(client, store, cfg, tz)
    forecast = _build_ch_only_forecast(client, store, cfg, coefficient, tz)
    now = datetime.now(tz)
    next_48_end = now + timedelta(hours=48)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    def ch_sum(end: datetime) -> float:
        return sum(float(slot["ch_kwh"]) for slot in forecast if legacy.parse_dt(slot["start"]) < end)

    common = {
        "model": "heating_degree_days",
        "base_temperature_c": cfg.base_temperature_c,
        "winter_mode_below_c": cfg.winter_mode_below_c,
        "summer_mode_above_c": cfg.summer_mode_above_c,
        "kwh_per_degree_day": round(coefficient, 4),
        "training_days_used": training_days,
        "training_slots_used": training_slots,
        "aggregate_status": "degraded",
        "ch_status": "fresh",
        "dhw_status": "unavailable",
        "dhw_reason": reason,
        "dhw_fallback_used": False,
        "last_updated": now.isoformat(),
    }

    def unknown_energy_sensor(entity: str, name: str, extra: dict | None = None) -> None:
        attrs = {
            "friendly_name": name,
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            **common,
        }
        if extra:
            attrs.update(extra)
        client.set_sensor(entity, "unknown", attrs)

    unknown_energy_sensor("sensor.ashp_forecast_next_30m", "ASHP Forecast Next 30m")
    unknown_energy_sensor("sensor.ashp_forecast_remaining_today", "ASHP Forecast Remaining Today")
    unknown_energy_sensor("sensor.ashp_forecast_next_24h", "ASHP Forecast Next 24h", {"forecast": forecast})
    unknown_energy_sensor("sensor.ashp_forecast_next_48h", "ASHP Forecast Next 48h", {"forecast": forecast})

    client.set_sensor(
        "sensor.ashp_forecast_ch_next_48h",
        round(ch_sum(next_48_end), 3),
        {
            "friendly_name": "ASHP CH Forecast Next 48h",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            **common,
            "forecast": forecast,
        },
    )
    unknown_energy_sensor("sensor.ashp_forecast_dhw_next_48h", "ASHP DHW Forecast Next 48h")
    _publish_health(client, "degraded", ch_status="fresh", dhw_status="unavailable", reason=reason)
    LOG.warning(
        "ASHP forecast published degraded: CH fresh, DHW unavailable, forecast_slots=%d, ch_today=%.2f kWh, reason=%s",
        len(forecast), ch_sum(midnight), reason,
    )


def _fmt_metric(value) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"


def _publish_weather_scores(client: HorizonHAClient, store: legacy.Store, tz: ZoneInfo) -> None:
    try:
        now = datetime.now(tz)
        scores = raw_forecast_scores(store.db)
        calibration = shadow_weather_calibration(store.db, now=now)
        overall = scores["overall"]
        state = overall["mae_c"] if overall["mae_c"] is not None else 0.0
        client.set_sensor(
            WEATHER_SCORE_ENTITY,
            round(float(state), 3),
            {
                "friendly_name": "ASHP Weather Raw Forecast MAE",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "phase": "shadow_bias_learning",
                "samples": overall["samples"],
                "mean_bias_c": overall["mean_bias_c"],
                "mae_c": overall["mae_c"],
                "rmse_c": overall["rmse_c"],
                "horizons": scores["horizons"],
                "max_horizon_hours": scores["max_horizon_hours"],
                "error_definition": "actual_minus_forecast",
                "calibration_applied": False,
                "calibration_window_days": calibration["window_days"],
                "calibration_samples": calibration["samples"],
                "calibration_oldest_sample_at": calibration["oldest_sample_at"],
                "calibration_newest_sample_at": calibration["newest_sample_at"],
                "calibration_min_global_samples": calibration["min_global_samples"],
                "calibration_min_horizon_samples": calibration["min_horizon_samples"],
                "calibration_max_abs_bias_c": calibration["max_abs_bias_c"],
                "global_median_bias_c": calibration["global_median_bias_c"],
                "global_bias_usable": calibration["global_bias_usable"],
                "global_shadow_correction_c": calibration["global_correction_c"],
                "raw_window_mae_c": calibration["raw"]["mae_c"],
                "shadow_corrected_mae_c": calibration["shadow_corrected"]["mae_c"],
                "shadow_improvement_pct": calibration["improvement_pct"],
                "shadow_horizons": calibration["horizons"],
                "last_updated": now.isoformat(),
            },
        )
        if overall["samples"]:
            LOG.info(
                "Weather raw baseline: samples=%d bias=%.3fC MAE=%.3fC RMSE=%.3fC",
                overall["samples"], overall["mean_bias_c"], overall["mae_c"], overall["rmse_c"],
            )
        LOG.info(
            "Weather bias shadow: window=%dd samples=%d oldest=%s newest=%s global_median=%sC usable=%s correction=%sC raw_MAE=%sC shadow_MAE=%sC improvement=%s%% calibration_applied=false",
            calibration["window_days"],
            calibration["samples"],
            calibration["oldest_sample_at"] or "none",
            calibration["newest_sample_at"] or "none",
            _fmt_metric(calibration["global_median_bias_c"]),
            calibration["global_bias_usable"],
            _fmt_metric(calibration["global_correction_c"]),
            _fmt_metric(calibration["raw"]["mae_c"]),
            _fmt_metric(calibration["shadow_corrected"]["mae_c"]),
            _fmt_metric(calibration["improvement_pct"]),
        )
        horizon_log = []
        for name, result in calibration["horizons"].items():
            horizon_log.append(
                f"{name}:n={result['samples']},median={_fmt_metric(result['median_bias_c'])}C,"
                f"correction={_fmt_metric(result['correction_c'])}C,source={result['correction_source']},"
                f"rawMAE={_fmt_metric(result['raw_mae_c'])}C,shadowMAE={_fmt_metric(result['shadow_mae_c'])}C"
            )
        LOG.info("Weather bias shadow horizons: %s", "; ".join(horizon_log))
    except Exception as exc:
        LOG.warning("Could not publish weather forecast bias diagnostics: %s", exc)


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
    legacy.ceil_time = production_slot_start
    LOG.info("ASHP Energy Forecaster starting in timezone %s", tz.key)
    LOG.info("CH=%s temperature=%s weather=%s", cfg.ch_energy_entity, cfg.outdoor_temperature_entity, cfg.weather_entity)
    while True:
        try:
            _complete_weather_observations(client, store, cfg, tz)
            try:
                legacy.run_once(client, store, cfg, tz)
            except DHWProductionUnavailable as exc:
                _publish_degraded_ch_forecast(client, store, cfg, tz, str(exc))
            else:
                _publish_health(client, "healthy", ch_status="fresh", dhw_status="fresh")
        except Exception as exc:
            _publish_health(client, "error", ch_status="error", dhw_status="unknown", reason=str(exc))
            LOG.exception("Forecast update failed")
        time.sleep(cfg.update_minutes * 60)


if __name__ == "__main__":
    main()
