"""Source-level contracts complement the executable packaged-controller tests.

Calibration state/request observations moved to calibration_coordinator in #116
PR 1. Scheduling stays in manual_low_calibration_runtime until PR 2.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
COORDINATOR = (ROOT / 'calibration_coordinator.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'manual_low_calibration_runtime.py').read_text(encoding='utf-8')
ACTIVE = (ROOT / 'active_runtime.py').read_text(encoding='utf-8')
MQTT_BUTTON = (PACKAGE / 'common' / 'mqtt_button.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')
AGENTS = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')


def test_coordinator_owns_low_intent_and_completion():
    assert "LOW_PENDING = 'manual_low_calibration_pending'" in COORDINATOR
    assert "c.db.set(LOW_PENDING, True)" in COORDINATOR
    assert "state = 'awaiting_deep_low'" in COORDINATOR
    assert "c.db.set(LOW_PENDING, False)" in COORDINATOR
    assert "c.db.set(LOW_COMPLETED_AT, low_reached)" in COORDINATOR
    assert "c.db.set('calibration_low_reached_at', low_reached)" in COORDINATOR
    assert "if before['low_pending'] and soc <= floor:" in COORDINATOR
    assert "if state != 'deep_recharge' and c.db.ok and c.db.get('calibration_low_reached_at'):" in COORDINATOR


def test_coordinator_owns_nonretained_mqtt_buttons_and_duplicate_requests():
    assert "LOW_BUTTON = 'button.home_energy_manager_request_low_calibration'" in COORDINATOR
    assert "CLEAR_BUTTON = 'button.home_energy_manager_clear_calibration_state'" in COORDINATOR
    assert "'Request Low Calibration'" in COORDINATOR
    assert "'Clear Calibration State'" in COORDINATOR
    assert 'publish_command_button(' in COORDINATOR
    assert 'threading.Event()' in COORDINATOR
    assert 'event.clear()' in COORDINATOR
    assert 'button press ignored because one is already pending' in COORDINATOR
    assert '"payload_press": "PRESS"' in MQTT_BUTTON
    assert 'message_callback_add(command_topic, _message)' in MQTT_BUTTON


def test_sample_and_state_are_invoked_through_one_coordinator():
    assert 'self._calibration = calibration_coordinator.CalibrationCoordinator(' in ACTIVE
    assert 'return self._calibration.state()' in ACTIVE
    assert 'return self._calibration.attributes(attrs)' in ACTIVE
    assert 'before = self._calibration.before_sample()' in ACTIVE
    assert 'await self._calibration.after_sample(before)' in ACTIVE
    assert 'core.Controller.sample =' not in RUNTIME
    assert 'core.Controller.calibration_state =' not in RUNTIME
    assert 'core.Controller.__init__ =' not in RUNTIME
    assert 'core.Controller.calibration_attrs =' not in RUNTIME


def test_packaged_coordinator_and_unchanged_calibration_planner():
    assert 'COPY components/controller/calibration_coordinator.py /app/runtime/controller/calibration_coordinator.py' in DOCKER
    assert 'COPY components/controller/manual_low_calibration_runtime.py /app/runtime/controller/app.py' in DOCKER
    assert 'import active_runtime as runtime' in RUNTIME
    assert '_calibration_policy_runtime._minimise_calibration_discharge = _natural_first_calibration_discharge' in RUNTIME
    assert "if controller.calibration_state() != 'awaiting_deep_low':" in RUNTIME


def test_low_depletion_rule_and_recharge_timing_unchanged():
    assert "latest_safe_reserve = window['end'] - timedelta(" in RUNTIME
    assert 'hours=recharge_hours, minutes=dwell_minutes + safety_minutes' in RUNTIME
    assert "controller.soc_at(forecast, latest_safe_reserve, 'forecast_no_slots')" in RUNTIME
    assert "strategy = 'natural_discharge'" in RUNTIME
    assert "strategy = 'natural_plus_forced'" in RUNTIME
    assert "strategy = 'forced_discharge'" in RUNTIME
    assert "residual_soc = max(0.0, projected_without_pause - float(reserve))" in RUNTIME
    assert "'planned_kwh': round(residual_kwh, 3)" in RUNTIME
    assert "plan['pause'] = {'mode': 'Disabled'" in RUNTIME
    assert "plan['discharge']['kind'] = 'calibration_natural_depletion'" in RUNTIME
    assert '_NATURAL_MARGIN_SOC = 0.5' in RUNTIME
    assert 'as late as practical within the regular off-peak window' in AGENTS


def test_clear_only_transient_state_preserves_history():
    clearable = COORDINATOR.split('CLEARABLE = (', 1)[1].split('\n)', 1)[0]
    for key in ('manual_low_calibration_pending', 'manual_high_calibration_pending',
                'high_calibration_requested', 'manual_high_calibration_requested',
                'calibration_low_reached_at'):
        assert key in COORDINATOR
    for key in ('last_full_soc_at', 'last_deep_calibration_at',
                'last_below_40_soc_at', 'last_below_20_soc_at',
                'last_low_soc_at', 'soc_crossing_40', 'soc_crossing_20'):
        assert key not in clearable
    assert 'historical calibration timestamps and learning preserved' in COORDINATOR
    assert '.ha.write(' not in COORDINATOR
    assert '.ensure(' not in COORDINATOR


def test_manual_low_status_preserves_user_facing_entities():
    for field in ('low_calibration_requested', 'low_calibration_request_button',
                  'low_calibration_request_button_available', 'low_calibration_requested_at',
                  'low_calibration_request_state', 'low_calibration_request_reason',
                  'low_calibration_request_target_soc', 'low_calibration_request_completed_at',
                  'low_calibration_request_last_result', 'calibration_clear_button',
                  'calibration_clear_button_available', 'calibration_state_cleared_at',
                  'calibration_state_clear_result', 'calibration_state_cleared_keys'):
        assert field in COORDINATOR
    assert 'automatic periodic calibration scheduling only' in AGENTS
    assert 'resets the deep-cycle interval' in AGENTS
