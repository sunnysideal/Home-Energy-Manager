from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE = (ROOT / 'active_runtime.py').read_text(encoding='utf-8')


def test_calibration_status_reports_all_persisted_calibration_entities():
    for token in (
        'last_full_soc_at',
        'last_deep_calibration_at',
        'calibration_low_reached_at',
        'last_below_40_soc_at',
        'last_below_20_soc_at',
        'last_low_soc_at',
        'top_full_age_days',
        'top_full_due_in_days',
        'deep_cycle_age_days',
        'deep_cycle_due_in_days',
    ):
        assert token in ACTIVE


def test_calibration_logging_includes_status_transitions_and_events():
    assert 'Calibration status: state=%s reason=%s' in ACTIVE
    assert 'Calibration state change: %s -> %s reason=%s' in ACTIVE
    assert 'Calibration event: full SOC observed' in ACTIVE
    assert 'Calibration event: deep calibration completed' in ACTIVE
    assert 'SOC calibration observation: crossed %.0f%% downward' in ACTIVE
    assert 'Calibration low SOC observed naturally/actively' in ACTIVE


def test_calibration_diagnostics_are_published_with_controller_status():
    assert 'core.Controller.calibration_attrs = _calibration_attrs_with_diagnostics' in ACTIVE
    assert "'top_full_every_days': top_days" in ACTIVE
    assert "'deep_cycle_every_days': deep_days" in ACTIVE
    assert "'deep_cycle_floor_soc': floor" in ACTIVE
