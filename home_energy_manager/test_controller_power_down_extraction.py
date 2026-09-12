from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / 'components' / 'controller'


def test_power_down_module_is_packaged():
    assert (CONTROLLER / 'controller_power_down.py').is_file()
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'controller_power_down.py /app/runtime/controller/controller_power_down.py' in dockerfile


def test_composition_root_delegates_power_down_helpers():
    source = (CONTROLLER / 'app.py').read_text()
    assert 'from controller_power_down import (' in source
    assert 'power_down_protected_soc as _power_down_protected_soc' in source
    assert 'power_down_info as _power_down_info' in source
    assert 'return _power_down_protected_soc(' in source
    assert 'return await _power_down_info(' in source


def test_extracted_module_owns_active_power_down_implementations():
    source = (CONTROLLER / 'controller_power_down.py').read_text()
    assert 'def power_down_protected_soc(' in source
    assert 'async def power_down_info(' in source
    assert "joined_events" in source
    assert "forecast_no_slots" in source
    assert '0.95' in source
