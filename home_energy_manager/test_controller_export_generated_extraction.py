from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / 'components' / 'controller'


def test_export_generated_module_is_packaged_and_composed():
    module = CONTROLLER / 'controller_export_generated.py'
    app = (CONTROLLER / 'app.py').read_text()
    docker = (ROOT / 'Dockerfile').read_text()

    assert module.exists()
    assert 'from controller_export_generated import (' in app
    assert 'COPY components/controller/controller_export_generated.py /app/runtime/controller/controller_export_generated.py' in docker


def test_export_generated_hard_rule_helpers_live_in_extracted_module():
    source = (CONTROLLER / 'controller_export_generated.py').read_text()
    for name in (
        'export_generated_inputs',
        'forecast_export_kwh_between',
        'forced_incremental_grid_export_kwh',
        'export_generated_end',
        'export_generated_guardrail_end',
        'export_generated_topup_need',
    ):
        assert f'def {name}(' in source


def test_composition_root_delegates_export_generated_surface():
    app = (CONTROLLER / 'app.py').read_text()
    for helper in (
        '_export_generated_inputs(self, state)',
        '_forecast_export_kwh_between(self, state, start, end, attr)',
        '_forced_incremental_grid_export_kwh(self, state, start, end, rate_w)',
        '_export_generated_end(self, state, start, latest, remaining_kwh, rate_w)',
        '_export_generated_guardrail_end(self, state, start, latest, rate_w, arrival_soc, cap, reserve)',
    ):
        assert helper in app
