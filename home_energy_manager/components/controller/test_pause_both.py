from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
APPLIER = (ROOT / "controller_plan_applier.py").read_text(encoding="utf-8")


def test_minimise_export_uses_pause_both_for_zero_flow_preservation():
    assert "self.operation_mode() == 'minimise_export'" in APP
    assert "plan['mode'] = 'PauseBoth'" in APP


def test_confirmed_cheap_idle_overlay_is_promoted_to_pause_both():
    assert "intelligent.get('pause_mode') == 'PauseDischarge'" in APP
    assert "intelligent['pause_mode'] = 'PauseBoth'" in APP
    assert "mode in ('PauseDischarge','PauseBoth')" in APPLIER
    assert "pause={'mode':mode" in APPLIER
