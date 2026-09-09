#!/usr/bin/env python3
"""Passive Home Energy Controller for Forecast Only mode.

This process intentionally contains no Home Assistant service-call code and no
inverter write method. It only reads the forecast sensor and publishes controller
status. The active controller (app.py) is never started when this mode is selected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from common.mqtt import MQTTPublisher

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options_controller.json"))
VERSION = os.environ.get("HOME_ENERGY_MANAGER_VERSION", "0.1.23")
LOG = logging.getLogger("home_energy_controller_forecast_only")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)


class PassiveController:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN missing")
        self.base = "http://supervisor/core/api"
        self.session: aiohttp.ClientSession | None = None
        self.stop_event = asyncio.Event()
        self.mqtt = MQTTPublisher(
            "controller",
            "Home Energy Manager – Controller",
            "Home Energy Controller",
            VERSION,
            self.token,
        )

    async def open(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + self.token}
        )

    async def close(self) -> None:
        self.mqtt.close()
        if self.session:
            await self.session.close()

    async def state(self, entity_id: str) -> dict | None:
        if not entity_id or not self.session:
            return None
        try:
            async with self.session.get(
                f"{self.base}/states/{entity_id}", timeout=15
            ) as response:
                return await response.json() if response.status == 200 else None
        except Exception as exc:
            LOG.warning("Forecast read failed for %s: %s", entity_id, exc)
            return None

    async def publish_rest(self, entity_id: str, state: str, attrs: dict) -> None:
        if not self.session:
            return
        async with self.session.post(
            f"{self.base}/states/{entity_id}",
            json={"state": state, "attributes": attrs},
            timeout=15,
        ) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"status publish HTTP {response.status}")

    async def publish_status(self) -> None:
        forecast_entity = str(
            self.cfg.get("home_energy_forecast_entity", "sensor.home_energy_forecast")
        )
        status_entity = str(
            self.cfg.get("status_entity_id", "sensor.home_energy_controller")
        )
        forecast = await self.state(forecast_entity)
        fattrs = forecast.get("attributes", {}) if isinstance(forecast, dict) else {}
        controller_inputs = fattrs.get("controller_inputs")
        generated_at = fattrs.get("forecast_generated_at")
        sequence = fattrs.get("forecast_sequence")

        attrs = {
            "friendly_name": "Home Energy Controller",
            "version": VERSION,
            "operation_mode": "forecast_only",
            "control_enabled": False,
            "inverter_writes_enabled": False,
            "active_controller_running": False,
            "forecast_entity": forecast_entity,
            "forecast_available": bool(forecast),
            "controller_inputs_ready": isinstance(controller_inputs, dict),
            "source_forecast_sequence": sequence,
            "forecast_generated_at": generated_at,
            "last_status_update": datetime.now(timezone.utc).isoformat(),
            "message": (
                "Forecast Only is active. Forecasting continues, but Home Energy "
                "Manager will not modify any inverter or battery setting."
            ),
        }

        if self.mqtt.publish_sensor(status_entity, "forecast_only", attrs):
            return
        await self.publish_rest(status_entity, "forecast_only", attrs)

    async def run(self) -> None:
        await self.open()
        LOG.info(
            "Forecast Only controller started. Active controller disabled; "
            "no inverter-write implementation is loaded."
        )
        try:
            while not self.stop_event.is_set():
                try:
                    await self.publish_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("Forecast Only status update failed: %s", exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()


def load_config() -> dict:
    raw = json.loads(OPTIONS_PATH.read_text())
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid controller configuration")
    if str(raw.get("operation_mode", "")) != "forecast_only":
        raise RuntimeError(
            "Passive controller may only run when operation_mode=forecast_only"
        )
    return raw


async def amain() -> None:
    controller = PassiveController(load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, controller.stop_event.set)
        except NotImplementedError:
            pass
    await controller.run()


if __name__ == "__main__":
    asyncio.run(amain())
