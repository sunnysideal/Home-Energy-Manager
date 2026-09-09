#!/usr/bin/env python3
"""Production ASHP forecaster entrypoint with guaranteed configured horizon.

The legacy forecaster remains authoritative.  This adapter only extends an otherwise
valid hourly weather series when the provider stops before the configured forecast
horizon.  Extension uses the provider's final temperature as a persistence fallback;
all CH/DHW modelling and the published output contract remain in ``main.py``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import main as legacy

LOG = logging.getLogger("ashp_forecast")


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
        # Give build_forecast() at least one weather point beyond requested_end so
        # its existing min(requested_end, available_end) produces requested_end.
        required_end = datetime.now(self.tz) + timedelta(hours=self.forecast_hours + 2)
        if last_dt >= required_end:
            return raw

        extended = list(raw)
        cursor = last_dt + timedelta(hours=1)
        appended = 0
        while cursor <= required_end:
            extended.append({"datetime": cursor.isoformat(), "temperature": last_temp})
            cursor += timedelta(hours=1)
            appended += 1
        LOG.info(
            "Weather horizon extended by %d hourly points using last temperature %.1f C "
            "to guarantee %dh ASHP forecast",
            appended,
            last_temp,
            self.forecast_hours,
        )
        return extended


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = legacy.Config.load()

    # Read HA timezone with a normal client first, then use the horizon-preserving
    # subclass for the production forecast loop.
    bootstrap = legacy.HAClient(os.environ.get("SUPERVISOR_TOKEN", ""))
    ha_cfg = bootstrap.get_config()
    timezone_name = str(ha_cfg.get("time_zone") or "UTC")
    try:
        bootstrap.mqtt.close()
    except Exception:
        pass

    tz = ZoneInfo(timezone_name)
    client = HorizonHAClient(
        os.environ.get("SUPERVISOR_TOKEN", ""),
        forecast_hours=cfg.forecast_hours,
        timezone_name=timezone_name,
    )
    store = legacy.Store(legacy.DB_PATH)

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
