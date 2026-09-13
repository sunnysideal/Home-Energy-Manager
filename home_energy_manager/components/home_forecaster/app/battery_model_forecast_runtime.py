#!/usr/bin/env python3
"""Forecast consumer for the forecaster-owned battery model (issue #51).

This wrapper promotes the model learned/published by ``battery_model_runtime``
from shadow-only state into the Home Forecaster's authoritative battery-model
interface.  Forecast charging is routed through that interface while retaining
legacy flat-charge behaviour for this migration stage.  The explicit
compatibility calculation keeps forecast outputs unchanged; later migration
work can enable SOC-band tapering without changing ownership again.

The Controller remains completely independent and does not consume this model.
"""
from __future__ import annotations

from typing import Any

import battery_model_core as model
from battery_model_contract import BATTERY_MODEL_ENTITY, SOC_BANDS

base = model.base

_wrapped_simulate_battery = base.simulate_battery
_active_model_attributes: dict[str, Any] | None = None


def _band_for_soc(attrs: dict[str, Any], soc_pct: float) -> dict[str, Any] | None:
    """Return the published model band covering ``soc_pct``."""
    bands = attrs.get("bands") if isinstance(attrs, dict) else None
    if not isinstance(bands, list):
        return None
    bounded = max(0.0, min(100.0, float(soc_pct)))
    for band in bands:
        try:
            lo = float(band["soc_lo"])
            hi = float(band["soc_hi"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= bounded < hi or (bounded >= 100.0 and hi == 100.0):
            return band
    return None


def forecast_charge_efficiency(
    attrs: dict[str, Any] | None,
    *,
    soc_pct: float,
    legacy_efficiency: float,
) -> float:
    """Return the charge efficiency used by the production forecast.

    #51 deliberately changes model ownership without tuning forecast maths.
    The SOC-band model is therefore consulted and validated here, but its taper
    factor is normalised out for this compatibility stage so the result is
    exactly the legacy charge efficiency.  This gives the Forecaster one model
    interface now and a safe switch point for later accuracy work.
    """
    legacy = float(legacy_efficiency)
    if attrs is None:
        return legacy
    band = _band_for_soc(attrs, soc_pct)
    if band is None:
        return legacy
    try:
        factor = float(band["effective_factor"])
    except (KeyError, TypeError, ValueError):
        return legacy
    if factor <= 0:
        return legacy
    # Explicit compatibility normalisation: model_factor / model_factor == 1.
    return legacy * (factor / factor)


def simulate_battery_with_forecaster_model(
    slot: dict[str, Any],
    batt: dict[str, Any],
    soc_kwh: float,
    charge_eff: float,
    discharge_eff: float,
):
    capacity = float(batt.get("capacity") or 0.0)
    soc_pct = (float(soc_kwh) / capacity * 100.0) if capacity > 0 else 0.0
    model_charge_eff = forecast_charge_efficiency(
        _active_model_attributes,
        soc_pct=soc_pct,
        legacy_efficiency=charge_eff,
    )
    return _wrapped_simulate_battery(slot, batt, soc_kwh, model_charge_eff, discharge_eff)


def _model_attributes_for_forecast(store, cfg, now):
    """Build the single model snapshot used for publication and simulation."""
    global _active_model_attributes
    attrs = model._model_attributes_original(store, cfg, now)
    diagnostics = dict(attrs.get("diagnostics") or {})
    diagnostics.update(
        {
            "mode": "forecaster_owned",
            "controller_consumes_model": False,
            "forecast_consumes_model": True,
            "forecast_charge_mode": "legacy_flat_compatibility",
        }
    )
    attrs["diagnostics"] = diagnostics
    _active_model_attributes = attrs
    return attrs


def publish_forecaster_model(client, store, cfg, now):
    """Observe, build and publish the same model snapshot used by the forecast."""
    model._ensure_schema(store)
    model._observe(client, store, cfg, now)
    attrs = _model_attributes_for_forecast(store, cfg, now)
    client.publish(BATTERY_MODEL_ENTITY, "active", attrs)
    return attrs


# Preserve the original builder so our wrapper can call it without recursion.
model._model_attributes_original = model._model_attributes
model._model_attributes = _model_attributes_for_forecast
model.publish_shadow_model = publish_forecaster_model
base.simulate_battery = simulate_battery_with_forecaster_model

if __name__ == "__main__":
    base.main()
