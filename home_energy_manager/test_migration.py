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


def test_export_import_roundtrip(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(); target.mkdir()
    make_db(source / "controller.db")
    make_db(source / "ashp_forecast.db")
    make_db(source / "home_energy_forecaster.db")
    (source / "last_forecast.json").write_text('{"ok": true}')
    bundle = tmp_path / "migration.zip"
    marker = target / "migration_imported.json"

    launcher.export_migration_bundle(source, bundle)
    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert {x["name"] for x in manifest["files"]} == set(launcher.MIGRATION_FILES)

    launcher.import_migration_bundle(target, bundle, marker)
    assert marker.exists()
    for name in launcher.MIGRATION_FILES:
        assert (target / name).exists()
    conn = sqlite3.connect(target / "controller.db")
    assert conn.execute("select value from sample").fetchone()[0] == "learned"
    conn.close()


def test_import_refuses_existing_state(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    make_db(source / "controller.db")
    bundle = tmp_path / "migration.zip"
    launcher.export_migration_bundle(source, bundle)
    target = tmp_path / "target"; target.mkdir()
    make_db(target / "controller.db")
    try:
        launcher.import_migration_bundle(target, bundle, target / "marker.json")
    except RuntimeError as exc:
        assert "Refusing migration import" in str(exc)
    else:
        raise AssertionError("import should refuse existing state")
