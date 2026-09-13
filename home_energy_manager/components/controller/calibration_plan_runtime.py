#!/usr/bin/env python3
"""Publish the Controller's expected deep-calibration sequence for forecasting.

This is a presentation/interface layer only. It does not change Controller policy
or inverter writes; the real recharge still waits for an observed low-SOC event.
"""
import asyncio
from datetime import timedelta

import manual_low_calibration_runtime as runtime

core = runtime.core
_original_publish = core.Controller.publish
_ENTITY = 'sensor.home_energy_manager_calibration_plan'


async def _publish_with_calibration_plan(self, plan=None):
    await _original_publish(self, plan)

    attrs = {
        'friendly_name': 'Home Energy Manager Calibration Plan',
        'icon': 'mdi:battery-sync',
        'active': False,
        'state': self.calibration_state(),
        'source': 'controller',
    }
    state = 'inactive'

    calibration = plan.get('calibration', {}) if isinstance(plan, dict) else {}
    offpeak = plan.get('offpeak', {}) if isinstance(plan, dict) else {}
    if calibration.get('state') == 'awaiting_deep_low' and calibration.get('feasible'):
        low_at = core.parse_dt(calibration.get('latest_safe_reserve_at') or calibration.get('expected_reserve_at'))
        offpeak_end = core.parse_dt(offpeak.get('end') or calibration.get('offpeak_end'))
        if low_at and offpeak_end:
            dwell_minutes = max(0, int(calibration.get('reserve_dwell_minutes') or self.c.get('reserve_dwell_minutes', 30)))
            safety_minutes = max(0, int(self.c.get('charge_safety_margin_minutes', 10)))
            recharge_start = low_at + timedelta(minutes=dwell_minutes)
            recharge_end = offpeak_end - timedelta(minutes=safety_minutes)
            rate_w = int(round(float(getattr(self, '_calibration_max_charge_w', 0.0) or 0.0)))
            attrs.update({
                'active': True,
                'state': 'awaiting_deep_low',
                'target_low_soc': float(calibration.get('target_soc') or self.c.get('deep_cycle_floor_soc', 4)),
                'expected_low_at': core.iso(low_at),
                'reserve_dwell_minutes': dwell_minutes,
                'expected_recharge_start': core.iso(recharge_start),
                'expected_recharge_end': core.iso(recharge_end),
                'expected_recharge_rate_w': rate_w,
                'expected_recharge_target_soc': 100,
                'offpeak_end': core.iso(offpeak_end),
                'strategy': calibration.get('strategy'),
                'reason': calibration.get('reason'),
            })
            state = 'active'

    if not self.mqtt.publish_sensor(_ENTITY, state, attrs):
        await self.ha.publish(_ENTITY, state, attrs)


core.Controller.publish = _publish_with_calibration_plan

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
