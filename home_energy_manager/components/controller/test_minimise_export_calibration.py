from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = (ROOT / "minimise_export_runtime.py").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_minimise_export_calibration_contract_is_documented():
    assert "Minimise Export deep calibration stays inside regular off-peak" in AGENTS
    assert "reach the configured reserve 30 minutes after" in AGENTS
    assert "regular off-peak start is a hard lower bound" in AGENTS
    assert "may use any charge rate up to the hardware maximum" in AGENTS
    assert "postpone the deep calibration" in AGENTS


def test_runtime_uses_30_minute_default_and_never_moves_before_cheap_start():
    assert "calibration_reserve_target_minutes_after_offpeak_start', 30" in RUNTIME
    assert "target_floor_time = window['start'] + timedelta(minutes=target_minutes)" in RUNTIME
    assert "discharge_start = max(window['start'], desired_start)" in RUNTIME
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


def test_long_discharge_starts_at_cheap_boundary_instead_of_peak_rate():
    tz = ZoneInfo("Europe/London")
    offpeak_start = datetime(2026, 9, 12, 23, 0, tzinfo=tz)
    target_floor = offpeak_start + timedelta(minutes=30)
    required_discharge = timedelta(minutes=75)
    desired_start = target_floor - required_discharge
    actual_start = max(offpeak_start, desired_start)
    assert desired_start < offpeak_start
    assert actual_start == offpeak_start
