import json
import sqlite3
import zipfile
from pathlib import Path

import launcher


def make_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("create table sample(value text)")
    conn.execute("insert into sample values ('learned')")
    conn.commit()
    conn.close()


def write_options(path: Path, action="export"):
    path.write_text(json.dumps({
        "migration_action": action,
        "controller": {"operation_mode": "maximise_export"},
        "mqtt": {"enabled": True, "host": "mqtt.example", "password": "secret"},
    }))


def test_export_import_roundtrip_with_options(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(); target.mkdir()
    make_db(source / "controller.db")
    make_db(source / "ashp_forecast.db")
    make_db(source / "home_energy_forecaster.db")
    (source / "last_forecast.json").write_text('{"ok": true}')
    options = tmp_path / "options.json"
    write_options(options)
    bundle = tmp_path / "migration.zip"
    marker = target / "migration_imported.json"

    launcher.export_migration_bundle(source, bundle, options)
    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format_version"] == 2
        assert {x["name"] for x in manifest["files"]} == set(launcher.MIGRATION_FILES)
        exported_options = json.loads(zf.read("options.json"))
        assert exported_options["mqtt"]["password"] == "secret"

    restored = {}
    monkeypatch.setattr(launcher, "_restore_supervisor_options", lambda value: restored.update(value))
    launcher.import_migration_bundle(target, bundle, marker)
    assert marker.exists()
    for name in launcher.MIGRATION_FILES:
        assert (target / name).exists()
    conn = sqlite3.connect(target / "controller.db")
    assert conn.execute("select value from sample").fetchone()[0] == "learned"
    conn.close()
    assert restored["controller"]["operation_mode"] == "maximise_export"


def test_import_refuses_existing_state_without_marker(tmp_path, monkeypatch):
    source = tmp_path / "source"; source.mkdir()
    make_db(source / "controller.db")
    options = tmp_path / "options.json"; write_options(options)
    bundle = tmp_path / "migration.zip"
    launcher.export_migration_bundle(source, bundle, options)
    target = tmp_path / "target"; target.mkdir()
    make_db(target / "controller.db")
    monkeypatch.setattr(launcher, "_restore_supervisor_options", lambda value: None)
    try:
        launcher.import_migration_bundle(target, bundle, target / "marker.json")
    except RuntimeError as exc:
        assert "without a migration marker" in str(exc)
    else:
        raise AssertionError("import should refuse existing state without marker")


def test_existing_v016_migration_marker_skips_databases_but_restores_options(tmp_path, monkeypatch):
    source = tmp_path / "source"; source.mkdir()
    make_db(source / "controller.db")
    options = tmp_path / "options.json"; write_options(options)
    bundle = tmp_path / "migration.zip"
    launcher.export_migration_bundle(source, bundle, options)

    target = tmp_path / "target"; target.mkdir()
    make_db(target / "controller.db")
    marker = target / "migration_imported.json"
    marker.write_text('{"imported_by_version":"0.1.16"}')
    restored = {}
    monkeypatch.setattr(launcher, "_restore_supervisor_options", lambda value: restored.update(value))

    launcher.import_migration_bundle(target, bundle, marker)
    conn = sqlite3.connect(target / "controller.db")
    assert conn.execute("select count(*) from sample").fetchone()[0] == 1
    conn.close()
    assert restored["mqtt"]["host"] == "mqtt.example"


def test_restore_supervisor_options_validates_and_resets_action(monkeypatch):
    calls = []
    def fake(path, method="GET", payload=None):
        calls.append((path, method, payload))
        if path.endswith("/validate"):
            return {"data": {"valid": True}}
        return {"result": "ok"}
    monkeypatch.setattr(launcher, "_supervisor_request", fake)
    launcher._restore_supervisor_options({"migration_action": "export", "controller": {"operation_mode": "maximise_export"}})
    assert calls[0][0] == "/addons/self/options/validate"
    assert calls[1][0] == "/addons/self/options"
    assert calls[1][2]["options"]["migration_action"] == "none"


def test_bootstrap_only_creates_config_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "addon_config"
    monkeypatch.setattr(launcher, "ADDON_CONFIG_DIR", config_dir)
    result = launcher.handle_migration({"migration_action": "bootstrap"})
    assert result == "stop"
    assert config_dir.is_dir()
    assert not any(tmp_path.glob("*.db"))
