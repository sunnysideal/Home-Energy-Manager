#!/usr/bin/env python3
"""ASHP component process supervisor.

Runs the existing forecaster unchanged together with the passive DHW sampler.  This
keeps the merged add-on's top-level launcher unaware of ASHP-internal helper processes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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


def main() -> None:
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)
    env = os.environ.copy()
    commands = [
        [sys.executable, "-u", str(ROOT / "main.py")],
        [sys.executable, "-u", str(ROOT / "dhw_collector.py")],
    ]
    for command in commands:
        children.append(subprocess.Popen(command, env=env))

    while True:
        for proc in children:
            rc = proc.poll()
            if rc is not None:
                print(f"[ashp_forecaster] child exited with code {rc}; stopping ASHP component", flush=True)
                stop_all()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
