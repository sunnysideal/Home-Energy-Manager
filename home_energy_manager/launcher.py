#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

OPTIONS = Path("/data/options.json")
COMPONENT_OPTIONS = {
    "ashp_forecaster": Path("/data/options_ashp_forecaster.json"),
    "home_forecaster": Path("/data/options_home_forecaster.json"),
    "controller": Path("/data/options_controller.json"),
}

children = []
stopping = False


def load_and_split_options():
    raw = json.loads(OPTIONS.read_text())
    # One-release compatibility aliases for pre-v0.1.20 supplier-specific EV settings.
    hf = raw.get("home_forecaster") if isinstance(raw.get("home_forecaster"), dict) else {}
    if hf:
        ev = hf.setdefault("ev", {})
        old_load = hf.get("load") if isinstance(hf.get("load"), dict) else {}
        old_tariff = hf.get("tariff") if isinstance(hf.get("tariff"), dict) else {}
        ev.setdefault("smart_charging_active_entity", old_load.get("ev_charging_entity", ""))
        ev.setdefault("smart_charging_dispatch_entity", old_tariff.get("intelligent_dispatching", ""))
        ev.setdefault("energy_total_kwh", "")
    ctl = raw.get("controller") if isinstance(raw.get("controller"), dict) else {}
    if ctl:
        if "ev_smart_charging_enabled" not in ctl and "intelligent_go_enabled" in ctl:
            ctl["ev_smart_charging_enabled"] = ctl.get("intelligent_go_enabled")
        if "ev_smart_charging_dispatch_entity" not in ctl:
            ctl["ev_smart_charging_dispatch_entity"] = ctl.get("intelligent_dispatch_entity", "")
        if "ev_smart_charging_active_entity" not in ctl:
            ctl["ev_smart_charging_active_entity"] = ctl.get("intelligent_car_charging_entity", "")
    mqtt = raw.get("mqtt") if isinstance(raw.get("mqtt"), dict) else {}
    os.environ["HOME_ENERGY_MQTT_CONFIG"] = json.dumps(mqtt, separators=(",", ":"))
    os.environ["HOME_ENERGY_MANAGER_VERSION"] = "0.1.29"
    for name, path in COMPONENT_OPTIONS.items():
        section = raw.get(name)
        if not isinstance(section, dict):
            raise RuntimeError(f"Missing or invalid configuration section: {name}")
        path.write_text(json.dumps(section, separators=(",", ":")))
    return raw


def process_list(raw):
    controller = raw.get("controller") if isinstance(raw.get("controller"), dict) else {}
    mode = str(controller.get("operation_mode", "maximise_export"))
    controller_script = (
        "/app/runtime/controller/forecast_only.py"
        if mode == "forecast_only"
        else "/app/runtime/controller/app.py"
    )
    if mode == "forecast_only":
        print(
            "[manager] Forecast Only selected: active controller will not be started; "
            "passive controller has no inverter-write implementation",
            flush=True,
        )
    return [
        ("ashp_forecaster", [sys.executable, "-u", "/app/runtime/ashp_forecaster/runner.py"]),
        ("home_forecaster", [sys.executable, "-u", "/app/runtime/home_forecaster/main.py"]),
        ("controller", [sys.executable, "-u", controller_script]),
    ]


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
    raw = load_and_split_options()
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    # Ordered startup preserves the existing pipeline without coupling the code:
    # ASHP forecast -> home forecast -> controller/passive controller.
    for name, cmd in process_list(raw):
        env = os.environ.copy()
        env["OPTIONS_PATH"] = str(COMPONENT_OPTIONS[name])
        env["PYTHONPATH"] = "/app" + ((os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
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
