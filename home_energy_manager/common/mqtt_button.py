"""Generic Home Assistant MQTT command-button infrastructure.

This module is transport/presentation only. Callers own the meaning and policy of
button commands. Commands are deliberately non-retained so reconnect/restart
cannot replay an old press.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

LOG = logging.getLogger("home_energy_manager.mqtt_button")


def publish_command_button(publisher, entity_id: str, name: str, icon: str, on_press: Callable[[], None]) -> bool:
    """Discover a package-owned HA button and subscribe to its command topic."""
    if not getattr(publisher, "available", False) or not getattr(publisher, "_client", None):
        return False
    if not entity_id.startswith("button."):
        raise ValueError("command button entity_id must start with button.")

    object_id = entity_id.split(".", 1)[1]
    unique_id = object_id
    discovery_topic = f"{publisher.discovery_prefix}/button/{unique_id}/config"
    command_topic = f"{publisher.topic_prefix}/{publisher.component}/{object_id}/command"
    registration_key = f"button:{entity_id}"
    if registration_key in publisher._discovered:
        return True

    def _message(_client, _userdata, message):
        payload = message.payload.decode(errors="replace").strip()
        if payload != "PRESS":
            return
        try:
            on_press()
        except Exception:
            LOG.exception("%s: command callback failed for %s", publisher.component, entity_id)

    payload = {
        "name": name,
        "unique_id": unique_id,
        "default_entity_id": entity_id,
        "command_topic": command_topic,
        "payload_press": "PRESS",
        "availability_topic": publisher.availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "icon": icon,
        "device": {
            "identifiers": [f"home_energy_manager_{publisher.component}"],
            "name": publisher.device_name,
            "manufacturer": "Home Energy Manager",
            "model": publisher.model,
            "sw_version": publisher.sw_version,
        },
        "origin": {"name": "Home Energy Manager", "sw_version": publisher.sw_version},
    }
    publisher._client.message_callback_add(command_topic, _message)
    publisher._client.subscribe(command_topic, qos=1)
    publisher._publish_raw(discovery_topic, json.dumps(payload, separators=(",", ":")), retain=True)
    publisher._discovered.add(registration_key)
    LOG.info("%s: MQTT command button discovered as %s", publisher.component, entity_id)
    return True
