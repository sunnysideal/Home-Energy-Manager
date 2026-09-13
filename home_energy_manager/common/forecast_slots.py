"""Shared time-slot boundary helpers for independently scheduled forecasters."""
from __future__ import annotations

import math
from datetime import datetime

DEFAULT_BOUNDARY_GRACE_SECONDS = 60.0


def production_slot_start(
    now: datetime,
    interval_minutes: int = 30,
    *,
    boundary_grace_seconds: float = DEFAULT_BOUNDARY_GRACE_SECONDS,
) -> datetime:
    """Return the deterministic production-slot start for an aware datetime.

    Forecasting processes run independently, so one may evaluate immediately before a
    half-hour boundary while another evaluates a few seconds after it. During the short
    post-boundary grace window both processes deliberately select the boundary that has
    just started; otherwise the next interval boundary is selected.

    Boundaries are calculated from Unix time rather than local wall-clock arithmetic so
    repeated/skipped local times at DST transitions remain unambiguous.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("production_slot_start requires a timezone-aware datetime")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    if boundary_grace_seconds < 0:
        raise ValueError("boundary_grace_seconds must not be negative")

    interval_seconds = int(interval_minutes) * 60
    timestamp = now.timestamp()
    current_boundary = math.floor(timestamp / interval_seconds) * interval_seconds
    seconds_after_boundary = timestamp - current_boundary

    if seconds_after_boundary <= boundary_grace_seconds:
        selected = current_boundary
    else:
        selected = current_boundary + interval_seconds
    return datetime.fromtimestamp(selected, tz=now.tzinfo)
