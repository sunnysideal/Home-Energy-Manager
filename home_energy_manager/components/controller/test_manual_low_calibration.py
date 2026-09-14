from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
RUNTIME = (ROOT / 'manual_low_calibration_runtime.py').read_text(encoding='utf-8')
MQTT_BUTTON = (PACKAGE / 'common' / 'mqtt_button.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')
AGENTS = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')


def test_request_is_persisted_and_drives_existing_calibration_state():
    assert "_PENDING_KEY = 'manual_low_calibration_pending'" in RUNTIME
    assert "controller.db.set(_PENDING_KEY, True)" in RUNTIME
    assert "return 'awaiting_deep_low'" in RUNTIME
    assert 'existing planner' in RUNTIME


def test_package_owns_low_calibration_mqtt_button():
    assert "_BUTTON_ENTITY = 'button.home_energy_manager_request_low_calibration'" in RUNTIME
    assert 'publish_command_button(' in RUNTIME
    assert "'Request Low Calibration'" in RUNTIME
    assert 'calibration_low_request_entity' not in RUNTIME
    assert 'input_button.' not in RUNTIME
    assert 'input_boolean.' not in RUNTIME


def test_mqtt_button_command_is_non_retained_and_thread_safe():
    assert '"command_topic": command_topic' in MQTT_BUTTON
    assert '"payload_press": "PRESS"' in MQTT_BUTTON
    assert 'message_callback_add(command_topic, _message)' in MQTT_BUTTON
    assert 'subscribe(command_topic, qos=1)' in MQTT_BUTTON
    assert 'threading.Event()' in RUNTIME
    assert 'event.set' in RUNTIME
    assert 'event.clear()' in RUNTIME
    assert '_latch_request(self)' in RUNTIME
    assert '_publish_raw(discovery_topic' in MQTT_BUTTON


def test_duplicate_press_while_pending_does_not_queue_another_calibration():
    assert 'button press ignored because one is already pending' in RUNTIME
    assert 'if _pending(controller):' in RUNTIME


def test_request_only_clears_after_observed_low_soc():
    assert 'if was_pending and soc <= floor:' in RUNTIME
    assert "self.db.set(_PENDING_KEY, False)" in RUNTIME
    assert "self.db.set(_COMPLETED_AT_KEY, completed_at)" in RUNTIME
    assert 'request cleared and recharge state preserved' in RUNTIME


def test_manual_request_diagnostics_are_exposed_on_existing_controller_sensor():
    for field in (
        'low_calibration_requested', 'low_calibration_request_button',
        'low_calibration_request_button_available', 'low_calibration_requested_at',
        'low_calibration_request_state', 'low_calibration_request_reason',
        'low_calibration_request_target_soc', 'low_calibration_request_completed_at',
        'low_calibration_request_last_result',
    ):
        assert field in RUNTIME


def test_runtime_is_packaged_above_existing_active_controller():
    assert 'COPY components/controller/active_runtime.py /app/runtime/controller/active_runtime.py' in DOCKER
    assert 'COPY components/controller/manual_low_calibration_runtime.py /app/runtime/controller/app.py' in DOCKER
    assert 'COPY common /app/common' in DOCKER
    assert 'import active_runtime as runtime' in RUNTIME


def test_hard_rule_allows_controller_requested_low_observation_to_reset_interval():
    assert 'through a controller-requested calibration' in AGENTS
    assert 'resets the deep-cycle interval' in AGENTS


def test_low_point_is_latest_safe_point_not_fixed_minutes_after_cheap_start():
    assert 'latest_safe_reserve = window[\'end\'] - timedelta(' in RUNTIME
    assert 'hours=recharge_hours, minutes=dwell_minutes + safety_minutes' in RUNTIME
    assert 'as late as practical within the regular off-peak window' in AGENTS
    assert '30 minutes after the regular off-peak window begins' not in AGENTS


def test_natural_depletion_is_evaluated_at_latest_safe_reserve_point():
    assert "controller.soc_at(forecast, latest_safe_reserve, 'forecast_no_slots')" in RUNTIME
    assert "strategy = 'natural_discharge'" in RUNTIME
    assert "strategy = 'natural_plus_forced'" in RUNTIME
    assert "strategy = 'forced_discharge'" in RUNTIME
    assert "'projected_soc_with_pause'" in RUNTIME
    assert "'projected_soc_without_pause'" in RUNTIME
    assert "'predicted_natural_depletion_soc'" in RUNTIME


def test_forced_discharge_is_only_residual_after_natural_depletion():
    assert 'residual_soc = max(0.0, projected_without_pause - float(reserve))' in RUNTIME
    assert 'residual_kwh = float(capacity) * residual_soc / 100.0' in RUNTIME
    assert "'residual_forced_discharge_kwh': round(residual_kwh, 3)" in RUNTIME
    assert "'planned_kwh': round(residual_kwh, 3)" in RUNTIME


def test_natural_calibration_disables_preservation_and_schedules_no_export():
    assert "plan['pause'] = {'mode': 'Disabled'" in RUNTIME
    assert "plan['discharge']['kind'] = 'calibration_natural_depletion'" in RUNTIME
    assert "diagnostics['action'] = 'allow_natural_discharge'" in RUNTIME


def test_small_forecast_noise_has_bounded_margin():
    assert '_NATURAL_MARGIN_SOC = 0.5' in RUNTIME
    assert 'if residual_soc <= _NATURAL_MARGIN_SOC and coverage_complete:' in RUNTIME


def test_automatic_and_manual_low_calibration_share_strategy_function():
    assert '_legacy_runtime._minimise_calibration_discharge = _natural_first_calibration_discharge' in RUNTIME
    assert "if controller.calibration_state() != 'awaiting_deep_low':" in RUNTIME


def test_manual_request_overrides_automatic_calibration_disabled_state():
    state_fn = RUNTIME.split('def _manual_calibration_state(self):', 1)[1].split('\ndef _request_status', 1)[0]
    assert "if _pending(self):" in state_fn
    assert "return 'awaiting_deep_low'" in state_fn
    assert "if state == 'disabled':" not in state_fn
    assert 'automatic ``disabled`` state' in state_fn
    assert 'automatic periodic calibration scheduling only' in AGENTS


def test_disabled_automatic_calibration_is_not_reported_as_manual_request_blocker():
    status_fn = RUNTIME.split('def _request_status(controller):', 1)[1].split('\ndef _calibration_attrs_with_manual_request', 1)[0]
    assert 'calibration_disabled' not in status_fn
    assert "return 'planned', 'user_requested'" in status_fn


def test_manual_low_endpoint_preserves_deep_recharge_when_auto_is_disabled():
    assert "self.db.set('calibration_low_reached_at', low_reached)" in RUNTIME
    assert "if self.db.ok and self.db.get('calibration_low_reached_at'):" in RUNTIME
    assert "return 'deep_recharge'" in RUNTIME
    assert 'preserving normal deep-recharge completion' in RUNTIME


def test_manual_recharge_completion_does_not_reenable_automatic_calibration():
    assert "if not self.c.get('calibration_enabled', True) and low_reached and soc >= 100:" in RUNTIME
    assert "self.db.set('last_full_soc_at', now_iso)" in RUNTIME
    assert "self.db.set('last_deep_calibration_at', now_iso)" in RUNTIME
    assert "self.db.set('calibration_low_reached_at', None)" in RUNTIME
    assert 'automatic calibration remains disabled' in RUNTIME
