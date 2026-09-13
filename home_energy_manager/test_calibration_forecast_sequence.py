from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_controller_publishes_expected_calibration_sequence():
    text = (ROOT / 'components/controller/calibration_plan_runtime.py').read_text()
    assert 'sensor.home_energy_manager_calibration_plan' in text
    assert "'expected_low_at'" in text
    assert "'expected_recharge_start'" in text
    assert "'expected_recharge_end'" in text
    assert "'expected_recharge_rate_w'" in text
    assert "'expected_recharge_target_soc': 100" in text


def test_forecaster_consumes_controller_declared_sequence():
    text = (ROOT / 'components/home_forecaster/app/calibration_forecast_runtime.py').read_text()
    assert 'sensor.home_energy_manager_calibration_plan' in text
    assert "client.state_optional(_ENTITY)" in text
    assert "batt['calibration_plan'] = plan" in text
    assert "sim_batt['charge_enabled'] = True" in text
    assert "sim_batt['charge_rate_w']" in text
    assert "sim_batt['charge_slots']" in text


def test_forecast_overlay_is_date_bounded_and_does_not_change_controller_completion():
    text = (ROOT / 'components/home_forecaster/app/calibration_forecast_runtime.py').read_text()
    assert "_overlaps(slot_start, slot_end, plan)" in text
    assert "plan['start']" in text and "plan['end']" in text
    controller = (ROOT / 'components/controller/calibration_plan_runtime.py').read_text()
    assert 'does not change Controller policy' in controller
    assert 'real recharge still waits for an observed low-SOC event' in controller


def test_docker_packages_calibration_wrappers():
    docker = (ROOT / 'Dockerfile').read_text()
    assert 'COPY components/home_forecaster/app/calibration_forecast_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py' in docker
    assert 'COPY components/controller/calibration_plan_runtime.py /app/runtime/controller/app.py' in docker
