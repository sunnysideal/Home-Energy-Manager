#!/usr/bin/env python3
"""ASHP component process supervisor.

Runs CH forecasting together with the authoritative thermal DHW sampling, learning,
forecasting, validation and diagnostics processes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE_OPTIONS = Path("/data/options_ashp_forecaster_core.json")
THERMAL_ONLY_KEYS = {
    "dhw_tank_upper_temperature_entity",
    "dhw_tank_lower_temperature_entity",
    "dhw_tank_volume_l",
    "dhw_ambient_temperature_entity",
    "dhw_min_usable_temperature_c",
    "dhw_thermal_sample_minutes",
}
children: list[subprocess.Popen] = []
stopping = False


def stop_all(signum=None, frame=None) -> None:
    global stopping
    if stopping:
        return
    stopping = True
    for proc in reversed(children):
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10.0
    for proc in reversed(children):
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
    raise SystemExit(0 if signum is not None else 1)


def _core_options_path(full_options_path: Path) -> Path:
    raw = json.loads(full_options_path.read_text())
    core = {key: value for key, value in raw.items() if key not in THERMAL_ONLY_KEYS}
    CORE_OPTIONS.write_text(json.dumps(core, separators=(",", ":")))
    return CORE_OPTIONS


def main() -> None:
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    full_options_path = Path(os.environ.get("OPTIONS_PATH", "/data/options_ashp_forecaster.json"))
    core_options_path = _core_options_path(full_options_path)

    forecast_env = os.environ.copy(); forecast_env["OPTIONS_PATH"] = str(core_options_path)
    thermal_env = os.environ.copy(); thermal_env["OPTIONS_PATH"] = str(full_options_path)

    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "forecast_runner.py")], env=forecast_env))
    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "dhw_collector.py")], env=thermal_env))
    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "dhw_startup_learning.py")], env=thermal_env))
    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "dhw_shadow_runner.py")], env=thermal_env))
    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "dhw_validation_runner.py")], env=thermal_env))
    children.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "dhw_diagnostics_runner.py")], env=thermal_env))

    while True:
        for proc in children:
            rc = proc.poll()
            if rc is not None:
                print(f"[ashp_forecaster] child exited with code {rc}; stopping ASHP component", flush=True)
                stop_all()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
