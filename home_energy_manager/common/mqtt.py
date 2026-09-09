from __future__ import annotations

import json
import logging
import os
import ssl
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("home_energy_manager.mqtt")

# These are package API entities, not implementation-detail entities.  Their MQTT
# identity must therefore survive component renames/reorganisation.  Earlier builds
# included the publishing component in unique_id, which allowed Home Assistant to
# retain long generated entity IDs such as
# sensor.home_energy_manager_home_forecaster_home_energy_forecast even after
# default_entity_id was introduced.
PERMANENT_SENSOR_UNIQUE_IDS = {
    "sensor.home_energy_forecast": "home_energy_manager_home_energy_forecast",
    "sensor.home_energy_forecast_health": "home_energy_manager_home_energy_forecast_health",
    "sensor.home_energy_controller": "home_energy_manager_home_energy_controller",
    "sensor.ashp_forecast_next_48h": "home_energy_manager_ashp_forecast_next_48h",
}


class MQTTPublisher:
    """Small HA MQTT Discovery publisher.

    This module is transport/presentation infrastructure only. It contains no
    forecasting or control policy.
    """

    def __init__(
        self,
        component: str,
        device_name: str,
        model: str,
        sw_version: str,
        ha_token: str,
    ) -> None:
        self.component = component
        self.device_name = device_name
        self.model = model
        self.sw_version = sw_version
        self.ha_token = ha_token
        self.cfg = self._config()
        self.enabled = bool(self.cfg.get("enabled", True))
        self.available = False
        self._client = None
        self._discovered: set[str] = set()
        self._legacy_removed: set[str] = set()
        self._legacy_discovery_removed: set[str] = set()
        self.discovery_prefix = str(self.cfg.get("discovery_prefix") or "homeassistant").strip("/")
        self.topic_prefix = str(self.cfg.get("topic_prefix") or "home_energy_manager").strip("/")
        self.availability_topic = f"{self.topic_prefix}/{self.component}/availability"
        if self.enabled:
            self._connect()

    @staticmethod
    def _config() -> dict[str, Any]:
        try:
            raw = os.environ.get("HOME_ENERGY_MQTT_CONFIG", "{}")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _supervisor_mqtt(self) -> dict[str, Any]:
        if not self.ha_token:
            return {}
        req = Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {self.ha_token}"},
        )
        try:
            with urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode() or "{}")
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            LOG.warning("%s: MQTT Supervisor service discovery unavailable: %s", self.component, exc)
            return {}

    def _connect(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except Exception as exc:
            LOG.warning("%s: MQTT disabled for this run: paho-mqtt unavailable: %s", self.component, exc)
            return

        discovered = self._supervisor_mqtt() if bool(self.cfg.get("auto_discover_broker", True)) else {}

        host = str(self.cfg.get("host") or discovered.get("host") or "").strip()
        port = int(self.cfg.get("port") or discovered.get("port") or 1883)
        username = str(self.cfg.get("username") or discovered.get("username") or "").strip()
        password = str(self.cfg.get("password") or discovered.get("password") or "")
        use_ssl = bool(
            self.cfg.get("ssl", discovered.get("ssl", discovered.get("tls", False)))
        )

        if not host:
            LOG.warning("%s: MQTT enabled but no broker was discovered/configured; using REST publishing for this run", self.component)
            return

        connected = threading.Event()
        connection_error: list[str] = []

        def on_connect(client, userdata, flags, reason_code, *args):
            try:
                rc = int(reason_code)
            except Exception:
                rc = 0 if str(reason_code).lower() in {"success", "0"} else 1
            if rc == 0:
                self.available = True
                connected.set()
            else:
                connection_error.append(str(reason_code))
                connected.set()

        client_id = f"home_energy_manager_{self.component}_{os.getpid()}"
        try:
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            except Exception:
                client = mqtt.Client(client_id=client_id)
            client.on_connect = on_connect
            if username:
                client.username_pw_set(username, password)
            if use_ssl:
                client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            client.will_set(self.availability_topic, "offline", qos=1, retain=True)
            client.connect(host, port, keepalive=45)
            client.loop_start()
            if not connected.wait(8):
                raise RuntimeError("broker connection timed out")
            if not self.available:
                raise RuntimeError("broker rejected connection: " + ", ".join(connection_error))
            self._client = client
            self._publish_raw(self.availability_topic, "online", retain=True)
            LOG.info("%s: MQTT connected to %s:%d; HA Discovery prefix=%s", self.component, host, port, self.discovery_prefix)
        except Exception as exc:
            self.available = False
            try:
                client.loop_stop()
            except Exception:
                pass
            LOG.warning("%s: MQTT connection failed; using REST publishing for this run: %s", self.component, exc)

    def close(self) -> None:
        if not self._client:
            return
        try:
            self._publish_raw(self.availability_topic, "offline", retain=True)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:
            pass
        self.available = False

    def _publish_raw(self, topic: str, payload: str, retain: bool = True) -> None:
        if not self._client:
            raise RuntimeError("MQTT client not connected")
        info = self._client.publish(topic, payload=payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        if getattr(info, "rc", 0) != 0:
            raise RuntimeError(f"MQTT publish failed rc={info.rc} topic={topic}")

    def _delete_legacy_rest_state(self, entity_id: str) -> None:
        if entity_id in self._legacy_removed:
            return
        self._legacy_removed.add(entity_id)
        if not bool(self.cfg.get("migrate_legacy_states", True)) or not self.ha_token:
            return
        req = Request(
            f"http://supervisor/core/api/states/{quote(entity_id, safe='._')}",
            headers={"Authorization": f"Bearer {self.ha_token}"},
            method="DELETE",
        )
        try:
            with urlopen(req, timeout=5):
                pass
            LOG.info("%s: removed legacy REST state %s before MQTT discovery", self.component, entity_id)
        except HTTPError as exc:
            if exc.code != 404:
                LOG.debug("%s: legacy state cleanup for %s returned HTTP %s", self.component, entity_id, exc.code)
        except Exception as exc:
            LOG.debug("%s: legacy state cleanup for %s skipped: %s", self.component, entity_id, exc)

    def _discovery_identity(self, entity_id: str, object_id: str) -> tuple[str, str, str | None]:
        """Return unique_id, discovery topic, and obsolete discovery topic if any."""
        permanent = PERMANENT_SENSOR_UNIQUE_IDS.get(entity_id)
        legacy_topic = f"{self.discovery_prefix}/sensor/home_energy_manager_{self.component}/{object_id}/config"
        if permanent:
            # Home Assistant recommends object_id=unique_id with no node_id.  More
            # importantly, this identity no longer changes when code moves between
            # Home Energy Manager components.
            topic = f"{self.discovery_prefix}/sensor/{permanent}/config"
            return permanent, topic, legacy_topic
        return f"home_energy_manager_{self.component}_{object_id}", legacy_topic, None

    def _remove_legacy_discovery(self, entity_id: str, legacy_topic: str | None, new_topic: str) -> None:
        if not legacy_topic or legacy_topic == new_topic or entity_id in self._legacy_discovery_removed:
            return
        self._legacy_discovery_removed.add(entity_id)
        if not bool(self.cfg.get("migrate_legacy_states", True)):
            return
        # An empty retained discovery payload removes the old MQTT component. This
        # lets the new permanent unique_id claim the requested default_entity_id.
        self._publish_raw(legacy_topic, "", retain=True)
        LOG.info("%s: removed obsolete MQTT discovery identity for %s", self.component, entity_id)

    def publish_sensor(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> bool:
        """Publish a sensor through HA MQTT Discovery.

        Returns True when MQTT was used; False tells the caller to use its
        existing Home Assistant REST state publishing path.
        """
        if not self.available or not self._client:
            return False
        if not entity_id.startswith("sensor."):
            return False

        object_id = entity_id.split(".", 1)[1]
        state_topic = f"{self.topic_prefix}/{self.component}/{object_id}/state"
        attrs_topic = f"{self.topic_prefix}/{self.component}/{object_id}/attributes"
        unique_id, discovery_topic, legacy_discovery_topic = self._discovery_identity(entity_id, object_id)

        if entity_id not in self._discovered:
            self._delete_legacy_rest_state(entity_id)
            self._remove_legacy_discovery(entity_id, legacy_discovery_topic, discovery_topic)
            name = str(attributes.get("friendly_name") or object_id.replace("_", " ").title())
            payload: dict[str, Any] = {
                "name": name,
                "unique_id": unique_id,
                "default_entity_id": entity_id,
                "state_topic": state_topic,
                "json_attributes_topic": attrs_topic,
                "availability_topic": self.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [f"home_energy_manager_{self.component}"],
                    "name": self.device_name,
                    "manufacturer": "Home Energy Manager",
                    "model": self.model,
                    "sw_version": self.sw_version,
                },
                "origin": {
                    "name": "Home Energy Manager",
                    "sw_version": self.sw_version,
                },
            }
            for source_key, discovery_key in (
                ("unit_of_measurement", "unit_of_measurement"),
                ("device_class", "device_class"),
                ("state_class", "state_class"),
                ("icon", "icon"),
                ("entity_category", "entity_category"),
            ):
                value = attributes.get(source_key)
                if value not in (None, ""):
                    payload[discovery_key] = value

            self._publish_raw(discovery_topic, json.dumps(payload, separators=(",", ":"), default=str), retain=True)
            self._discovered.add(entity_id)

        self._publish_raw(state_topic, str(state), retain=True)
        self._publish_raw(
            attrs_topic,
            json.dumps(attributes, separators=(",", ":"), default=str),
            retain=True,
        )
        return True
