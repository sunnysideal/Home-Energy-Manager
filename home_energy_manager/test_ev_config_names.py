from pathlib import Path
import yaml

def test_ev_config_is_supplier_neutral():
    cfg=yaml.safe_load((Path(__file__).parent/"config.yaml").read_text())
    hf=cfg["options"]["home_forecaster"]
    ctl=cfg["options"]["controller"]
    assert "ev" in hf
    assert set(hf["ev"]) == {"energy_total_kwh","smart_charging_dispatch_entity","smart_charging_active_entity"}
    assert "intelligent_dispatching" not in hf["tariff"]
    assert "ev_smart_charging_enabled" in ctl
    assert "ev_smart_charging_dispatch_entity" in ctl
    assert "ev_smart_charging_active_entity" in ctl
    assert not any(k.startswith("intelligent_") for k in ctl)
