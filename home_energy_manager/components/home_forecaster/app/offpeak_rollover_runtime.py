#!/usr/bin/env python3
"""Tariff-window rollover guard for Home Forecaster issue #91.

The legacy selector deliberately skips an overnight block once it has started.
When the following night's tariff has not yet been published, that previously
fell through to a supplier-specific 23:30-05:30 fallback in the core forecaster.
This runtime layer keeps tariff interpretation supplier-neutral: it first uses
the normal selector, then derives the following regular window from an actually
observed active overnight block. If no regular block can be established from
tariff data, forecasting fails explicitly rather than inventing tariff hours.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import battery_model_forecast_runtime as runtime
import battery_model_seed

base = runtime.base
_original_select_controller_offpeak = base.select_controller_offpeak
_model = getattr(runtime, "model", None)
_original_publish_battery_model = _model.publish_shadow_model if _model is not None else None


def _shift_local_day(value: datetime, days: int, tz) -> datetime:
    local = value.astimezone(tz)
    day = local.date() + timedelta(days=days)
    return datetime.combine(day, local.timetz().replace(tzinfo=None), tz)


def _valid_regular_window(block, tz):
    """Only complete overnight blocks may control the battery schedule."""
    start, end, _rate = block
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    duration = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    crosses_midnight = local_start.date() < local_end.date()
    starts_early = local_start.date() == local_end.date() and local_start.hour < 3
    return (
        timedelta(hours=2) <= duration <= timedelta(hours=10)
        and local_end.hour <= 8
        and (crosses_midnight or starts_early)
    )


def select_controller_offpeak(import_rates, now, tz):
    """Keep the active supplier night; reject partial future tariff fragments.

    The old selector intentionally skipped the current period at its start.
    On the following night the supplier often publishes only 23:00-00:00.
    Treating that one-hour fragment as the next overnight window caused the
    controller to replace a valid 06:00 charge end with midnight.
    """
    now_utc = now.astimezone(timezone.utc)
    blocks = sorted(
        base.find_overnight_blocks(import_rates, tz),
        key=lambda block: block[0].astimezone(timezone.utc),
    )
    # An established cheap period remains authoritative until its actual end,
    # even when a portion of tomorrow's tariff is already published.
    active = next(
        (block for block in blocks
         if _valid_regular_window(block, tz)
         and block[0].astimezone(timezone.utc) <= now_utc
         < block[1].astimezone(timezone.utc)),
        None,
    )
    if active is not None:
        return active

    selected = _original_select_controller_offpeak(import_rates, now, tz)
    if selected is not None and _valid_regular_window(selected, tz):
        return selected

    future = next(
        (block for block in blocks
         if _valid_regular_window(block, tz)
         and block[0].astimezone(timezone.utc) > now_utc),
        None,
    )
    if future is not None:
        return future

    # Never make an incomplete supplier fragment look like tomorrow's whole
    # cheap window. The controller must preserve its last verified schedule.
    raise base.HAError(
        "Regular overnight cheap block could not be identified from tariff data; fixed tariff hours are not assumed"
    )

def publish_battery_model_with_seed(client, store, cfg, now):
    _model._ensure_schema(store)
    battery_model_seed.seed_if_needed(client, store, _model, now)
    return _original_publish_battery_model(client, store, cfg, now)


base.select_controller_offpeak = select_controller_offpeak
if _model is not None:
    _model.publish_shadow_model = publish_battery_model_with_seed

if __name__ == "__main__":
    base.main()
