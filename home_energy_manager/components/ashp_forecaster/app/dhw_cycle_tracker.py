"""Identify and classify complete DHW heating cycles from passive thermal samples."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class CycleSample:
    timestamp: datetime
    upper_temp_c: float
    lower_temp_c: float
    dhw_heating: bool
    dhw_energy_total_kwh: float | None
    outdoor_temp_c: float | None
    target_temp_c: float | None = None
    immersion_heating: bool = False
    valid: bool = True


@dataclass(frozen=True)
class HeatingCycle:
    start_ts: datetime
    end_ts: datetime
    start_upper_c: float
    start_lower_c: float
    end_upper_c: float
    end_lower_c: float
    target_temp_c: float | None
    electrical_kwh: float
    outdoor_temp_c: float | None
    cycle_type: str = "normal_dhw"
    valid: bool = True

    @property
    def duration_minutes(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds() / 60.0


def _classify(samples: list[CycleSample], start_idx: int, end_idx: int) -> tuple[str, bool, float | None]:
    active = samples[start_idx:end_idx + 1]
    if any(s.immersion_heating for s in active):
        targets = [s.target_temp_c for s in active if s.target_temp_c is not None]
        target = sum(targets) / len(targets) if targets else None
        return "immersion", False, target

    targets = [s.target_temp_c for s in active if s.target_temp_c is not None]
    target_stable = not targets or max(targets) - min(targets) <= 1.0
    target = sum(targets) / len(targets) if targets else None
    if not target_stable:
        return "normal_dhw", False, target

    high_target = bool(targets and max(targets) >= 58.0)
    high_observed = max(s.upper_temp_c for s in active) >= 58.0
    return ("high_temp_dhw" if high_target or high_observed else "normal_dhw"), True, target


def build_cycle(samples: list[CycleSample], *, max_gap_minutes: float = 12.0) -> HeatingCycle | None:
    """Build one complete cycle from a contiguous sample sequence."""
    if len(samples) < 3:
        return None
    ordered = sorted(samples, key=lambda s: s.timestamp)
    if any(not s.valid for s in ordered):
        return None
    for left, right in zip(ordered, ordered[1:]):
        if right.timestamp <= left.timestamp:
            return None
        if (right.timestamp - left.timestamp) > timedelta(minutes=max_gap_minutes):
            return None

    start_idx = None
    for idx in range(1, len(ordered)):
        if not ordered[idx - 1].dhw_heating and ordered[idx].dhw_heating:
            start_idx = idx
            break
    if start_idx is None:
        return None

    end_idx = None
    for idx in range(start_idx + 1, len(ordered)):
        if ordered[idx - 1].dhw_heating and not ordered[idx].dhw_heating:
            end_idx = idx
            break
    if end_idx is None:
        return None

    pre = ordered[start_idx - 1]
    post = ordered[end_idx]
    if pre.dhw_energy_total_kwh is None or post.dhw_energy_total_kwh is None:
        return None
    electrical = post.dhw_energy_total_kwh - pre.dhw_energy_total_kwh
    if not (0.05 <= electrical <= 20.0):
        return None

    outdoor_values = [
        s.outdoor_temp_c for s in ordered[start_idx:end_idx + 1]
        if s.outdoor_temp_c is not None
    ]
    outdoor = sum(outdoor_values) / len(outdoor_values) if outdoor_values else None
    cycle_type, valid, target = _classify(ordered, start_idx, end_idx)

    return HeatingCycle(
        start_ts=ordered[start_idx].timestamp,
        end_ts=post.timestamp,
        start_upper_c=pre.upper_temp_c,
        start_lower_c=pre.lower_temp_c,
        end_upper_c=post.upper_temp_c,
        end_lower_c=post.lower_temp_c,
        target_temp_c=target,
        electrical_kwh=electrical,
        outdoor_temp_c=outdoor,
        cycle_type=cycle_type,
        valid=valid,
    )
