from __future__ import annotations

import re
from pathlib import Path

PUBLIC_LOG_PATH = Path("/config/www/home_energy_manager/latest.log")
PUBLIC_LOG_MAX_BYTES = 2 * 1024 * 1024

_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:password|passwd|token|access_token|refresh_token|api_key|apikey|secret)\b\s*[=:]\s*)[^\s&,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|access_token|refresh_token|api_key|apikey|secret)=)[^&#\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:mqtt|mqtts|http|https)://[^\s:/@]+:)[^\s/@]+(@)"), r"\1[REDACTED]\2"),
)


def sanitise_public_log_text(text: str) -> str:
    clean = text
    for pattern, replacement in _PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean


class PublicLogFile:
    def __init__(self, path: Path = PUBLIC_LOG_PATH, max_bytes: int = PUBLIC_LOG_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max(256, int(max_bytes))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("")

    def write(self, text: str) -> None:
        clean = sanitise_public_log_text(text)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(clean)
        if self.path.stat().st_size > self.max_bytes:
            self._trim()

    def _trim(self) -> None:
        data = self.path.read_bytes()
        marker = b"[manager] public log trimmed to most recent output\n"
        budget = max(0, self.max_bytes - len(marker))
        tail = data[-budget:] if budget else b""
        if tail:
            newline = tail.find(b"\n")
            if newline >= 0:
                tail = tail[newline + 1 :]
        self.path.write_bytes(marker + tail)
