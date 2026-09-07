from pathlib import Path


def test_supervisor_and_mqtt_permissions_declared():
    text = (Path(__file__).parent / "config.yaml").read_text()
    assert "homeassistant_api: true" in text
    assert "hassio_api: true" in text
    assert "hassio_role: default" in text
    assert "hassio_role: manager" not in text
    assert "services:\n- mqtt:want" in text or "services:\n  - mqtt:want" in text
