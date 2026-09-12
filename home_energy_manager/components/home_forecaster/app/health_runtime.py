#!/usr/bin/env python3
"""Runtime health policy for optional Home Forecaster capabilities.

The physical forecast remains owned by ``main``; Axle pricing and battery idle
standby learning are packaged runtime layers below this one. This wrapper only
classifies health diagnostics: an optional feature that is not configured is
informational, while a configured feature that cannot be read remains a
degradation.
"""
from __future__ import annotations

from typing import Any

import battery_idle_core as pricing

base = pricing.base

EXPORT_UNCONFIGURED_REASON = "Export tariff unavailable: export income outputs incomplete"
EV_UNAVAILABLE_REASON = "EV Smart Charging dispatch entity unavailable: dynamic cheap slots and EV forecast ignored"

_last_capabilities: dict[str, dict[str, Any]] = {}
_wrapped_make_forecast = base.make_forecast
_wrapped_publish_health = base.publish_health


def _configured(section: dict[str, Any], *keys: str) -> bool:
    return any(str(section.get(key) or "").strip() for key in keys)


def classify_optional_health(cfg, reasons: list[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    tariff = cfg.section("tariff")
    ev = cfg.section("ev")

    export_configured = _configured(tariff, "export_current_rate", "export_current_day_rates", "export_next_day_rates")
    ev_enabled = bool(ev.get("smart_charging_enabled", False))
    ev_dispatch_configured = _configured(ev, "smart_charging_dispatch")

    degraded: list[str] = []
    capabilities: dict[str, dict[str, Any]] = {
        "export_tariff": {"status": "configured" if export_configured else "unconfigured", "required": False},
        "ev_smart_charging": {
            "status": "configured" if ev_enabled and ev_dispatch_configured else "unconfigured",
            "required": False,
            "enabled": ev_enabled,
        },
    }

    for reason in reasons:
        if reason == EXPORT_UNCONFIGURED_REASON and not export_configured:
            capabilities["export_tariff"]["message"] = reason
            continue
        if reason == EV_UNAVAILABLE_REASON and (not ev_enabled or not ev_dispatch_configured):
            capabilities["ev_smart_charging"]["message"] = reason
            continue
        degraded.append(reason)

    return degraded, capabilities


def make_forecast_with_health_policy(client, store, cfg, now):
    global _last_capabilities
    payload, reasons = _wrapped_make_forecast(client, store, cfg, now)
    reasons, _last_capabilities = classify_optional_health(cfg, list(reasons))
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if isinstance(attrs, dict):
        attrs["capabilities"] = _last_capabilities
    return payload, reasons


def publish_health_with_capabilities(client, state, reasons, last_success, duration_s, store, failures):
    _wrapped_publish_health(client, state, reasons, last_success, duration_s, store, failures)
    try:
        current = client.state_optional(base.HEALTH_ENTITY) or {}
        attrs = dict(current.get("attributes") or {})
        attrs["capabilities"] = _last_capabilities
        client.publish(base.HEALTH_ENTITY, state, attrs)
    except Exception as exc:
        base.LOG.warning("Forecast health capability attributes unavailable: %s", exc)


base.make_forecast = make_forecast_with_health_policy
base.publish_health = publish_health_with_capabilities

if __name__ == "__main__":
    base.main()
