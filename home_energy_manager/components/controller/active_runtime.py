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
_original_sample = core.Controller.sample

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
        'last_low_soc_at',
        'Home Energy Manager Last Low SOC',
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


async def _sample_with_any_low_soc_calibration(self):
    # Let the core sample first. If a scheduled deep calibration is awaiting its
    # low point, this preserves the existing calibration_low_reached_at marker so
    # the dwell/recharge sequence can continue normally.
    await _original_sample(self)
    if not self.db.ok or not self.discovery_ready or not self.c.get('calibration_enabled', True):
        return
    st = await self.ha.state(self.c.get('battery_soc_entity', ''))
    soc = core.as_float(st.get('state')) if st else None
    if soc is None:
        return
    floor = float(self.c.get('deep_cycle_floor_soc', 4))
    if soc <= floor:
        # Any genuine visit to the configured floor is sufficient low-end BMS
        # calibration. Keep a permanent timestamp for observability and reset the
        # deep-cycle interval immediately. calibration_low_reached_at remains a
        # separate workflow marker used only for an already-requested deep cycle.
        now_iso = core.iso(self.now())
        previous = core.parse_dt(self.db.get('last_low_soc_at'))
        # Record once per visit rather than moving the timestamp every 30 seconds
        # while the battery remains sitting at reserve.
        if previous is None or (self.now().astimezone(core.timezone.utc) - previous.astimezone(core.timezone.utc)).total_seconds() > 300:
            self.db.set('last_low_soc_at', now_iso)
        self.db.set('last_deep_calibration_at', now_iso)


async def _publish_with_calibration_sensors(self, plan=None):
    await _original_publish(self, plan)
    floor_soc = float(self.c.get('deep_cycle_floor_soc', 4))
    values = {
        'last_full_soc_at': self.db.get('last_full_soc_at') if self.db.ok else None,
        'last_deep_calibration_at': self.db.get('last_deep_calibration_at') if self.db.ok else None,
        'last_low_soc_at': self.db.get('last_low_soc_at') if self.db.ok else None,
    }
    for entity_id, key, friendly_name, icon in _CALIBRATION_SENSORS:
        value = values.get(key)
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
core.Controller.sample = _sample_with_any_low_soc_calibration
core.Controller.publish = _publish_with_calibration_sensors

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
