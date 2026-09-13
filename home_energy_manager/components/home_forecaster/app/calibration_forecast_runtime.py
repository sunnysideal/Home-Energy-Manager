#!/usr/bin/env python3
"""Overlay Controller-declared deep-calibration recharge into Home Forecaster.

The Controller remains policy owner. This wrapper consumes the package-owned
calibration-plan entity and only changes forecast simulation, never inverter state.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from typing import Any

import battery_model_forecast_runtime as runtime

base = runtime.base
_ENTITY = 'sensor.home_energy_manager_calibration_plan'
_original_battery_config = base.battery_config
_original_simulate_fractional = base.simulate_battery_fractional


def _parse_plan(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict) or str(state.get('state') or '').lower() != 'active':
        return None
    attrs = state.get('attributes') or {}
    if not attrs.get('active'):
        return None
    try:
        start = base.parse_dt(attrs.get('expected_recharge_start'))
        end = base.parse_dt(attrs.get('expected_recharge_end'))
        rate_w = float(attrs.get('expected_recharge_rate_w') or 0.0)
        target_soc = float(attrs.get('expected_recharge_target_soc') or 100.0)
    except Exception:
        return None
    if not start or not end or end <= start or rate_w <= 0:
        return None
    return {'start': start, 'end': end, 'rate_w': rate_w, 'target_soc': target_soc}


def battery_config_with_calibration_plan(client, cfg, reasons):
    batt = _original_battery_config(client, cfg, reasons)
    plan = _parse_plan(client.state_optional(_ENTITY))
    batt['calibration_plan'] = plan
    if plan:
        base.LOG.info(
            'Calibration forecast overlay: expected recharge %s-%s @ %.0fW target=%.0f%%',
            plan['start'].isoformat(), plan['end'].isoformat(), plan['rate_w'], plan['target_soc'],
        )
    return batt


def _overlaps(slot_start, slot_end, plan):
    return slot_start.astimezone(timezone.utc) < plan['end'].astimezone(timezone.utc) and slot_end.astimezone(timezone.utc) > plan['start'].astimezone(timezone.utc)


def simulate_fractional_with_calibration(slot, batt, soc_kwh, charge_eff, discharge_eff):
    plan = batt.get('calibration_plan') if isinstance(batt, dict) else None
    if not plan:
        return _original_simulate_fractional(slot, batt, soc_kwh, charge_eff, discharge_eff)

    slot_start = slot['start_dt']
    slot_end = (slot_start.astimezone(timezone.utc) + base.timedelta(hours=slot['duration_h'])).astimezone(slot_start.tzinfo)
    if not _overlaps(slot_start, slot_end, plan):
        return _original_simulate_fractional(slot, batt, soc_kwh, charge_eff, discharge_eff)

    sim_batt = deepcopy(batt)
    # The synthetic slot is injected only into forecast slots that overlap the
    # one-shot Controller-declared recharge interval, so its HH:MM representation
    # cannot recur on later forecast days.
    start_local = plan['start'].astimezone(slot_start.tzinfo)
    end_local = plan['end'].astimezone(slot_start.tzinfo)
    sim_batt['charge_enabled'] = True
    sim_batt['charge_rate_w'] = min(float(sim_batt.get('max_rate_w') or plan['rate_w']), plan['rate_w'])
    sim_batt['charge_slots'] = list(sim_batt.get('charge_slots') or []) + [
        (start_local.strftime('%H:%M:%S'), end_local.strftime('%H:%M:%S'), plan['target_soc'])
    ]
    return _original_simulate_fractional(slot, sim_batt, soc_kwh, charge_eff, discharge_eff)


base.battery_config = battery_config_with_calibration_plan
base.simulate_battery_fractional = simulate_fractional_with_calibration

if __name__ == '__main__':
    base.main()
