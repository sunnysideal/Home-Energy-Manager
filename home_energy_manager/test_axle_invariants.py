from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADAPTER = (ROOT / "components" / "axle" / "main.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "components" / "controller" / "axle_only.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "launcher.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.yaml").read_text(encoding="utf-8")


def test_axle_source_is_hacs_entities_not_direct_api():
    for entity in ("sensor.axle_start_time", "sensor.axle_end_time", "sensor.axle_import_export", "sensor.axle_updated_at"):
        assert entity in ADAPTER
    assert "sensor.home_energy_manager_axle" in ADAPTER
    assert "api.axle" not in ADAPTER.lower()
    assert "axle token" not in ADAPTER.lower()


def test_axle_only_does_not_load_normal_planner():
    assert 'mode == "axle_only"' in LAUNCHER
    assert '"/app/runtime/controller/axle_only.py"' in LAUNCHER
    assert "maximise_export" not in CONTROLLER
    assert "minimise_export" not in CONTROLLER
    assert "export_generated" not in CONTROLLER


def test_import_events_do_not_enter_action_path():
    assert 'event_type == "export"' in CONTROLLER


def test_event_energy_uses_max_discharge_duration_efficiency_and_reserve():
    assert "max_discharge / 1000.0" in CONTROLLER
    assert "duration_h" in CONTROLLER
    assert "discharge_eff" in CONTROLLER
    assert "raw_required_soc = reserve + required_kwh / capacity * 100.0" in CONTROLLER
    assert "required_soc = min(100.0, raw_required_soc)" in CONTROLLER


def test_active_event_uses_max_discharge_and_reserve_target():
    assert 'ents.get("discharge_slot_1_target")' in CONTROLLER
    assert 'ents.get("discharge_rate")' in CONTROLLER
    assert "max_discharge" in CONTROLLER
    assert "reserve" in CONTROLLER


def test_temporary_settings_are_snapshotted_and_restored():
    assert "capture_snapshot" in CONTROLLER
    assert "restore_snapshot" in CONTROLLER
    assert "await self.restore_snapshot()" in CONTROLLER


def test_public_mode_and_axle_entity_are_configured():
    assert "forecast_only|axle_only" in CONFIG
    assert "sensor.home_energy_manager_axle" in CONFIG
