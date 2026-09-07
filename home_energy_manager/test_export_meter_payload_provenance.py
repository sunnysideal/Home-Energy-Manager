from pathlib import Path

APP=(Path(__file__).parent/"components/controller/app.py").read_text()

def test_export_generated_uses_current_forecast_metering():
    assert "metering=attrs.get('metering')" in APP
    assert "metering.get('export_source') or vals.get('grid_export_meter_source')" in APP
    assert "metering.get('export_entity')" in APP
    assert "'export_meter_entity':meter_entity" in APP
