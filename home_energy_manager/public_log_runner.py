#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys

from public_log import PUBLIC_LOG_PATH, PublicLogFile


def main() -> int:
    public_log = PublicLogFile()
    banner = f"[manager] public diagnostic log enabled at {PUBLIC_LOG_PATH} (Home Assistant /local/home_energy_manager/latest.log)\n"
    sys.stdout.write(banner)
    sys.stdout.flush()
    public_log.write(banner)

    proc = subprocess.Popen(
        [sys.executable, "-u", "/app/launcher.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            public_log.write(line)
    except KeyboardInterrupt:
        proc.terminate()
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
