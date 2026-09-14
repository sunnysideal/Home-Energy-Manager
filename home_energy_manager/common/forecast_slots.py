"""Shared time-slot boundary helpers for independently scheduled forecasters."""
from __future__ import annotations

import math
from datetime import datetime


def production_slot_start(now: datetime, interval_minutes: int = 30) -> datetime:
    """Return the clock-aligned production slot containing ``now``.

    All production forecasters use the same floor-to-boundary rule, so CH and DHW
    always publish the currently active slot first. Boundaries are calculated from
    Unix time rather than local wall-clock arithmetic so repeated/skipped local times
    at DST transitions remain unambiguous.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("production_slot_start requires a timezone-aware datetime")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    interval_seconds = int(interval_minutes) * 60
    timestamp = now.timestamp()
    selected = math.floor(timestamp / interval_seconds) * interval_seconds
    return datetime.fromtimestamp(selected, tz=now.tzinfo)
