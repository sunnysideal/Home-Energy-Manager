#!/usr/bin/env python3
"""Pre-program the forecast deep-calibration recharge slot in Controller."""
import asyncio
from datetime import timedelta

import manual_high_calibration_runtime as runtime

core = runtime.core
_legacy_runtime = runtime.runtime._legacy_runtime
_original_strategy = runtime.runtime._natural_first_calibration_discharge


def _strategy_with_forecast_recharge(controller, plan, window, capacity, reserve, max_discharge, forecast):
    diagnostics = _original_strategy(controller, plan, window, capacity, reserve, max_discharge, forecast)
    if not diagnostics or not diagnostics.get('feasible') or diagnostics.get('state') != 'awaiting_deep_low':
        return diagnostics

    low_at = core.parse_dt(diagnostics.get('latest_safe_reserve_at'))
    if not low_at:
        return diagnostics
    dwell_minutes = max(0, int(controller.c.get('reserve_dwell_minutes', 15)))
    rate_w = max(1.0, float(getattr(controller, '_calibration_max_charge_w', 0.0)))
    recharge_kwh = float(capacity) * max(0.0, 100.0 - float(reserve)) / 100.0
    recharge_hours = recharge_kwh / max(0.001, rate_w / 1000.0)
    charge_start = (low_at + timedelta(minutes=dwell_minutes)).replace(second=0, microsecond=0)
    charge_end = charge_start + timedelta(hours=recharge_hours)

    # This is a normal Controller charge plan, deliberately written before the
    # low point is observed. The next planning cycle may move it while future;
    # active-slot start immutability applies once it has begun.
    plan['charge'] = {
        'start': core.iso(charge_start),
        'end': core.iso(charge_end),
        'rate_w': int(round(rate_w)),
        'target_soc': 100,
        'planned_kwh': round(recharge_kwh, 3),
        'kind': 'calibration_forecast_recharge',
    }
    diagnostics.update({
        'forecast_recharge_start': core.iso(charge_start),
        'forecast_recharge_end': core.iso(charge_end),
        'forecast_recharge_rate_w': int(round(rate_w)),
        'forecast_recharge_target_soc': 100,
        'forecast_recharge_planned_kwh': round(recharge_kwh, 3),
    })
    core.LOG.info(
        'Low calibration forecast recharge: %s-%s @ %.0fW logical_target=100%% dwell=%dm',
        charge_start.strftime('%Y-%m-%d %H:%M'), charge_end.strftime('%Y-%m-%d %H:%M'), rate_w, dwell_minutes,
    )
    return diagnostics


_legacy_runtime._minimise_calibration_discharge = _strategy_with_forecast_recharge

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
