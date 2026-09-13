"""Stable Home Assistant contract for the forecaster-owned battery model.

This module defines the interface only.  The Home Forecaster does not publish a
production learned model from this module yet, and the Controller does not
consume it yet.  Those ownership changes are deliberately staged in later
migration issues.
"""

from __future__ import annotations

from datetime import datetime, timezone

BATTERY_MODEL_ENTITY = "sensor.home_energy_manager_battery_model"
BATTERY_MODEL_SCHEMA_VERSION = 1
DEFAULT_MAX_MODEL_AGE_SECONDS = 900

SOC_BANDS = [
    (0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60),
    (60, 70), (70, 80), (80, 90), (90, 95), (95, 98), (98, 99),
    (99, 100),
]

GENERIC_FACTORS = {
    (0, 10): .95, (10, 20): .95, (20, 30): .95, (30, 40): .95,
    (40, 50): .95, (50, 60): .95, (60, 70): .95, (70, 80): .95,
    (80, 90): .93, (90, 95): .88, (95, 98): .78, (98, 99): .58,
    (99, 100): .35,
}


def build_battery_model_attributes(
    *,
    generated_at: str,
    bands: list[dict],
    top_completion_allowance_minutes: float,
    generic_top_completion_minutes: float = 15.0,
    source: str = "home_forecaster",
    diagnostics: dict | None = None,
) -> dict:
    """Build the versioned HA attributes for the physical battery model."""
    attrs = {
        "schema_version": BATTERY_MODEL_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": source,
        "bands": bands,
        "top_completion_allowance_minutes": float(top_completion_allowance_minutes),
        "generic_top_completion_minutes": float(generic_top_completion_minutes),
    }
    if diagnostics:
        attrs["diagnostics"] = diagnostics
    return attrs


def generic_bands() -> list[dict]:
    """Return the exact generic SOC-band model currently used by Controller."""
    return [
        {
            "soc_lo": lo,
            "soc_hi": hi,
            "generic_factor": GENERIC_FACTORS[(lo, hi)],
            "learned_factor": None,
            "confidence": 0.0,
            "effective_factor": GENERIC_FACTORS[(lo, hi)],
        }
        for lo, hi in SOC_BANDS
    ]


def validate_battery_model_attributes(
    attrs: dict,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_MODEL_AGE_SECONDS,
) -> tuple[bool, str]:
    """Validate completeness, compatibility and freshness of model attributes.

    The future Controller consumer can use the returned reason to select its
    existing local fallback deterministically.  This function itself has no
    effect on Controller behaviour.
    """
    if not isinstance(attrs, dict):
        return False, "not_a_mapping"
    if attrs.get("schema_version") != BATTERY_MODEL_SCHEMA_VERSION:
        return False, "schema_version"

    generated_at = attrs.get("generated_at")
    if not isinstance(generated_at, str):
        return False, "generated_at"
    try:
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return False, "generated_at"
    if stamp.tzinfo is None:
        return False, "generated_at_timezone"

    current = now or datetime.now(timezone.utc)
    age = (current - stamp.astimezone(timezone.utc)).total_seconds()
    if age < -60:
        return False, "generated_at_future"
    if age > max_age_seconds:
        return False, "stale"

    bands = attrs.get("bands")
    if not isinstance(bands, list) or len(bands) != len(SOC_BANDS):
        return False, "bands"

    expected = set(SOC_BANDS)
    seen = set()
    for band in bands:
        if not isinstance(band, dict):
            return False, "band_mapping"
        try:
            key = (float(band["soc_lo"]), float(band["soc_hi"]))
            factor = float(band["effective_factor"])
        except (KeyError, TypeError, ValueError):
            return False, "band_fields"
        if key not in expected or key in seen:
            return False, "band_range"
        if not 0.05 <= factor <= 1.0:
            return False, "effective_factor"
        seen.add(key)
    if seen != expected:
        return False, "band_coverage"

    try:
        top = float(attrs["top_completion_allowance_minutes"])
        generic_top = float(attrs["generic_top_completion_minutes"])
    except (KeyError, TypeError, ValueError):
        return False, "top_completion"
    if generic_top < 0 or top < generic_top:
        return False, "top_completion_range"

    return True, "ok"


def charge_minutes_from_model(
    soc: float,
    target: float,
    rate_w: float,
    capacity_kwh: float,
    attrs: dict,
) -> float:
    """Reference calculation used only for contract/parity testing in #49."""
    if rate_w <= 0 or target <= soc:
        return 0.0
    factors = {
        (float(b["soc_lo"]), float(b["soc_hi"])): float(b["effective_factor"])
        for b in attrs["bands"]
    }
    minutes = 0.0
    for lo, hi in SOC_BANDS:
        overlap = max(0.0, min(target, hi) - max(soc, lo))
        if overlap:
            minutes += (
                capacity_kwh * (overlap / 100.0)
                / ((rate_w / 1000.0) * max(0.05, factors[(float(lo), float(hi))]))
                * 60.0
            )
    if target >= 100:
        minutes += float(attrs["top_completion_allowance_minutes"])
    return minutes
