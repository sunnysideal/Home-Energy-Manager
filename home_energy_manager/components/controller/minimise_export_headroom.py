"""Solar-headroom target calculation for minimise-export overnight charging.

This module consumes the Home Forecaster's already-produced no-slots load/PV
segments. It does not forecast load, PV, ASHP demand or EV demand itself.
"""
from __future__ import annotations

import math


def _avoidable_export_kwh(
    segments,
    start_soc: float,
    capacity_kwh: float,
    reserve_soc: float,
    max_charge_w: float,
    max_discharge_w: float,
) -> float:
    """Return export caused by lack of battery headroom for one candidate SOC.

    The calculation deliberately uses unity battery efficiency. That is
    conservative for headroom sizing: real charging stores less than the AC
    surplus and real discharge removes more stored energy than the AC load it
    serves, so the real battery will normally have at least as much headroom as
    this calculation predicts.

    Export above the hardware charge-power limit is not counted as avoidable by
    overnight SOC because extra empty capacity cannot absorb it any faster.
    """
    capacity = max(0.001, float(capacity_kwh))
    reserve_kwh = capacity * max(0.0, min(100.0, float(reserve_soc))) / 100.0
    stored = capacity * max(float(reserve_soc), min(100.0, float(start_soc))) / 100.0
    max_charge_kw = max(0.0, float(max_charge_w)) / 1000.0
    max_discharge_kw = max(0.0, float(max_discharge_w)) / 1000.0
    avoidable_export = 0.0

    for start, end, load_kwh, pv_kwh in segments:
        duration_h = max(0.0, (end - start).total_seconds() / 3600.0)
        net_ac = float(pv_kwh) - float(load_kwh)
        if net_ac > 0.0:
            chargeable_ac = min(net_ac, max_charge_kw * duration_h)
            room = max(0.0, capacity - stored)
            stored_gain = min(chargeable_ac, room)
            stored += stored_gain
            avoidable_export += max(0.0, chargeable_ac - stored_gain)
        elif net_ac < 0.0:
            discharge_needed = min(-net_ac, max_discharge_kw * duration_h)
            available = max(0.0, stored - reserve_kwh)
            stored -= min(discharge_needed, available)

    return max(0.0, avoidable_export)


def highest_soc_without_avoidable_export(
    segments,
    capacity_kwh: float,
    reserve_soc: float,
    max_charge_w: float,
    max_discharge_w: float,
    tolerance_kwh: float = 0.01,
):
    """Return the highest integer start SOC that avoids battery-full export.

    Returns ``(target_soc, diagnostics)``. If even the reserve-floor trajectory
    still fills the battery, the reserve floor is returned; the remaining export
    is reported as unavoidable by overnight SOC alone.
    """
    floor = int(math.ceil(max(0.0, min(100.0, float(reserve_soc)))))
    tolerance = max(0.0, float(tolerance_kwh))
    if not segments:
        return 100, {
            "target_soc": 100,
            "avoidable_export_kwh": 0.0,
            "strategy": "no_forecast_segments_fail_safe_full",
        }

    for candidate in range(100, floor - 1, -1):
        export = _avoidable_export_kwh(
            segments,
            candidate,
            capacity_kwh,
            reserve_soc,
            max_charge_w,
            max_discharge_w,
        )
        if export <= tolerance:
            return candidate, {
                "target_soc": candidate,
                "avoidable_export_kwh": round(export, 3),
                "strategy": "highest_soc_without_avoidable_pv_export",
            }

    export = _avoidable_export_kwh(
        segments,
        floor,
        capacity_kwh,
        reserve_soc,
        max_charge_w,
        max_discharge_w,
    )
    return floor, {
        "target_soc": floor,
        "avoidable_export_kwh": round(export, 3),
        "strategy": "reserve_floor_still_exports",
    }
