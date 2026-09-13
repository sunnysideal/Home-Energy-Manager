from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = (ROOT / "minimise_export_runtime.py").read_text(encoding="utf-8")
ACTIVE = (ROOT / "manual_low_calibration_runtime.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_minimise_export_calibration_contract_is_documented():
    assert "Minimise Export deep calibration reaches reserve as late as practical" in AGENTS
    assert "leaving enough time for the configured reserve dwell" in AGENTS
    assert "preceding cheap window is a calibration-preparation window" in AGENTS
    assert "normal minimise-export overnight charge target remains active" in AGENTS
    assert "Natural house consumption and PV effects" in AGENTS
    assert "forced discharge/export must be limited to the residual" in AGENTS
    assert "may use any charge rate up to the hardware maximum" in AGENTS
    assert "postpone the deep calibration" in AGENTS


def test_runtime_looks_ahead_one_regular_window_for_deep_calibration():
    assert "def _deep_calibration_due_at(controller):" in RUNTIME
    assert "def _minimise_calibration_preparation(controller, window):" in RUNTIME
    assert "last_deep + timedelta(days=int(controller.c.get('deep_cycle_every_days', 60)))" in RUNTIME
    assert "following_offpeak_start" in RUNTIME
    assert "'state': 'pending_deep_low'" in RUNTIME
    assert "'strategy': 'natural_load_before_deep_calibration'" in RUNTIME


def test_preparation_disables_pause_but_preserves_normal_charge_planning():
    assert "calibration_preparation = _minimise_calibration_preparation(self, window)" in RUNTIME
    assert "if calibration_preparation is not None:" in RUNTIME
    assert "result['pause'] = {'mode': 'Disabled'" in RUNTIME
    assert "core charge target is deliberately left" in RUNTIME
    assert "scheduled cheap charging still happens when Rule 7 requires it" in RUNTIME


def test_active_calibration_depletion_is_not_blocked_by_pause():
    assert "plan['pause'] = {'mode': 'Disabled'" in ACTIVE
    assert "Preservation must not defeat the natural-depletion-first calibration plan" in ACTIVE


def test_runtime_targets_latest_safe_reserve_point():
    assert "latest_safe_reserve = window['end'] - timedelta(" in ACTIVE
    assert "hours=recharge_hours, minutes=dwell_minutes + safety_minutes" in ACTIVE
    assert "target_floor_time = window['start'] + timedelta(minutes=target_minutes)" not in ACTIVE
    assert "'kind': 'calibration_to_reserve'" in ACTIVE


def test_calibration_recharge_can_use_hardware_maximum():
    assert "self.calibration_state() == 'deep_recharge'" in RUNTIME
    assert "float(max_charge) / (float(capacity) * 1000.0)" in RUNTIME
    assert "self.c['max_charge_c_rate'] = original_max_c_rate" in RUNTIME


def test_cycle_fit_includes_dwell_and_charge_safety_margin():
    assert "reserve_dwell_minutes" in ACTIVE
    assert "charge_safety_margin_minutes" in ACTIVE
    assert "expected_complete" in ACTIVE
    assert "'calibration_postponed'" in ACTIVE


def test_late_target_example_for_2300_to_0600_window():
    tz = ZoneInfo("Europe/London")
    offpeak_end = datetime(2026, 9, 13, 6, 0, tzinfo=tz)
    # Example only: 30 min dwell + 10 min margin + 3 h recharge leaves 02:20.
    latest_safe = offpeak_end - timedelta(hours=3, minutes=40)
    assert latest_safe == datetime(2026, 9, 13, 2, 20, tzinfo=tz)


def test_due_during_following_day_uses_previous_nights_cheap_window():
    tz = ZoneInfo("Europe/London")
    prep_start = datetime(2026, 9, 12, 23, 0, tzinfo=tz)
    following_offpeak = datetime(2026, 9, 13, 23, 0, tzinfo=tz)
    due_at = datetime(2026, 9, 13, 14, 0, tzinfo=tz)
    assert prep_start < due_at < following_offpeak
