#!/usr/bin/env python3
"""Normalize the HACS Axle VPP integration for Home Energy Manager.

This component never calls Axle directly.  The HACS integration owns all Axle API
communication/authentication; this process only reads its Home Assistant entities
and publishes one stable package-owned sensor for the controller.
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

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options_axle.json"))
VERSION = os.environ.get("HOME_ENERGY_MANAGER_VERSION", "0.1.43")
LOG = logging.getLogger("home_energy_axle")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")

AXLE_SOURCE_ENTITIES = {
    "start": "sensor.axle_vpp_axle_start_time",
    "end": "sensor.axle_vpp_axle_end_time",
    "type": "sensor.axle_vpp_axle_import_export",
    "window": "sensor.axle_vpp_axle_event_window_state",
    "updated": "sensor.axle_vpp_axle_updated_at",
}


def parse_dt(value):
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def clean_state(item):
    if not isinstance(item, dict):
        return None
    value = item.get("state")
    if value in (None, "", "unknown", "unavailable"):
        return None
    return str(value)


class AxleAdapter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN missing")
        self.base = "http://supervisor/core/api"
        self.session = None
        self.stop_event = asyncio.Event()
        self.mqtt = MQTTPublisher("axle", "Home Energy Manager – Axle", "Axle Event Adapter", VERSION, self.token)

    async def open(self):
        self.session = aiohttp.ClientSession(headers={"Authorization": "Bearer " + self.token})

    async def close(self):
        self.mqtt.close()
        if self.session:
            await self.session.close()

    async def state(self, entity_id: str):
        if not entity_id or not self.session:
            return None
        try:
            async with self.session.get(f"{self.base}/states/{entity_id}", timeout=15) as response:
                return await response.json() if response.status == 200 else None
        except Exception as exc:
            LOG.debug("HACS Axle entity read failed for %s: %s", entity_id, exc)
            return None

    async def publish_rest(self, entity_id: str, state: str, attrs: dict):
        async with self.session.post(f"{self.base}/states/{entity_id}", json={"state": state, "attributes": attrs}, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Axle status publish HTTP {response.status}")

    async def update(self):
        output = str(self.cfg.get("publish_entity", "sensor.home_energy_manager_axle"))
        enabled = bool(self.cfg.get("enabled", True))
        ids = dict(AXLE_SOURCE_ENTITIES)
        now = datetime.now(timezone.utc)

        if not enabled:
            attrs = {"friendly_name": "Home Energy Manager Axle", "version": VERSION, "source": "hacs_axle_vpp", "source_available": False, "event_available": False, "active": False, "last_status_update": now.isoformat()}
            if not self.mqtt.publish_sensor(output, "disabled", attrs):
                await self.publish_rest(output, "disabled", attrs)
            return

        raw = {key: await self.state(entity) for key, entity in ids.items()}
        source_available = any(item is not None for item in raw.values())
        start = parse_dt(clean_state(raw["start"]))
        end = parse_dt(clean_state(raw["end"]))
        event_type_raw = (clean_state(raw["type"]) or "").strip().lower()
        event_type = "export" if event_type_raw == "export" else "import" if event_type_raw == "import" else None
        source_window = clean_state(raw["window"])
        source_updated_at = clean_state(raw["updated"])
        valid_window = bool(start and end and end > start and event_type)
        active = bool(valid_window and start <= now < end)
        upcoming = bool(valid_window and now < start)
        finished = bool(valid_window and now >= end)
        event_available = bool(valid_window and (active or upcoming))

        if not source_available:
            state = "unavailable"
        elif active:
            state = f"{event_type}_active"
        elif upcoming:
            state = f"{event_type}_upcoming"
        elif finished:
            state = "finished"
        else:
            state = "idle"

        attrs = {
            "friendly_name": "Home Energy Manager Axle",
            "version": VERSION,
            "source": "hacs_axle_vpp",
            "source_available": source_available,
            "event_available": event_available,
            "event_type": event_type,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "active": active,
            "upcoming": upcoming,
            "duration_minutes": round((end - start).total_seconds() / 60, 1) if valid_window else None,
            "minutes_to_start": round((start - now).total_seconds() / 60, 1) if upcoming else 0 if active else None,
            "minutes_remaining": round((end - now).total_seconds() / 60, 1) if active else None,
            "source_window_state": source_window,
            "source_updated_at": source_updated_at,
            "source_entities": ids,
            "last_status_update": now.isoformat(),
        }
        if not self.mqtt.publish_sensor(output, state, attrs):
            await self.publish_rest(output, state, attrs)

    async def run(self):
        await self.open()
        LOG.info("Axle adapter started; source=HACS Axle VPP integration")
        try:
            while not self.stop_event.is_set():
                try:
                    await self.update()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("Axle adapter update failed: %s", exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=max(10, int(self.cfg.get("poll_seconds", 30))))
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()


def load_config():
    raw = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid Axle configuration")
    return raw


async def amain():
    app = AxleAdapter(load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, app.stop_event.set)
        except NotImplementedError:
            pass
    await app.run()


if __name__ == "__main__":
    asyncio.run(amain())
