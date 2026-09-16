from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
RUNTIME = (ROOT / 'manual_high_calibration_runtime.py').read_text(encoding='utf-8')
CHARGE = (ROOT / 'calibration_charge_runtime.py').read_text(encoding='utf-8')
LOW = (ROOT / 'manual_low_calibration_runtime.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')


def test_package_owns_high_calibration_button():
    assert "_BUTTON_ENTITY = 'button.home_energy_manager_request_high_calibration'" in RUNTIME
    assert "'Request High Calibration'" in RUNTIME
    assert "'mdi:battery-arrow-up'" in RUNTIME
    assert 'publish_command_button(' in RUNTIME
    assert 'input_button.' not in RUNTIME
    assert 'input_boolean.' not in RUNTIME


def test_request_is_persistent_and_duplicate_safe():
    assert "_PENDING_KEY = 'manual_high_calibration_pending'" in RUNTIME
    assert "controller.db.set(_PENDING_KEY, True)" in RUNTIME
    assert 'button press ignored because one is already pending' in RUNTIME
    assert 'if _pending(controller):' in RUNTIME


def test_manual_high_uses_existing_top_due_planner_even_when_auto_disabled():
    state_fn = RUNTIME.split('def _manual_high_calibration_state(self):', 1)[1].split('\ndef _request_status', 1)[0]
    assert "if _pending(self):" in state_fn
    assert "return 'top_due'" in state_fn
    assert 'calibration_enabled' in state_fn
    assert 'automatic periodic scheduling only' in state_fn
    assert 'direct' not in state_fn.lower() or 'direct inverter' not in state_fn.lower()


def test_deep_calibration_keeps_priority_and_high_request_remains_latched():
    state_fn = RUNTIME.split('def _manual_high_calibration_state(self):', 1)[1].split('\ndef _request_status', 1)[0]
    assert "if state in ('awaiting_deep_low', 'deep_recharge'):" in state_fn
    assert 'return state' in state_fn
    assert "return 'blocked', f'existing_{state}'" in RUNTIME


def test_completion_requires_actual_100_percent_and_resets_top_timestamp():
    sample_fn = RUNTIME.split('async def _sample_with_manual_high_calibration(self):', 1)[1]
    assert 'if soc >= _TARGET_SOC:' in sample_fn
    assert "_TARGET_SOC = 100.0" in RUNTIME
    assert "self.db.set('last_full_soc_at', now_iso)" in sample_fn
    assert "self.db.set(_PENDING_KEY, False)" in sample_fn
    assert "self.db.set(_LAST_RESULT_KEY, 'completed')" in sample_fn
    assert '99%' in RUNTIME


def test_automatic_calibration_setting_is_not_reenabled_on_completion():
    assert "'enabled' if self.c.get('calibration_enabled', True) else 'disabled'" in RUNTIME
    assert "self.c['calibration_enabled']" not in RUNTIME
    assert "self.c['calibration_enabled'] =" not in RUNTIME


def test_request_uses_normal_charge_session_top_completion_learning_path():
    assert 'top_due' in RUNTIME
    assert 'charge_minutes' not in RUNTIME
    assert 'ensure(' not in RUNTIME
    assert '.ha.write(' not in RUNTIME


def test_request_diagnostics_cover_button_state_reason_attempt_and_completion():
    for field in (
        'high_calibration_requested',
        'high_calibration_request_button',
        'high_calibration_request_button_available',
        'high_calibration_requested_at',
        'high_calibration_request_state',
        'high_calibration_request_reason',
        'high_calibration_request_target_soc',
        'high_calibration_request_completed_at',
        'high_calibration_request_last_attempt_at',
        'high_calibration_request_last_result',
    ):
        assert field in RUNTIME
    assert "return 'charging', 'user_requested'" in RUNTIME
    assert "return 'planned', 'user_requested'" in RUNTIME
    assert "return ('completed', 'target_observed')" in RUNTIME


def test_existing_clear_calibration_button_clears_high_request_without_history():
    assert "('manual_high_calibration_pending', False)" in LOW
    assert "controller.db.set('manual_high_calibration_last_result', 'cleared')" in LOW
    clearable = LOW.split('_CLEARABLE_CALIBRATION_STATE = (', 1)[1].split('\n)', 1)[0]
    assert 'last_full_soc_at' not in clearable


def test_runtime_chain_loads_high_layer_before_calibration_charge_layer():
    assert 'COPY components/controller/manual_high_calibration_runtime.py /app/runtime/controller/manual_high_calibration_runtime.py' in DOCKER
    assert 'import manual_high_calibration_runtime as runtime' in CHARGE
    assert 'import manual_low_calibration_runtime as runtime' in RUNTIME


def test_forecast_only_path_is_unchanged_and_write_free():
    forecast_only = (ROOT / 'forecast_only.py').read_text(encoding='utf-8')
    assert 'manual_high_calibration_runtime' not in forecast_only
    assert '/services/' not in forecast_only
