#!/usr/bin/env python3
"""ASHP component process supervisor.

Runs the authoritative legacy forecast model through the horizon-preserving entrypoint
together with passive DHW sampling/learning, shadow forecasting, validation and
diagnostics processes. The production forecaster receives only the configuration fields
its legacy dataclass understands; passive helpers receive the full ASHP configuration.
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
LEGACY_OPTIONS = Path("/data/options_ashp_forecaster_legacy.json")
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


def _legacy_options_path(full_options_path: Path) -> Path:
    raw = json.loads(full_options_path.read_text())
    legacy = {key: value for key, value in raw.items() if key not in THERMAL_ONLY_KEYS}
    LEGACY_OPTIONS.write_text(json.dumps(legacy, separators=(",", ":")))
    return LEGACY_OPTIONS


def main() -> None:
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    full_options_path = Path(os.environ.get("OPTIONS_PATH", "/data/options_ashp_forecaster.json"))
    legacy_options_path = _legacy_options_path(full_options_path)

    forecast_env = os.environ.copy()
    forecast_env["OPTIONS_PATH"] = str(legacy_options_path)
    passive_env = os.environ.copy()
    passive_env["OPTIONS_PATH"] = str(full_options_path)

    children.append(subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "forecast_runner.py")], env=forecast_env
    ))
    children.append(subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "dhw_collector.py")], env=passive_env
    ))
    # One-shot startup learning runs alongside the collector. It waits for the collector's
    # schema/bootstrap data, then exits after a learning pass. This removes the restart-to-
    # hourly-pass delay while keeping the collector as owner of ongoing hourly retraining.
    startup_learning = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "dhw_startup_learning.py")], env=passive_env
    )
    children.append(subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "dhw_shadow_runner.py")], env=passive_env
    ))
    children.append(subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "dhw_validation_runner.py")], env=passive_env
    ))
    children.append(subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "dhw_diagnostics_runner.py")], env=passive_env
    ))

    while True:
        startup_rc = startup_learning.poll()
        if startup_rc not in (None, 0):
            print(f"[ashp_forecaster] startup DHW learner exited with code {startup_rc}", flush=True)
            stop_all()
        for proc in children:
            rc = proc.poll()
            if rc is not None:
                print(f"[ashp_forecaster] child exited with code {rc}; stopping ASHP component", flush=True)
                stop_all()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
