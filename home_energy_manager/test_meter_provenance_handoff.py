from pathlib import Path

APP = (Path(__file__).parent / "components/controller/controller_discovery.py").read_text()


def test_controller_prefers_top_level_metering_provenance():
    assert "metering=attrs.get('metering')" in APP
    assert "controller.c['grid_export_meter_source']=meter_export_source or ci_export_source or 'battery'" in APP
    assert "controller.c['grid_import_meter_source']=meter_import_source or ci_import_source or 'battery'" in APP
    assert "controller.c['grid_export_energy_total_entity']=meter_export_entity" in APP
    assert "controller.c['grid_import_energy_total_entity']=meter_import_entity" in APP
    assert "Forecast meter provenance mismatch for export" in APP
