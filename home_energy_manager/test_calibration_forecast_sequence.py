from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_controller_preprograms_expected_calibration_recharge():
    text = (ROOT / 'components/controller/calibration_charge_runtime.py').read_text()
    assert "diagnostics.get('latest_safe_reserve_at')" in text
    assert "controller.c.get('reserve_dwell_minutes', 15)" in text
    assert "plan['charge']" in text
    assert "'kind': 'calibration_forecast_recharge'" in text
    assert "'target_soc': 100" in text
    assert "_calibration_max_charge_w" in text


def test_recharge_is_controller_plan_not_forecaster_overlay():
    assert not (ROOT / 'components/controller/calibration_plan_runtime.py').exists()
    assert not (ROOT / 'components/home_forecaster/app/calibration_forecast_runtime.py').exists()
    docker = (ROOT / 'Dockerfile').read_text()
    assert 'calibration_plan_runtime.py' not in docker
    assert 'calibration_forecast_runtime.py' not in docker
    assert 'COPY components/controller/calibration_charge_runtime.py /app/runtime/controller/app.py' in docker


def test_hard_rule_allows_forecast_slot_before_observed_floor():
    rules = (ROOT / 'components/controller/AGENTS.md').read_text()
    assert 'pre-program the expected future calibration recharge slot' in rules
    assert 'does not wait for an actual low-SOC observation before being written' in rules
    assert 'default `reserve_dwell_minutes` is 15 minutes' in rules
