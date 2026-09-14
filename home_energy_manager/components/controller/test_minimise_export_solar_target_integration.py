from pathlib import Path

ROOT = Path(__file__).resolve().parent
OVERLAY = (ROOT / "controller_overlay_runtime.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
DOCKER = (ROOT.parent.parent / "Dockerfile").read_text(encoding="utf-8")


def test_rule_7_requires_solar_headroom_plus_peak_protection():
    assert "highest cheap-rate SOC that still leaves enough forecast battery headroom" in AGENTS
    assert "maximum of: the configured minimum floor" in AGENTS
    assert "SOC required to supply the forecast peak-rate bridge" in AGENTS
    assert "solar-headroom target" in AGENTS
    assert "low-solar/high-load days should tend toward a 100% cheap-rate target" in AGENTS
    assert "hardware charge-power limit is not avoidable" in AGENTS


def test_active_overlay_raises_floor_to_solar_target_before_legacy_safety_logic():
    assert "from minimise_export_headroom import highest_soc_without_avoidable_export" in OVERLAY
    assert "async with _minimise_export_solar_floor(self,forecast,window) as solar_diag" in OVERLAY
    assert "controller.c['minimise_export_min_soc']=max(float(original),float(solar_target))" in OVERLAY
    assert "final_target=max(int(minimise.get('required_target_soc') or 0)" in OVERLAY
    assert "'strategy']='avoid_peak_import_and_preserve_pv_headroom'" in OVERLAY


def test_incomplete_headroom_forecast_fails_safe_full():
    assert "incomplete_forecast_fail_safe_full" in OVERLAY
    assert "missing_battery_inputs_fail_safe_full" in OVERLAY
    assert "solar_headroom_target_soc':100" in OVERLAY


def test_runtime_image_packages_headroom_helper():
    assert "COPY components/controller/minimise_export_headroom.py /app/runtime/controller/minimise_export_headroom.py" in DOCKER
