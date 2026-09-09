import json

from common.mqtt import MQTTPublisher, PERMANENT_SENSOR_UNIQUE_IDS


def publisher(component="home_forecaster"):
    obj = object.__new__(MQTTPublisher)
    obj.component = component
    obj.device_name = "Test Device"
    obj.model = "Test Model"
    obj.sw_version = "0.1.37"
    obj.ha_token = ""
    obj.cfg = {"migrate_legacy_states": True}
    obj.enabled = True
    obj.available = True
    obj._client = object()
    obj._discovered = set()
    obj._legacy_removed = set()
    obj._legacy_discovery_removed = set()
    obj.discovery_prefix = "homeassistant"
    obj.topic_prefix = "home_energy_manager"
    obj.availability_topic = f"home_energy_manager/{component}/availability"
    return obj


def test_package_api_entities_have_component_independent_unique_ids():
    expected = {
        "sensor.home_energy_forecast": "home_energy_manager_home_energy_forecast",
        "sensor.home_energy_forecast_health": "home_energy_manager_home_energy_forecast_health",
        "sensor.home_energy_controller": "home_energy_manager_home_energy_controller",
        "sensor.ashp_forecast_next_48h": "home_energy_manager_ashp_forecast_next_48h",
    }
    assert PERMANENT_SENSOR_UNIQUE_IDS == expected


def test_permanent_entity_removes_old_discovery_and_publishes_new_identity(monkeypatch):
    pub = publisher()
    writes = []
    monkeypatch.setattr(pub, "_delete_legacy_rest_state", lambda entity_id: None)
    monkeypatch.setattr(pub, "_publish_raw", lambda topic, payload, retain=True: writes.append((topic, payload, retain)))

    assert pub.publish_sensor(
        "sensor.home_energy_forecast",
        1,
        {"friendly_name": "Home Energy Forecast"},
    ) is True

    old_topic = "homeassistant/sensor/home_energy_manager_home_forecaster/home_energy_forecast/config"
    new_topic = "homeassistant/sensor/home_energy_manager_home_energy_forecast/config"
    assert writes[0] == (old_topic, "", True)
    config_write = next(item for item in writes if item[0] == new_topic)
    payload = json.loads(config_write[1])
    assert payload["unique_id"] == "home_energy_manager_home_energy_forecast"
    assert payload["default_entity_id"] == "sensor.home_energy_forecast"


def test_non_api_sensor_keeps_existing_component_scoped_identity(monkeypatch):
    pub = publisher("controller")
    writes = []
    monkeypatch.setattr(pub, "_delete_legacy_rest_state", lambda entity_id: None)
    monkeypatch.setattr(pub, "_publish_raw", lambda topic, payload, retain=True: writes.append((topic, payload, retain)))

    entity_id = "sensor.home_energy_manager_battery_learning"
    assert pub.publish_sensor(entity_id, "ready", {"friendly_name": "Battery Learning"}) is True

    topic = "homeassistant/sensor/home_energy_manager_controller/home_energy_manager_battery_learning/config"
    config_write = next(item for item in writes if item[0] == topic)
    payload = json.loads(config_write[1])
    assert payload["unique_id"] == "home_energy_manager_controller_home_energy_manager_battery_learning"
    assert not any(item[1] == "" for item in writes)
