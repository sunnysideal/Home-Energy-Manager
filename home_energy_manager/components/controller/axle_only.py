#!/usr/bin/env python3
"""Axle-only controller.

Passive like Forecast Only except when a HACS-backed Axle Export event requires
preparation or is active.  It does not run the normal optimisation planner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from common.mqtt import MQTTPublisher

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options_controller.json"))
VERSION = os.environ.get("HOME_ENERGY_MANAGER_VERSION", "0.1.43")
LOG = logging.getLogger("home_energy_controller_axle_only")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")


def number(item):
    try:
        value = float(item.get("state")) if item else None
        return value if value is not None and math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def time_value(dt):
    return dt.astimezone().strftime("%H:%M:%S")


class AxleOnlyController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN missing")
        self.base = "http://supervisor/core/api"
        self.session = None
        self.stop_event = asyncio.Event()
        self.mqtt = MQTTPublisher("controller", "Home Energy Manager – Controller", "Home Energy Controller", VERSION, self.token)
        self.snapshot = None
        self.event_key = None

    async def open(self):
        self.session = aiohttp.ClientSession(headers={"Authorization": "Bearer " + self.token})

    async def close(self):
        self.mqtt.close()
        if self.session:
            await self.session.close()

    async def state(self, entity_id):
        if not entity_id:
            return None
        try:
            async with self.session.get(f"{self.base}/states/{entity_id}", timeout=15) as response:
                return await response.json() if response.status == 200 else None
        except Exception:
            return None

    async def write(self, entity_id, value):
        domain = entity_id.split(".", 1)[0]
        data = {"entity_id": entity_id}
        if domain == "switch":
            service = "turn_on" if str(value).lower() in {"on", "true", "1"} else "turn_off"
        elif domain in {"number", "input_number"}:
            service = "set_value"; data["value"] = float(value)
        elif domain in {"select", "input_select"}:
            service = "select_option"; data["option"] = str(value)
        elif domain == "time":
            service = "set_value"; data["time"] = str(value)
        elif domain == "input_datetime":
            service = "set_datetime"; data["time"] = str(value)
        else:
            raise ValueError(f"Unsupported writable entity domain {domain}")
        async with self.session.post(f"{self.base}/services/{domain}/{service}", json=data, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"{domain}.{service} HTTP {response.status}")

    async def publish_rest(self, entity_id, state, attrs):
        async with self.session.post(f"{self.base}/states/{entity_id}", json={"state": state, "attributes": attrs}, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"status publish HTTP {response.status}")

    def controller_entities(self, forecast):
        attrs = forecast.get("attributes", {}) if isinstance(forecast, dict) else {}
        ci = attrs.get("controller_inputs") if isinstance(attrs.get("controller_inputs"), dict) else {}
        ents = ci.get("entities") if isinstance(ci.get("entities"), dict) else {}
        return ents, ci

    async def capture_snapshot(self, ents):
        keys = ("eco_mode", "charge_schedule_enable", "discharge_schedule_enable", "charge_slot_1_start", "charge_slot_1_end", "charge_slot_1_target", "charge_rate", "discharge_slot_1_start", "discharge_slot_1_end", "discharge_slot_1_target", "discharge_rate", "pause_mode", "pause_start", "pause_end")
        snap = {}
        for key in keys:
            entity = str(ents.get(key) or "")
            item = await self.state(entity)
            if entity and item:
                snap[key] = {"entity": entity, "state": item.get("state")}
        self.snapshot = snap

    async def restore_snapshot(self):
        if not self.snapshot:
            return
        for key, item in self.snapshot.items():
            try:
                await self.write(item["entity"], item["state"])
            except Exception as exc:
                LOG.warning("Axle restore failed for %s: %s", key, exc)
        self.snapshot = None
        self.event_key = None

    async def set_if_needed(self, entity, value):
        if not entity:
            raise RuntimeError("Required inverter entity missing")
        current = await self.state(entity)
        if current is not None and str(current.get("state", "")).strip().lower() == str(value).strip().lower():
            return
        await self.write(entity, value)

    async def cycle(self):
        now = datetime.now(timezone.utc)
        axle_entity = str(self.cfg.get("axle_entity", "sensor.home_energy_manager_axle"))
        forecast_entity = str(self.cfg.get("home_energy_forecast_entity", "sensor.home_energy_forecast"))
        status_entity = str(self.cfg.get("status_entity_id", "sensor.home_energy_controller"))
        axle = await self.state(axle_entity)
        forecast = await self.state(forecast_entity)
        a = axle.get("attributes", {}) if isinstance(axle, dict) else {}
        ents, ci = self.controller_entities(forecast)
        event_type = str(a.get("event_type") or "").lower()
        start, end = parse_dt(a.get("start")), parse_dt(a.get("end"))
        event_available = bool(a.get("event_available")) and event_type == "export" and start and end and end > start
        active = bool(event_available and start <= now < end)
        upcoming = bool(event_available and now < start)
        key = f"{start.isoformat()}|{end.isoformat()}" if event_available else None

        soc = number(await self.state(str(ents.get("battery_soc") or "")))
        capacity = number(await self.state(str(ents.get("battery_capacity") or "")))
        reserve = number(await self.state(str(ents.get("battery_reserve") or "")))
        max_discharge = number(await self.state(str(ents.get("inverter_max_discharge_rate") or "")))
        max_charge = number(await self.state(str(ents.get("inverter_max_charge_rate") or "")))
        discharge_eff = float(self.cfg.get("axle_discharge_efficiency", 0.95))
        required_soc = None
        full_event_possible = None
        action = "passive"
        message = "Axle Only is passive; no qualifying Axle Export event is active or requires preparation."

        if not event_available:
            if self.snapshot:
                await self.restore_snapshot()
        elif None in (soc, capacity, reserve, max_discharge, max_charge) or capacity <= 0:
            action = "blocked"
            message = "Axle Export event detected but required battery/controller inputs are unavailable."
        else:
            duration_h = (end - start).total_seconds() / 3600
            required_kwh = (max_discharge / 1000.0) * duration_h / max(0.5, discharge_eff)
            required_soc = min(100.0, reserve + required_kwh / capacity * 100.0)
            full_event_possible = reserve + required_kwh / capacity * 100.0 <= 100.0
            if self.event_key and self.event_key != key:
                await self.restore_snapshot()
            if not self.snapshot:
                await self.capture_snapshot(ents)
                self.event_key = key

            if active:
                action = "axle_export"
                message = "Axle Export event active: battery forced to maximum discharge rate down to configured reserve."
                await self.set_if_needed(str(ents.get("charge_schedule_enable") or ""), "off")
                await self.set_if_needed(str(ents.get("discharge_slot_1_start") or ""), time_value(start))
                await self.set_if_needed(str(ents.get("discharge_slot_1_end") or ""), time_value(end))
                await self.set_if_needed(str(ents.get("discharge_slot_1_target") or ""), reserve)
                await self.set_if_needed(str(ents.get("discharge_rate") or ""), max_discharge)
                await self.set_if_needed(str(ents.get("discharge_schedule_enable") or ""), "on")
                if ents.get("eco_mode"):
                    await self.set_if_needed(str(ents.get("eco_mode")), "on")
            elif upcoming and soc + 0.5 < required_soc:
                remaining_h = max((start - now).total_seconds() / 3600, 1 / 60)
                shortfall_kwh = capacity * (required_soc - soc) / 100.0
                required_charge_w = min(max_charge, max(500.0, shortfall_kwh / remaining_h * 1000.0 / 0.95))
                action = "axle_prepare"
                message = "Preparing battery for Axle Export event; charging only the SOC shortfall required for full-rate discharge."
                charge_end = min(start, now + timedelta(hours=shortfall_kwh / max(required_charge_w / 1000.0 * 0.95, 0.1)) + timedelta(minutes=2))
                await self.set_if_needed(str(ents.get("discharge_schedule_enable") or ""), "off")
                await self.set_if_needed(str(ents.get("charge_slot_1_start") or ""), time_value(now))
                await self.set_if_needed(str(ents.get("charge_slot_1_end") or ""), time_value(charge_end))
                await self.set_if_needed(str(ents.get("charge_slot_1_target") or ""), math.ceil(required_soc))
                await self.set_if_needed(str(ents.get("charge_rate") or ""), round(required_charge_w))
                await self.set_if_needed(str(ents.get("charge_schedule_enable") or ""), "on")
            else:
                action = "axle_ready"
                message = "Battery already has enough stored energy for full-rate discharge throughout the Axle Export event."

        attrs = {
            "friendly_name": "Home Energy Controller", "version": VERSION,
            "operation_mode": "axle_only", "control_enabled": action in {"axle_prepare", "axle_export"},
            "inverter_writes_enabled": action in {"axle_prepare", "axle_export"},
            "normal_optimisation_enabled": False, "axle_entity": axle_entity,
            "axle_event_type": event_type or None, "axle_event_start": start.isoformat() if start else None,
            "axle_event_end": end.isoformat() if end else None, "axle_event_active": active,
            "axle_action": action, "battery_soc": soc, "required_event_start_soc": round(required_soc, 1) if required_soc is not None else None,
            "full_event_possible": full_event_possible, "max_discharge_rate_w": max_discharge,
            "controller_inputs_version": ci.get("version"), "last_status_update": now.isoformat(), "message": message,
        }
        if not self.mqtt.publish_sensor(status_entity, action, attrs):
            await self.publish_rest(status_entity, action, attrs)

    async def run(self):
        await self.open()
        LOG.info("Axle Only controller started; normal optimisation planner is not loaded")
        try:
            while not self.stop_event.is_set():
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("Axle Only cycle failed: %s", exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=max(10, int(self.cfg.get("sample_interval_seconds", 30))))
                except asyncio.TimeoutError:
                    pass
        finally:
            try:
                await self.restore_snapshot()
            finally:
                await self.close()


def load_config():
    raw = json.loads(OPTIONS_PATH.read_text())
    if not isinstance(raw, dict) or str(raw.get("operation_mode", "")) != "axle_only":
        raise RuntimeError("Axle-only controller may only run when operation_mode=axle_only")
    return raw


async def amain():
    controller = AxleOnlyController(load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, controller.stop_event.set)
        except NotImplementedError:
            pass
    await controller.run()


if __name__ == "__main__":
    asyncio.run(amain())
