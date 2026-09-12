from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = (ROOT / "minimise_export_runtime.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_minimise_export_calibration_contract_is_documented():
    assert "Minimise Export deep calibration targets reserve shortly after off-peak begins while minimising unpaid export" in AGENTS
    assert "reach the configured reserve 30 minutes after" in AGENTS
    assert "preceding cheap window is a calibration-preparation window" in AGENTS
    assert "normal minimise-export overnight charge target remains active" in AGENTS
    assert "Natural house consumption during the preparation day" in AGENTS
    assert "calculated start may be before the regular off-peak boundary" in AGENTS
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


def test_active_calibration_discharge_is_not_blocked_by_pause_discharge():
    assert "if calibration.get('action') == 'discharge_to_reserve':" in RUNTIME
    assert "A feasible calibration discharge must not be blocked" in RUNTIME
    assert "result['pause'] = {'mode': 'Disabled'" in RUNTIME


def test_runtime_uses_30_minute_reserve_target_without_cheap_start_clamp():
    assert "calibration_reserve_target_minutes_after_offpeak_start', 30" in RUNTIME
    assert "target_floor_time = window['start'] + timedelta(minutes=target_minutes)" in RUNTIME
    assert "target_floor_time - timedelta(hours=discharge_hours)" in RUNTIME
    assert "max(window['start'], desired_start)" not in RUNTIME
    assert "'kind': 'calibration_to_reserve'" in RUNTIME


def test_calibration_recharge_can_use_hardware_maximum():
    assert "self.calibration_state() == 'deep_recharge'" in RUNTIME
    assert "float(max_charge) / (float(capacity) * 1000.0)" in RUNTIME
    assert "self.c['max_charge_c_rate'] = original_max_c_rate" in RUNTIME


def test_cycle_fit_includes_dwell_and_charge_safety_margin():
    assert "reserve_dwell_minutes" in RUNTIME
    assert "charge_safety_margin_minutes" in RUNTIME
    assert "expected_complete <= window['end']" in RUNTIME
    assert "'kind'] = 'calibration_postponed'" in RUNTIME


def test_default_target_example_is_2330_for_2300_tariff_start():
    tz = ZoneInfo("Europe/London")
    offpeak_start = datetime(2026, 9, 12, 23, 0, tzinfo=tz)
    assert offpeak_start + timedelta(minutes=30) == datetime(2026, 9, 12, 23, 30, tzinfo=tz)


def test_long_discharge_can_start_before_cheap_rate_to_hit_2330_reserve():
    tz = ZoneInfo("Europe/London")
    offpeak_start = datetime(2026, 9, 12, 23, 0, tzinfo=tz)
    target_floor = offpeak_start + timedelta(minutes=30)
    required_discharge = timedelta(minutes=75)
    discharge_start = target_floor - required_discharge
    assert discharge_start == datetime(2026, 9, 12, 22, 15, tzinfo=tz)
    assert discharge_start < offpeak_start
    assert discharge_start + required_discharge == target_floor


def test_due_during_following_day_uses_previous_nights_cheap_window():
    tz = ZoneInfo("Europe/London")
    prep_start = datetime(2026, 9, 12, 23, 0, tzinfo=tz)
    following_offpeak = datetime(2026, 9, 13, 23, 0, tzinfo=tz)
    due_at = datetime(2026, 9, 13, 14, 0, tzinfo=tz)
    assert prep_start < due_at < following_offpeak
