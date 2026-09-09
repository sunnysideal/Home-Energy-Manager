"""Conservative DHW draw-event detection from two cylinder temperature sensors."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

WATER_KWH_PER_LITRE_C = 4.186 / 3600.0


@dataclass(frozen=True)
class ThermalSample:
    timestamp: datetime
    upper_temp_c: float
    lower_temp_c: float
    heating: bool = False
    valid: bool = True


@dataclass(frozen=True)
class DrawEvent:
    timestamp: datetime
    estimated_thermal_kwh: float
    confidence: float
    upper_before_c: float
    lower_before_c: float
    upper_after_c: float
    lower_after_c: float


def _thermal_drop_kwh(
    previous: ThermalSample,
    current: ThermalSample,
    *,
    volume_l: float,
    upper_fraction: float,
) -> float:
    upper_v = volume_l * upper_fraction
    lower_v = volume_l - upper_v
    upper_drop = max(previous.upper_temp_c - current.upper_temp_c, 0.0)
    lower_drop = max(previous.lower_temp_c - current.lower_temp_c, 0.0)
    return WATER_KWH_PER_LITRE_C * (upper_v * upper_drop + lower_v * lower_drop)


def detect_draw(
    previous: ThermalSample,
    current: ThermalSample,
    *,
    volume_l: float = 250.0,
    upper_fraction: float = 0.45,
    lower_drop_threshold_c: float = 0.75,
    upper_drop_threshold_c: float = 0.40,
) -> DrawEvent | None:
    """Detect a likely draw while deliberately favouring false negatives.

    Standing losses over a normal five-minute interval should be far smaller than the
    thresholds.  Heating periods are excluded because their temperature response is
    ambiguous.  Large sensor jumps and long gaps are rejected rather than learned.
    """
    if not previous.valid or not current.valid or previous.heating or current.heating:
        return None
    seconds = (current.timestamp - previous.timestamp).total_seconds()
    if seconds < 60.0 or seconds > 15.0 * 60.0:
        return None
    if volume_l <= 0 or not 0.1 <= upper_fraction <= 0.9:
        return None

    upper_delta = current.upper_temp_c - previous.upper_temp_c
    lower_delta = current.lower_temp_c - previous.lower_temp_c
    if abs(upper_delta) > 15.0 or abs(lower_delta) > 20.0:
        return None

    upper_drop = max(-upper_delta, 0.0)
    lower_drop = max(-lower_delta, 0.0)
    if lower_drop < lower_drop_threshold_c and upper_drop < upper_drop_threshold_c:
        return None

    estimated = _thermal_drop_kwh(
        previous, current, volume_l=volume_l, upper_fraction=upper_fraction
    )
    if estimated < 0.05:
        return None

    # A lower-zone-first response is especially characteristic of a small/medium draw;
    # an upper-zone fall raises confidence for a larger draw. Keep confidence bounded
    # because without a flow meter the event magnitude remains inferred.
    score = 0.45
    score += min(lower_drop / 4.0, 0.25)
    score += min(upper_drop / 3.0, 0.20)
    if lower_drop > upper_drop:
        score += 0.05
    confidence = min(max(score, 0.0), 0.95)

    return DrawEvent(
        timestamp=current.timestamp,
        estimated_thermal_kwh=estimated,
        confidence=confidence,
        upper_before_c=previous.upper_temp_c,
        lower_before_c=previous.lower_temp_c,
        upper_after_c=current.upper_temp_c,
        lower_after_c=current.lower_temp_c,
    )
