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


def _setdefault_from(target: dict, key: str, source: dict, source_key: str | None = None, default=None) -> None:
    source_key = source_key or key
    if key not in target:
        target[key] = source.get(source_key, default)


def _normalise_home_forecaster(hf: dict) -> None:
    """Normalize merged/standalone config variants to the v0.2.14 runtime contract.

    v0.1.31 accidentally shipped the standalone-style Home Forecaster schema in the
    merged add-on. Preserve users' saved values from that release while restoring the
    canonical merged runtime structure expected by components/home_forecaster/app/main.py.
    """
    settings = hf.setdefault("settings", {})
    for key, default in (
        ("timezone", "Europe/London"),
        ("history_days", 28),
        ("day_of_week_weighting", True),
        ("same_weekday_weight", 1.5),
        ("forecast_interval_minutes", 5),
        ("controller_refresh_request_entity", "sensor.home_energy_forecast_refresh_request"),
        ("charge_efficiency", 0.95),
        ("discharge_efficiency", 0.95),
        ("minimum_baseline_w", 200),
        ("log_level", "INFO"),
    ):
        _setdefault_from(settings, key, hf, default=default)

    battery = hf.setdefault("battery", {})
    for key in ("charge_target_1", "charge_target_2", "discharge_target_1", "discharge_target_2"):
        standalone_key = key.replace("_target_", "_target_soc_")
        _setdefault_from(battery, key, battery, standalone_key, "")

    load = hf.setdefault("load", {})
    load.setdefault("energy_total_kwh", "")

    pv = hf.get("pv") if isinstance(hf.get("pv"), dict) else {}
    solar = hf.setdefault("solar", {})
    _setdefault_from(solar, "energy_total_kwh", pv, default="")
    _setdefault_from(solar, "solcast_today", pv, default="")
    _setdefault_from(solar, "solcast_tomorrow", pv, default="")
    _setdefault_from(solar, "solcast_day_3", pv, "solcast_day3", "")

    grid = hf.get("grid") if isinstance(hf.get("grid"), dict) else {}
    meter = hf.setdefault("meter", {})
    _setdefault_from(meter, "import_energy_total_kwh", grid, default="")
    _setdefault_from(meter, "export_energy_total_kwh", grid, default="")

    tariff = hf.setdefault("tariff", {})
    _setdefault_from(tariff, "import_energy_total_kwh", grid, "inverter_import_energy_total_kwh", "")
    _setdefault_from(tariff, "export_energy_total_kwh", grid, "inverter_export_energy_total_kwh", "")
    _setdefault_from(tariff, "import_current_rate", tariff, "import_rate", "")
    _setdefault_from(tariff, "export_current_rate", tariff, "export_rate", "")
    tariff.setdefault("import_current_day_rates", "")
    tariff.setdefault("import_next_day_rates", "")
    tariff.setdefault("export_current_day_rates", "")
    tariff.setdefault("export_next_day_rates", "")

    ashp = hf.setdefault("ashp", {})
    _setdefault_from(ashp, "forecast_48h", ashp, "forecast_entity", "sensor.ashp_forecast_next_48h")
    ashp.setdefault("ch_energy_total_kwh", "sensor.ashp_electrical_energy_ch")
    ashp.setdefault("dhw_energy_total_kwh", "sensor.ashp_electrical_energy_dhw")

    ev = hf.setdefault("ev", {})
    old_load = hf.get("load") if isinstance(hf.get("load"), dict) else {}
    old_tariff = hf.get("tariff") if isinstance(hf.get("tariff"), dict) else {}
    ev.setdefault("smart_charging_active_entity", old_load.get("ev_charging_entity", ""))
    ev.setdefault("smart_charging_dispatch_entity", old_tariff.get("intelligent_dispatching", ""))
    ev.setdefault("energy_total_kwh", "")


def load_and_split_options():
    raw = json.loads(OPTIONS.read_text())
    hf = raw.get("home_forecaster") if isinstance(raw.get("home_forecaster"), dict) else {}
    if hf:
        _normalise_home_forecaster(hf)

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
    os.environ["HOME_ENERGY_MANAGER_VERSION"] = "0.1.37"
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
