from pathlib import Path

ROOT = Path(__file__).parent / "components/controller"
APP=(ROOT/"app.py").read_text() + "\n" + (ROOT/"controller_legacy_core.py").read_text()

def test_export_generated_uses_current_forecast_metering():
    assert "metering=attrs.get('metering')" in APP
    assert "metering.get('export_source') or vals.get('grid_export_meter_source')" in APP
    assert "metering.get('export_entity')" in APP
    assert "'export_meter_entity':meter_entity" in APP
