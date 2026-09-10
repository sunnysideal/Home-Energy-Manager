#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config_migration import migrate_runtime_options

OPTIONS = Path("/data/options.json")
DATA_DIR = Path("/data")
ADDON_CONFIG_DIR = Path("/config")
MIGRATION_BUNDLE = ADDON_CONFIG_DIR / "home_energy_manager_migration_v1.zip"
MIGRATION_MARKER = DATA_DIR / "migration_imported.json"
MIGRATION_FILES = ("controller.db", "ashp_forecast.db", "home_energy_forecaster.db", "last_forecast.json")
COMPONENT_OPTIONS = {
    "ashp_forecaster": Path("/data/options_ashp_forecaster.json"),
    "home_forecaster": Path("/data/options_home_forecaster.json"),
    "axle": Path("/data/options_axle.json"),
    "controller": Path("/data/options_controller.json"),
}
DEFAULT_AXLE_OPTIONS = {
    "enabled": True,
    "start_time_entity": "sensor.axle_vpp_axle_start_time",
    "end_time_entity": "sensor.axle_vpp_axle_end_time",
    "import_export_entity": "sensor.axle_vpp_axle_import_export",
    "window_state_entity": "sensor.axle_vpp_axle_event_window_state",
    "updated_at_entity": "sensor.axle_vpp_axle_updated_at",
    "publish_entity": "sensor.home_energy_manager_axle",
    "poll_seconds": 30,
}
HA_CONFIG_URL = "http://supervisor/core/api/config"
SUPERVISOR_BASE = "http://supervisor"
VERSION = "0.1.46"
children = []
stopping = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        dst.close(); src.close()
    _sqlite_integrity(destination)


def _supervisor_request(path: str, method: str = "GET", payload=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(f"{SUPERVISOR_BASE}{path}", data=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read(); return json.loads(raw.decode()) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Supervisor {method} {path} failed: {exc.code} {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Supervisor {method} {path} failed: {exc}") from exc


def _restore_supervisor_options(options: dict) -> None:
    restored = json.loads(json.dumps(options)); restored["migration_action"] = "none"
    validation = _supervisor_request("/addons/self/options/validate", "POST", {"options": restored})
    data = validation.get("data") if isinstance(validation, dict) else None
    if isinstance(data, dict) and data.get("valid") is False:
        raise RuntimeError(f"Migrated options failed Supervisor validation: {data}")
    _supervisor_request("/addons/self/options", "POST", {"options": restored})


def export_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE, options_path: Path = OPTIONS) -> Path:
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hem-migration-export-") as tmp_name:
        tmp = Path(tmp_name); files = []
        for name in MIGRATION_FILES:
            source = data_dir / name
            if not source.exists(): continue
            destination = tmp / name
            if name.endswith(".db"): _sqlite_backup(source, destination)
            else: shutil.copy2(source, destination)
            files.append({"name": name, "size": destination.stat().st_size, "sha256": _sha256(destination)})
        if not files: raise RuntimeError("No learned-state files exist in /data; nothing to export")
        manifest = {"format_version": 2, "home_energy_manager_version": VERSION, "files": files}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if options_path.exists(): shutil.copy2(options_path, tmp / "options.json")
        temp_bundle = bundle.with_suffix(bundle.suffix + ".tmp"); temp_bundle.unlink(missing_ok=True)
        with zipfile.ZipFile(temp_bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(tmp / "manifest.json", "manifest.json")
            for item in files: archive.write(tmp / item["name"], item["name"])
            if (tmp / "options.json").exists(): archive.write(tmp / "options.json", "options.json")
        temp_bundle.replace(bundle)
    print(f"[manager] migration export created: {bundle}", flush=True); return bundle


def import_migration_bundle(data_dir: Path = DATA_DIR, bundle: Path = MIGRATION_BUNDLE, marker: Path = MIGRATION_MARKER) -> None:
    if not bundle.exists(): raise RuntimeError(f"Migration bundle not found: {bundle}")
    marker_exists = marker.exists(); existing = [name for name in MIGRATION_FILES if (data_dir / name).exists()]
    if existing and not marker_exists:
        raise RuntimeError("Refusing migration import because learned-state files already exist without a migration marker: " + ", ".join(existing))
    with tempfile.TemporaryDirectory(prefix="hem-migration-import-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(bundle, "r") as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names: raise RuntimeError("Migration bundle has no manifest.json")
            manifest = json.loads(archive.read("manifest.json")); format_version = int(manifest.get("format_version", 0))
            if format_version not in (1, 2): raise RuntimeError(f"Unsupported migration format: {format_version}")
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries: raise RuntimeError("Migration manifest contains no files")
            for item in entries:
                name = item.get("name") if isinstance(item, dict) else None
                if name not in MIGRATION_FILES or name not in names: raise RuntimeError(f"Invalid migration file entry: {name!r}")
                target = tmp / name
                with archive.open(name) as src, target.open("wb") as dst: shutil.copyfileobj(src, dst)
                if target.stat().st_size != int(item.get("size", -1)): raise RuntimeError(f"Size mismatch for migration file {name}")
                if _sha256(target) != item.get("sha256"): raise RuntimeError(f"Checksum mismatch for migration file {name}")
                if name.endswith(".db"): _sqlite_integrity(target)
            migrated_options = json.loads(archive.read("options.json")) if format_version >= 2 and "options.json" in names else None
        restored = []
        if not marker_exists:
            data_dir.mkdir(parents=True, exist_ok=True)
            for item in entries:
                name = item["name"]; src = tmp / name; dst = data_dir / name; temp_dst = data_dir / (name + ".migration_tmp")
                shutil.copy2(src, temp_dst); temp_dst.replace(dst); restored.append(name)
            marker.write_text(json.dumps({"format_version": format_version, "imported_by_version": VERSION, "bundle": str(bundle), "files": restored}, indent=2) + "\n")
        else:
            print(f"[manager] learned-state migration already completed; marker={marker}", flush=True)
        if isinstance(migrated_options, dict): _restore_supervisor_options(migrated_options)
    if restored: print(f"[manager] migration import completed: {', '.join(restored)}", flush=True)


def handle_migration(raw: dict) -> str:
    action = str(raw.get("migration_action", "none") or "none").strip().lower()
    if action == "none": return "continue"
    if action == "bootstrap":
        ADDON_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[manager] migration bootstrap ready: {ADDON_CONFIG_DIR}", flush=True)
        print("[manager] no components started and no learned-state databases created", flush=True); return "stop"
    if action == "export": export_migration_bundle(); return "continue"
    if action == "import": import_migration_bundle(); return "continue"
    raise RuntimeError(f"Unknown migration_action: {action}")


def _normalise_home_forecaster(hf: dict) -> None:
    wrapper = {"home_forecaster": hf, "ashp_forecaster": {}, "axle": {}, "controller": {}, "mqtt": {}}
    runtime, _, _ = migrate_runtime_options(wrapper); hf.clear(); hf.update(runtime["home_forecaster"])


def _resolve_home_assistant_timezone(raw):
    hf = raw.get("home_forecaster") if isinstance(raw.get("home_forecaster"), dict) else {}
    settings = hf.setdefault("settings", {}) if isinstance(hf, dict) else {}
    fallback = str(settings.get("timezone") or "Europe/London"); token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print(f"[manager] Home Assistant timezone unavailable (no SUPERVISOR_TOKEN); using fallback {fallback}", flush=True); return fallback
    req = Request(HA_CONFIG_URL, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=15) as response: payload = json.loads(response.read().decode())
        timezone_name = str(payload.get("time_zone") or "").strip()
        if not timezone_name: raise ValueError("Home Assistant /config did not return time_zone")
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"[manager] Home Assistant timezone lookup failed; using fallback {fallback}: {exc}", flush=True); timezone_name = fallback
    settings["timezone"] = timezone_name
    controller = raw.get("controller") if isinstance(raw.get("controller"), dict) else {}
    controller["timezone"] = timezone_name
    print(f"[manager] runtime timezone={timezone_name} (Home Assistant)", flush=True); return timezone_name


def load_and_split_options():
    saved = json.loads(OPTIONS.read_text())
    if handle_migration(saved) == "stop": return None
    raw, canonical, diagnostics = migrate_runtime_options(saved); _resolve_home_assistant_timezone(raw)
    if not isinstance(raw.get("axle"), dict):
        raw["axle"] = dict(DEFAULT_AXLE_OPTIONS)
        print("[manager] Axle configuration absent in saved options; using HACS Axle defaults", flush=True)
    mqtt = raw.get("mqtt") if isinstance(raw.get("mqtt"), dict) else {}
    canonical_mqtt = canonical.get("advanced", {}).get("mqtt") if isinstance(canonical.get("advanced"), dict) else None
    if isinstance(canonical_mqtt, dict): mqtt = canonical_mqtt
    os.environ["HOME_ENERGY_MQTT_CONFIG"] = json.dumps(mqtt, separators=(",", ":"))
    os.environ["HOME_ENERGY_MANAGER_VERSION"] = VERSION
    print("[manager] config migration: " f"layout={diagnostics.source_layout} canonical=v{diagnostics.canonical_version} " f"moves={len(diagnostics.moves)} duplicates={len(diagnostics.duplicates)} conflicts={len(diagnostics.conflicts)}", flush=True)
    for message in diagnostics.conflicts: print(f"[manager] config conflict: {message}", flush=True)
    for name, path in COMPONENT_OPTIONS.items():
        section = raw.get(name)
        if not isinstance(section, dict): raise RuntimeError(f"Missing or invalid configuration section after migration: {name}")
        path.write_text(json.dumps(section, separators=(",", ":")))
    return raw


def process_list(raw):
    controller = raw.get("controller") if isinstance(raw.get("controller"), dict) else {}
    mode = str(controller.get("operation_mode", "maximise_export"))
    if mode == "forecast_only": controller_script = "/app/runtime/controller/forecast_only.py"
    elif mode == "axle_only": controller_script = "/app/runtime/controller/axle_only.py"
    else: controller_script = "/app/runtime/controller/app.py"
    if mode == "forecast_only":
        print("[manager] Forecast Only selected: active controller will not be started; passive controller has no inverter-write implementation", flush=True)
    elif mode == "axle_only":
        print("[manager] Axle Only selected: normal optimisation controller will not be started; writes are limited to Axle Export preparation/event handling", flush=True)
    return [
        ("ashp_forecaster", [sys.executable, "-u", "/app/runtime/ashp_forecaster/runner.py"]),
        ("home_forecaster", [sys.executable, "-u", "/app/runtime/home_forecaster/axle_pricing_runner.py"]),
        ("axle", [sys.executable, "-u", "/app/runtime/axle/main.py"]),
        ("controller", [sys.executable, "-u", controller_script]),
    ]


def stop_all(signum=None, frame=None):
    global stopping
    if stopping: return
    stopping = True; print("[manager] shutdown requested", flush=True)
    for name, proc in reversed(children):
        if proc.poll() is None:
            try: proc.terminate()
            except ProcessLookupError: pass
    deadline = time.monotonic() + 15
    for name, proc in reversed(children):
        if proc.poll() is None:
            remaining = max(0.1, deadline - time.monotonic())
            try: proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired: proc.kill()
    sys.exit(0 if signum is not None else 1)


def main():
    raw = load_and_split_options()
    if raw is None: return
    signal.signal(signal.SIGTERM, stop_all); signal.signal(signal.SIGINT, stop_all)
    for name, cmd in process_list(raw):
        env = os.environ.copy(); env["OPTIONS_PATH"] = str(COMPONENT_OPTIONS[name]); env["PYTHONPATH"] = "/app" + ((os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        print(f"[manager] starting {name}", flush=True); proc = subprocess.Popen(cmd, env=env); children.append((name, proc)); time.sleep(1.0)
    while True:
        for name, proc in children:
            rc = proc.poll()
            if rc is not None:
                print(f"[manager] component {name} exited with code {rc}; stopping package", flush=True); stop_all()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
