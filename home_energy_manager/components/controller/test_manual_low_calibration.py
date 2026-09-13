from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
RUNTIME = (ROOT / 'manual_low_calibration_runtime.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')
AGENTS = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')


def test_request_is_persisted_and_drives_existing_calibration_state():
    assert "_PENDING_KEY = 'manual_low_calibration_pending'" in RUNTIME
    assert "controller.db.set(_PENDING_KEY, True)" in RUNTIME
    assert "if _pending(self):\n        return 'awaiting_deep_low'" in RUNTIME
    assert 'All actual calibration planning and inverter writes continue through the existing' in RUNTIME


def test_input_button_and_boolean_are_edge_triggered_without_replay():
    assert "entity_id.startswith('input_button.')" in RUNTIME
    assert "entity_id.startswith('input_boolean.')" in RUNTIME
    assert "if previous is None:" in RUNTIME
    assert "previous.lower() != 'on' and value.lower() == 'on'" in RUNTIME
    assert 'one is already pending' in RUNTIME


def test_request_only_clears_after_observed_low_soc():
    assert "if soc is None or soc > floor:\n        return" in RUNTIME
    assert "self.db.set(_PENDING_KEY, False)" in RUNTIME
    assert "self.db.set(_COMPLETED_AT_KEY, completed_at)" in RUNTIME
    assert 'request cleared and normal deep-cycle timer reset' in RUNTIME
    assert 'merely\n    # requesting or starting discharge never resets/clears the request' in RUNTIME


def test_manual_request_diagnostics_are_exposed_on_existing_controller_sensor():
    for field in (
        'low_calibration_requested',
        'low_calibration_request_entity',
        'low_calibration_requested_at',
        'low_calibration_request_state',
        'low_calibration_request_reason',
        'low_calibration_request_target_soc',
        'low_calibration_request_completed_at',
        'low_calibration_request_last_result',
    ):
        assert field in RUNTIME


def test_package_config_exposes_optional_request_entity():
    cfg = yaml.safe_load((PACKAGE / 'config.yaml').read_text(encoding='utf-8'))
    assert cfg['options']['energy_strategy']['calibration_low_request_entity'] == ''
    assert cfg['schema']['energy_strategy']['calibration_low_request_entity'] == 'str?'


def test_runtime_is_packaged_above_existing_active_controller():
    assert 'COPY components/controller/active_runtime.py /app/runtime/controller/active_runtime.py' in DOCKER
    assert 'COPY components/controller/manual_low_calibration_runtime.py /app/runtime/controller/app.py' in DOCKER
    assert 'import active_runtime as runtime' in RUNTIME


def test_hard_rule_allows_controller_requested_low_observation_to_reset_interval():
    assert 'through a controller-requested calibration' in AGENTS
    assert 'resets the deep-cycle interval' in AGENTS
