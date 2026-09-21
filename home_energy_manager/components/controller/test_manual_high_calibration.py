"""High-calibration contract: the same coordinator handles low and high intent."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
COORDINATOR = (ROOT / 'calibration_coordinator.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'manual_high_calibration_runtime.py').read_text(encoding='utf-8')
CHARGE = (ROOT / 'calibration_charge_runtime.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')


def test_high_button_and_existing_persistence_contract():
    assert "HIGH_PENDING = 'manual_high_calibration_pending'" in COORDINATOR
    assert "HIGH_BUTTON = 'button.home_energy_manager_request_high_calibration'" in COORDINATOR
    assert "'Request High Calibration'" in COORDINATOR
    assert "'mdi:battery-arrow-up'" in COORDINATOR
    assert 'publish_command_button(' in COORDINATOR
    assert 'c.db.set(HIGH_PENDING, True)' in COORDINATOR
    assert 'button press ignored because one is already pending' in COORDINATOR


def test_manual_high_uses_existing_top_due_without_overriding_deep_cycle():
    state_fn = COORDINATOR.split('    def state(self):', 1)[1].split('\n    def _low_status', 1)[0]
    assert "if state in ('awaiting_deep_low', 'deep_recharge'):" in state_fn
    assert "return 'top_due' if self._pending(HIGH_PENDING) else state" in state_fn
    assert "return 'blocked', f'existing_{state}'" in COORDINATOR
    assert "c.c['calibration_enabled'] =" not in COORDINATOR


def test_observed_100_percent_only_completes_high_request():
    observation = COORDINATOR.split('    async def after_sample(self, before):', 1)[1]
    assert "if before['high_pending']:" in observation
    assert 'if soc >= 100:' in observation
    assert "c.db.set('last_full_soc_at', now_iso)" in observation
    assert 'c.db.set(HIGH_PENDING, False)' in observation
    assert "c.db.set(HIGH_RESULT, 'completed')" in observation
    assert 'never complete a high request at 99%' in observation


def test_high_diagnostics_and_clear_keep_historical_state():
    for field in ('high_calibration_requested', 'high_calibration_request_button',
                  'high_calibration_request_button_available', 'high_calibration_requested_at',
                  'high_calibration_request_state', 'high_calibration_request_reason',
                  'high_calibration_request_target_soc', 'high_calibration_request_completed_at',
                  'high_calibration_request_last_attempt_at', 'high_calibration_request_last_result'):
        assert field in COORDINATOR
    assert "(HIGH_PENDING, False)" in COORDINATOR
    assert "c.db.set(HIGH_RESULT, 'cleared')" in COORDINATOR
    clearable = COORDINATOR.split('CLEARABLE = (', 1)[1].split('\n)', 1)[0]
    assert 'last_full_soc_at' not in clearable
    assert 'charge_minutes' not in COORDINATOR
    assert 'ensure(' not in COORDINATOR
    assert '.ha.write(' not in COORDINATOR


def test_high_compatibility_import_has_no_controller_patches():
    assert 'import manual_low_calibration_runtime as runtime' in RUNTIME
    assert 'import manual_high_calibration_runtime as runtime' in CHARGE
    assert 'COPY components/controller/manual_high_calibration_runtime.py /app/runtime/controller/manual_high_calibration_runtime.py' in DOCKER
    assert 'core.Controller.sample =' not in RUNTIME
    assert 'core.Controller.calibration_state =' not in RUNTIME
    assert 'core.Controller.__init__ =' not in RUNTIME


def test_forecast_only_path_is_unchanged_and_write_free():
    passive = (ROOT / 'forecast_only.py').read_text(encoding='utf-8')
    assert 'manual_high_calibration_runtime' not in passive
    assert '/services/' not in passive
