from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_progressive_write_retry_defaults_and_formula():
    source = (ROOT / "components/controller/controller_legacy_core.py").read_text()
    cfg = (ROOT / "config.yaml").read_text()
    component_cfg = (ROOT / "components/controller/standalone-config.yaml").read_text()

    assert "write_retry_attempts',4" in source
    assert "write_retry_delay_seconds',10" in source
    assert "wait=max(0,base_delay*i)" in source
    assert "write_retry_attempts: 4" in cfg
    assert "write_retry_delay_seconds: 10" in cfg
    assert "write_retry_attempts: 4" in component_cfg
    assert "write_retry_delay_seconds: 10" in component_cfg
