from pathlib import Path
import yaml


def test_ev_config_is_supplier_neutral_and_single_owner():
    cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
    ev = cfg["options"]["ev"]
    assert set(ev) == {
        "energy_total_kwh",
        "smart_charging_dispatch",
        "smart_charging_active",
        "smart_charging_enabled",
    }
    assert not any(k.startswith("intelligent_") for k in ev)
    text = (Path(__file__).parent / "config.yaml").read_text()
    assert "intelligent_dispatching" not in text
    assert "ev_smart_charging_dispatch_entity" not in text
    assert "ev_smart_charging_active_entity" not in text
