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
import urllib.error
import urllib.request
from pathlib import Path

OPTIONS = Path("/data/options.json")
DATA_DIR = Path("/data")
ADDON_CONFIG_DIR = Path("/config")
MIGRATION_BUNDLE = ADDON_CONFIG_DIR / "home_energy_manager_migration_v1.zip"
MIGRATION_MARKER = DATA_DIR / "migration_imported.json"
MIGRATION_OPTIONS_NAME = "options.json"
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

def _supervisor_request(path: str, method: str = "GET", payload=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable; cannot restore app configuration")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://supervisor" + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Supervisor API {path} failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Supervisor API {path} failed: {exc}") from exc
    if not raw:
        return {}
    return json.loads(raw)

def _restore_supervisor_options(options: dict) -> None:
    if not isinstance(options, dict):
        raise RuntimeError("Migration options payload is not an object")
    restored = json.loads(json.dumps(options))
    restored["migration_action"] = "none"
    validation = _supervisor_request("/addons/self/options/validate", "POST", restored)
    data = validation.get("data", validation) if isinstance(validation, dict) else {}
    if isinstance(data, dict) and data.get("valid") is False:
        raise RuntimeError(f"Migrated app configuration is invalid: {data.get('message') or validation}")
    _supervisor_request("/addons/self/options", "POST", {"options": restored})
    print("[manager] Supervisor app configuration restored; migration_action reset to none", flush=True)

def export_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE, options_path: Path = OPTIONS) -> Path:
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
        if not options_path.exists():
            raise RuntimeError(f"App configuration not found: {options_path}")
        options = json.loads(options_path.read_text())
        if not isinstance(options, dict):
            raise RuntimeError("Current app configuration is not a JSON object")
        options_copy = tmp / MIGRATION_OPTIONS_NAME
        options_copy.write_text(json.dumps(options, indent=2) + "\n")
        manifest = {
            "format_version": 2,
            "home_energy_manager_version": "0.1.17",
            "files": files,
            "options": {
                "name": MIGRATION_OPTIONS_NAME,
                "size": options_copy.stat().st_size,
                "sha256": _sha256(options_copy),
            },
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        temp_bundle = bundle.with_suffix(bundle.suffix + ".tmp")
        if temp_bundle.exists():
            temp_bundle.unlink()
        with zipfile.ZipFile(temp_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp / "manifest.json", "manifest.json")
            for item in files:
                zf.write(tmp / item["name"], item["name"])
            zf.write(options_copy, MIGRATION_OPTIONS_NAME)
        temp_bundle.replace(bundle)
    print(f"[manager] migration export created: {bundle}", flush=True)
    return bundle

def import_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE, marker: Path = MIGRATION_MARKER) -> None:
    if not bundle.exists():
        raise RuntimeError(f"Migration bundle not found: {bundle}")
    with tempfile.TemporaryDirectory(prefix="hem-migration-import-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(bundle, "r") as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                raise RuntimeError("Migration bundle has no manifest.json")
            manifest = json.loads(zf.read("manifest.json"))
            format_version = manifest.get("format_version")
            if format_version not in (1, 2):
                raise RuntimeError(f"Unsupported migration format: {format_version}")
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise RuntimeError("Migration manifest files field is invalid")
            extracted = []
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
                extracted.append(name)

            imported_options = None
            options_meta = manifest.get("options") if format_version >= 2 else None
            if options_meta:
                if not isinstance(options_meta, dict) or options_meta.get("name") != MIGRATION_OPTIONS_NAME or MIGRATION_OPTIONS_NAME not in names:
                    raise RuntimeError("Invalid migration options entry")
                target = tmp / MIGRATION_OPTIONS_NAME
                with zf.open(MIGRATION_OPTIONS_NAME) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if target.stat().st_size != int(options_meta.get("size", -1)):
                    raise RuntimeError("Size mismatch for migration options")
                if _sha256(target) != options_meta.get("sha256"):
                    raise RuntimeError("Checksum mismatch for migration options")
                imported_options = json.loads(target.read_text())
                if not isinstance(imported_options, dict):
                    raise RuntimeError("Migration options payload is not an object")

        restored = []
        if marker.exists():
            print(f"[manager] learned-state migration already completed; preserving existing /data state", flush=True)
        else:
            existing = [name for name in MIGRATION_FILES if (data_dir / name).exists()]
            if existing:
                raise RuntimeError(
                    "Refusing learned-state migration because files already exist in /data without a migration marker: "
                    + ", ".join(existing)
                )
            data_dir.mkdir(parents=True, exist_ok=True)
            for name in extracted:
                src = tmp / name
                dst = data_dir / name
                temp_dst = data_dir / (name + ".migration_tmp")
                shutil.copy2(src, temp_dst)
                temp_dst.replace(dst)
                restored.append(name)
            marker.write_text(json.dumps({
                "format_version": format_version,
                "imported_by_version": "0.1.17",
                "bundle": str(bundle),
                "files": restored,
            }, indent=2) + "\n")
            print(f"[manager] migration import completed: {', '.join(restored) if restored else 'no learned-state files'}", flush=True)

        if imported_options is None:
            raise RuntimeError("Migration bundle does not contain app configuration; export again with local v0.1.17")
        _restore_supervisor_options(imported_options)

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
        print("[manager] export complete; no components started", flush=True)
        return "stop"
    if action == "import":
        import_migration_bundle()
        print("[manager] import complete; no components started. Start again to run normally.", flush=True)
        return "stop"
    raise RuntimeError(f"Unknown migration_action: {action}")

def load_and_split_options():
    raw = json.loads(OPTIONS.read_text())
    migration_result = handle_migration(raw)
    if migration_result == "stop":
        return False
    mqtt = raw.get("mqtt") if isinstance(raw.get("mqtt"), dict) else {}
    os.environ["HOME_ENERGY_MQTT_CONFIG"] = json.dumps(mqtt, separators=(",", ":"))
    os.environ["HOME_ENERGY_MANAGER_VERSION"] = "0.1.17"
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
