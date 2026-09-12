from pathlib import Path

ROOT=Path(__file__).parent
CONTROLLER=ROOT/'components'/'controller'

def test_battery_module_is_packaged():
    docker=(ROOT/'Dockerfile').read_text()
    assert 'components/controller/controller_battery.py /app/runtime/controller/controller_battery.py' in docker

def test_composition_root_delegates_battery_helpers():
    app=(CONTROLLER/'app.py').read_text()
    assert 'from controller_battery import (' in app
    for name in ('soc_at','forecast_battery_kwh_between','planned_discharge_soc_adjustment','projected_charge_start_soc','latest_charge_start_for_rate','choose_rate_and_start','band_factor','dwell','charge_minutes','choose_rate'):
        assert f'def {name}(' in app

def test_extracted_module_contains_bounded_battery_calculations():
    source=(CONTROLLER/'controller_battery.py').read_text()
    for name in ('soc_at','forecast_battery_kwh_between','planned_discharge_soc_adjustment','projected_charge_start_soc','latest_charge_start_for_rate','choose_rate_and_start','band_factor','dwell','charge_minutes','choose_rate'):
        assert f'def {name}(' in source
    assert '.ha.write(' not in source
    assert 'operation_mode' not in source
