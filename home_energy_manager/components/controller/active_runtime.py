#!/usr/bin/env python3
"""Active-controller entrypoint enforcing user-owned charge target SOC.

The planner may calculate logical SOC objectives, but Home Energy Manager must
never write the inverter charge-target entity. Charging is controlled by slot
timing and rate; the user's inverter target remains untouched.
"""
import asyncio

import minimise_export_runtime as runtime

core = runtime.core
_original_ensure = core.Controller.ensure
_original_publish = core.Controller.publish

_CALIBRATION_SENSORS = (
    (
        'sensor.home_energy_manager_last_full_charge',
        'last_full_soc_at',
        'Home Energy Manager Last Full Charge',
        'mdi:battery-high',
    ),
    (
        'sensor.home_energy_manager_last_deep_cycle',
        'last_deep_calibration_at',
        'Home Energy Manager Last Deep Cycle',
        'mdi:battery-sync',
    ),
    (
        'sensor.home_energy_manager_last_low_soc',
        'low_reached_at',
        'Home Energy Manager Last Calibration Low SOC',
        'mdi:battery-low',
    ),
)


async def _ensure_without_charge_target(self, field, entity, desired, window_end):
    if field == 'charge_target':
        core.LOG.info(
            'Charge target write suppressed: logical_target=%s entity=%s; '
            'charge quantity is controlled by duration/rate',
            desired, entity,
        )
        return True
    return await _original_ensure(self, field, entity, desired, window_end)


async def _publish_with_calibration_sensors(self, plan=None):
    await _original_publish(self, plan)
    calibration = self.calibration_attrs()
    floor_soc = float(self.c.get('deep_cycle_floor_soc', 4))
    for entity_id, key, friendly_name, icon in _CALIBRATION_SENSORS:
        value = calibration.get(key)
        attrs = {
            'friendly_name': friendly_name,
            'device_class': 'timestamp',
            'icon': icon,
            'calibration_key': key,
            'calibration_soc': 100 if key == 'last_full_soc_at' else floor_soc,
            'battery_soc_entity': self.c.get('battery_soc_entity') or None,
        }
        state = value or 'unknown'
        if not self.mqtt.publish_sensor(entity_id, state, attrs):
            await self.ha.publish(entity_id, state, attrs)


core.Controller.ensure = _ensure_without_charge_target
core.Controller.publish = _publish_with_calibration_sensors

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
