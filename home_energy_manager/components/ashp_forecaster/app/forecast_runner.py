#!/usr/bin/env python3
"""Production ASHP forecaster entrypoint with guaranteed configured horizon.

This adapter preserves the legacy ASHP forecast contract while adding two production
safety functions: weather-horizon extension and guarded DHW source selection. The
thermal DHW model is only selected after its independent validation process persists
promotion readiness and a fresh, complete thermal forecast is available. Every failure
condition falls back to legacy DHW before CH priority/total-energy calculations run.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import main as legacy
from dhw_production_selector import SelectionResult, select_dhw_forecast

LOG = logging.getLogger("ashp_forecast")
SOURCE_ENTITY = "sensor.ashp_dhw_production_source"
THERMAL_REFRESH_WAIT_SECONDS = 8.0
THERMAL_REFRESH_POLL_SECONDS = 0.25
TRANSIENT_THERMAL_REASONS = {
    "thermal_entity_unavailable",
    "thermal_entity_read_failed",
    "thermal_forecast_not_published",
    "thermal_horizon_incomplete",
    "thermal_forecast_missing_timestamp",
    "thermal_forecast_stale",
    "thermal_forecast_incomplete",
}


def _fallback_timezone_name() -> str:
    """Return the add-on timezone without requiring the HA API to be available."""
    candidate = str(os.environ.get("TZ") or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        LOG.warning("Invalid TZ environment value %r; falling back to UTC", candidate)
        return "UTC"
    return candidate


def _timezone_name(client: legacy.HAClient) -> str:
    """Prefer HA's configured timezone, but never fail startup if /config is unavailable."""
    fallback = _fallback_timezone_name()
    try:
        ha_cfg = client.get_config()
    except Exception as exc:
        LOG.warning(
            "Could not read Home Assistant timezone during startup; using add-on timezone %s: %s",
            fallback,
            exc,
        )
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

    def get_hourly_weather(self, entity_id: str):
        raw = super().get_hourly_weather(entity_id)
        parsed: list[tuple[datetime, float]] = []
        for row in raw:
            try:
                parsed.append((legacy.parse_dt(str(row["datetime"])).astimezone(self.tz), float(row["temperature"])))
            except (KeyError, TypeError, ValueError):
                continue
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
        # Append whole hourly points until the final point is at or beyond the
        # required horizon.  Using ``cursor <= required_end`` before appending can
        # stop up to 59 minutes short when ``required_end`` is not hour-aligned.
        while cursor < required_end:
            cursor += timedelta(hours=1)
            extended.append({"datetime": cursor.isoformat(), "temperature": last_temp})
            appended += 1
        LOG.info(
            "Weather horizon extended by %d hourly points using last temperature %.1f C "
            "to guarantee %dh ASHP forecast",
            appended,
            last_temp,
            self.forecast_hours,
        )
        return extended


def _select_dhw_with_refresh_wait(
    client: legacy.HAClient,
    store: legacy.Store,
    cfg,
    starts,
    legacy_values: list[float],
) -> SelectionResult:
    """Select DHW, briefly waiting out the shadow-publisher race when appropriate.

    The thermal publisher is a sibling process within the ASHP component. Both wake on
    the same configured cadence, but process scheduling can let production read HA a few
    seconds before the freshly aligned 96-slot thermal horizon is published. Only
    transient publication/alignment failures are retried here. Structural readiness,
    quality fallback and all other selector decisions remain immediate and unchanged.
    """
    max_age_minutes = max(25.0, float(cfg.update_minutes) * 2.0)

    def select() -> SelectionResult:
        return select_dhw_forecast(
            store.db,
            starts,
            legacy_values,
            client.get_state,
            max_age_minutes=max_age_minutes,
        )

    result = select()
    if result.source == "thermal" or result.reason not in TRANSIENT_THERMAL_REASONS:
        return result

    first_reason = result.reason
    deadline = time.monotonic() + THERMAL_REFRESH_WAIT_SECONDS
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(THERMAL_REFRESH_POLL_SECONDS, remaining))
        result = select()
        if result.source == "thermal" or result.reason not in TRANSIENT_THERMAL_REASONS:
            break

    if result.source == "thermal":
        LOG.info(
            "DHW thermal forecast arrived after transient %s; using fresh aligned thermal horizon",
            first_reason,
        )
    elif result.reason in TRANSIENT_THERMAL_REASONS:
        LOG.info(
            "DHW thermal forecast still unavailable after %.1fs (%s); using legacy fallback",
            THERMAL_REFRESH_WAIT_SECONDS,
            result.reason,
        )
    return result


def _install_dhw_selector(client: legacy.HAClient, store: legacy.Store) -> None:
    """Wrap the legacy DHW builder with a fail-safe production selector.

    Selection occurs before ``legacy.build_forecast`` applies DHW priority to CH, so the
    public 30-minute slot contract remains internally consistent whichever source wins.
    """
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
            client.set_sensor(
                SOURCE_ENTITY,
                result.source,
                {
                    "friendly_name": "ASHP DHW Production Forecast Source",
                    "source": result.source,
                    "reason": result.reason,
                    "thermal_selected": result.source == "thermal",
                    "last_updated": datetime.now(client.tz).isoformat() if hasattr(client, "tz") else datetime.now().isoformat(),
                },
            )
        except Exception as exc:
            LOG.debug("Could not publish DHW production source diagnostic: %s", exc)
        return result.values

    legacy.build_dhw_forecast = selected_builder


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = legacy.Config.load()
    token = os.environ.get("SUPERVISOR_TOKEN", "")

    client = HorizonHAClient(
        token,
        forecast_hours=cfg.forecast_hours,
        timezone_name=_fallback_timezone_name(),
    )
    timezone_name = _timezone_name(client)
    client.tz = ZoneInfo(timezone_name)
    tz = client.tz
    store = legacy.Store(legacy.DB_PATH)
    _install_dhw_selector(client, store)

    LOG.info("ASHP Energy Forecaster starting in timezone %s", tz.key)
    LOG.info("CH=%s temperature=%s weather=%s", cfg.ch_energy_entity, cfg.outdoor_temperature_entity, cfg.weather_entity)

    while True:
        try:
            legacy.run_once(client, store, cfg, tz)
        except Exception:
            LOG.exception("Forecast update failed")
        time.sleep(cfg.update_minutes * 60)


if __name__ == "__main__":
    main()
