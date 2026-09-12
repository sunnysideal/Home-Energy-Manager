from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
APPLIER = (ROOT / "controller_plan_applier.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_minimise_export_uses_pause_both_for_zero_flow_preservation():
    assert "self.operation_mode() == 'minimise_export'" in APP
    assert "plan['mode'] = 'PauseBoth'" in APP


def test_confirmed_cheap_idle_overlay_is_promoted_to_pause_both():
    assert "intelligent.get('pause_mode') == 'PauseDischarge'" in APP
    assert "intelligent['pause_mode'] = 'PauseBoth'" in APP
    assert "mode in ('PauseDischarge','PauseBoth')" in APPLIER
    assert "pause={'mode':mode" in APPLIER


def test_minimise_export_coordinates_pause_and_charge_as_one_plan():
    assert "async def _coordinate_minimise_offpeak" in APP
    assert "preserved_soc = await self.live_soc() if active else None" in APP
    assert "charge_minutes = self.charge_minutes(preserved_soc, target, rate, capacity)" in APP
    assert "pause_end = charge_start.replace(second=0, microsecond=0)" in APP
    assert "'joint_pause_charge_plan'] = True" in APP
    assert "PauseBoth -> Charge" in APP


def test_minimise_export_does_not_schedule_charge_when_preserved_soc_meets_target():
    assert "needs_charge = target > preserved_soc + 0.5" in APP
    assert "charge_end = charge_start" in APP
    assert "planned_kwh = 0.0" in APP


def test_joint_plan_is_not_applied_to_calibration_or_confirmed_ev_overlay():
    assert "plan.get('intelligent_go', {}).get('confirmed')" in APP
    assert "self.calibration_state() in ('awaiting_deep_low', 'deep_recharge')" in APP


def test_joint_pause_charge_rule_is_documented_as_invariant():
    assert "regular cheap-window preservation and charging are one coordinated plan" in AGENTS
    assert "must not overlap `PauseBoth`" in AGENTS
    assert "SOC that the controller expects `PauseBoth` to preserve" in AGENTS
