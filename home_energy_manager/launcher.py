#!/usr/bin/env python3
import json
import hashlib
import shutil
import sqlite3
import tempfile
import zipfile
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

OPTIONS = Path("/data/options.json")
DATA_DIR = Path("/data")
ADDON_CONFIG_DIR = Path("/config")
MIGRATION_BUNDLE = ADDON_CONFIG_DIR / "home_energy_manager_migration_v1.zip"
MIGRATION_MARKER = DATA_DIR / "migration_imported.json"
MIGRATION_FILES = (
    "controller.db",
    "ashp_forecast.db",
    "home_energy_forecaster.db",
    "last_forecast.json",
)
COMPONENT_OPTIONS = {
    "ashp_forecaster": Path("/data/options_ashp_forecaster.json"),
    "home_forecaster": Path("/data/options_home_forecaster.json"),
    "controller": Path("/data/options_controller.json"),
}

PROCESSES = [
    ("ashp_forecaster", [sys.executable, "-u", "/app/runtime/ashp_forecaster/main.py"]),
    ("home_forecaster", [sys.executable, "-u", "/app/runtime/home_forecaster/main.py"]),
    ("controller", [sys.executable, "-u", "/app/runtime/controller/app.py"]),
]

children = []
stopping = False

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _sqlite_integrity(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not row or row[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path.name}: {row[0] if row else 'no result'}")

def _sqlite_backup(source: Path, destination: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _sqlite_integrity(destination)

def export_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE) -> Path:
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hem-migration-export-") as tmp_name:
        tmp = Path(tmp_name)
        files = []
        for name in MIGRATION_FILES:
            source = data_dir / name
            if not source.exists():
                continue
            destination = tmp / name
            if name.endswith(".db"):
                _sqlite_backup(source, destination)
            else:
                shutil.copy2(source, destination)
            files.append({
                "name": name,
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            })
        if not files:
            raise RuntimeError("No learned-state files exist in /data; nothing to export")
        manifest = {
            "format_version": 1,
            "home_energy_manager_version": "0.1.16",
            "files": files,
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        temp_bundle = bundle.with_suffix(bundle.suffix + ".tmp")
        if temp_bundle.exists():
            temp_bundle.unlink()
        with zipfile.ZipFile(temp_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp / "manifest.json", "manifest.json")
            for item in files:
                zf.write(tmp / item["name"], item["name"])
        temp_bundle.replace(bundle)
    print(f"[manager] migration export created: {bundle}", flush=True)
    return bundle

def import_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE, marker: Path = MIGRATION_MARKER) -> None:
    if marker.exists():
        print(f"[manager] migration import already completed; marker={marker}", flush=True)
        return
    if not bundle.exists():
        raise RuntimeError(f"Migration bundle not found: {bundle}")
    existing = [name for name in MIGRATION_FILES if (data_dir / name).exists()]
    if existing:
        raise RuntimeError(
            "Refusing migration import because learned-state files already exist in /data: " + ", ".join(existing)
        )
    with tempfile.TemporaryDirectory(prefix="hem-migration-import-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(bundle, "r") as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                raise RuntimeError("Migration bundle has no manifest.json")
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format_version") != 1:
                raise RuntimeError(f"Unsupported migration format: {manifest.get('format_version')}")
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries:
                raise RuntimeError("Migration manifest contains no files")
            for item in entries:
                name = item.get("name") if isinstance(item, dict) else None
                if name not in MIGRATION_FILES or name not in names:
                    raise RuntimeError(f"Invalid migration file entry: {name!r}")
                target = tmp / name
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if target.stat().st_size != int(item.get("size", -1)):
                    raise RuntimeError(f"Size mismatch for migration file {name}")
                if _sha256(target) != item.get("sha256"):
                    raise RuntimeError(f"Checksum mismatch for migration file {name}")
                if name.endswith(".db"):
                    _sqlite_integrity(target)
        data_dir.mkdir(parents=True, exist_ok=True)
        restored = []
        for item in entries:
            name = item["name"]
            src = tmp / name
            dst = data_dir / name
            temp_dst = data_dir / (name + ".migration_tmp")
            shutil.copy2(src, temp_dst)
            temp_dst.replace(dst)
            restored.append(name)
        marker.write_text(json.dumps({
            "format_version": 1,
            "imported_by_version": "0.1.16",
            "bundle": str(bundle),
            "files": restored,
        }, indent=2) + "\n")
    print(f"[manager] migration import completed: {', '.join(restored)}", flush=True)

def handle_migration(raw: dict) -> None:
    action = str(raw.get("migration_action", "none") or "none").strip().lower()
    if action == "none":
        return "continue"
    if action == "bootstrap":
        ADDON_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[manager] migration bootstrap ready: {ADDON_CONFIG_DIR}", flush=True)
        print("[manager] no components started and no learned-state databases created", flush=True)
        return "stop"
    if action == "export":
        export_migration_bundle()
        return "continue"
    if action == "import":
        import_migration_bundle()
        return "continue"
    raise RuntimeError(f"Unknown migration_action: {action}")

def load_and_split_options():
    raw = json.loads(OPTIONS.read_text())
    migration_result = handle_migration(raw)
    if migration_result == "stop":
        return False
    mqtt = raw.get("mqtt") if isinstance(raw.get("mqtt"), dict) else {}
    os.environ["HOME_ENERGY_MQTT_CONFIG"] = json.dumps(mqtt, separators=(",", ":"))
    os.environ["HOME_ENERGY_MANAGER_VERSION"] = "0.1.16"
    for name, path in COMPONENT_OPTIONS.items():
        section = raw.get(name)
        if not isinstance(section, dict):
            raise RuntimeError(f"Missing or invalid configuration section: {name}")
        path.write_text(json.dumps(section, separators=(",", ":")))
    return True

def stop_all(signum=None, frame=None):
    global stopping
    if stopping:
        return
    stopping = True
    print("[manager] shutdown requested", flush=True)
    for name, proc in reversed(children):
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 15
    for name, proc in reversed(children):
        if proc.poll() is None:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
    sys.exit(0 if signum is not None else 1)

def main():
    if not load_and_split_options():
        return
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    # Ordered startup preserves the existing pipeline without coupling the code:
    # ASHP forecast -> home forecast -> controller.
    for name, cmd in PROCESSES:
        env = os.environ.copy()
        env["OPTIONS_PATH"] = str(COMPONENT_OPTIONS[name])
        env["PYTHONPATH"] = "/app" + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        print(f"[manager] starting {name}", flush=True)
        proc = subprocess.Popen(cmd, env=env)
        children.append((name, proc))
        time.sleep(1.0)

    while True:
        for name, proc in children:
            rc = proc.poll()
            if rc is not None:
                print(f"[manager] component {name} exited with code {rc}; stopping package", flush=True)
                stop_all()
        time.sleep(1.0)

if __name__ == "__main__":
    main()
