from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
JOINT = (ROOT / "controller_minimise_offpeak.py").read_text(encoding="utf-8")
APPLIER = (ROOT / "controller_plan_applier.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_minimise_export_does_not_pause_normal_cheap_window():
    assert "self.operation_mode() == 'minimise_export'" in APP
    assert "plan['mode'] = 'PauseBoth'" not in APP
    assert "plan['mode'] = 'Disabled'" in APP


def test_confirmed_cheap_idle_overlay_is_promoted_to_pause_both():
    assert "intelligent.get('pause_mode') == 'PauseDischarge'" in APP
    assert "intelligent['pause_mode'] = 'PauseBoth'" in APP
    assert "mode in ('PauseDischarge','PauseBoth')" in APPLIER
    assert "pause={'mode':mode" in APPLIER


def test_minimise_export_forecasts_discharge_before_charge():
    assert "coordinate_minimise_offpeak as _coordinate_minimise_offpeak" in APP
    assert "await _coordinate_minimise_offpeak(self, state, plan, window, fallback)" in APP
    assert "async def coordinate_minimise_offpeak" in JOINT
    assert "controller.choose_rate_and_start(" in JOINT
    assert "controller.soc_at(state, now, 'forecast_no_slots')" in JOINT
    assert "controller.live_soc()" in JOINT
    assert "forecast['joint_pause_charge_plan'] = True" in JOINT
    assert "plan['pause'] = {'mode': 'Disabled'" in JOINT


def test_joint_plan_is_not_applied_to_calibration_or_confirmed_ev_overlay():
    assert "plan.get('intelligent_go', {}).get('confirmed')" in JOINT
    assert "controller.calibration_state() in ('awaiting_deep_low', 'deep_recharge')" in JOINT


def test_overnight_eco_rule_is_documented_as_invariant():
    assert "do not schedule a regular overnight preservation pause" in AGENTS
    assert "including household load/PV before charging" in AGENTS
    assert "anchor that projection to live SOC" in AGENTS
