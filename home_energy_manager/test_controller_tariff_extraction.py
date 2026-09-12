from pathlib import Path


ROOT = Path(__file__).parent
CONTROLLER = ROOT / 'components' / 'controller'


def test_tariff_module_exists_and_is_packaged():
    module = CONTROLLER / 'controller_tariff.py'
    assert module.exists()
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'controller_tariff.py /app/runtime/controller/controller_tariff.py' in dockerfile


def test_tariff_helpers_live_in_extracted_module():
    source = (CONTROLLER / 'controller_tariff.py').read_text()
    for helper in (
        'offpeak_from_forecast',
        'persist_offpeak',
        'fallback_offpeak',
        'current_active_offpeak',
    ):
        assert f'def {helper}(' in source


def test_composition_root_delegates_tariff_helpers():
    source = (CONTROLLER / 'app.py').read_text()
    assert 'from controller_tariff import (' in source
    assert 'return _offpeak_from_forecast(self,state)' in source
    assert 'return _persist_offpeak(self,window)' in source
    assert 'return _fallback_offpeak(self)' in source
    assert 'return _current_active_offpeak(self)' in source
