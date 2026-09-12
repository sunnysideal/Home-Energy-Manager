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
        100.0,
        None,
    ),
    (
        'sensor.home_energy_manager_last_deep_cycle',
        'last_deep_calibration_at',
        'Home Energy Manager Last Deep Cycle',
        'mdi:battery-sync',
        None,
        None,
    ),
    (
        'sensor.home_energy_manager_last_below_40_soc',
        'last_below_40_soc_at',
        'Home Energy Manager Last Below 40% SOC',
        'mdi:battery-40',
        40.0,
        'soc_crossing_40',
    ),
    (
        'sensor.home_energy_manager_last_below_20_soc',
        'last_below_20_soc_at',
        'Home Energy Manager Last Below 20% SOC',
        'mdi:battery-20',
        20.0,
        'soc_crossing_20',
    ),
    (
        'sensor.home_energy_manager_last_low_soc',
        'last_low_soc_at',
        'Home Energy Manager Last Low SOC',
        'mdi:battery-low',
        None,
        'soc_crossing_floor',
    ),
)


def _store_soc_crossing(controller, threshold, key_prefix, previous_soc, soc, now_iso, elapsed_seconds):
    """Persist one downward SOC threshold crossing and the observed step around it."""
    controller.db.set(f'last_below_{int(threshold)}_soc_at', now_iso)
    controller.db.set(f'{key_prefix}_soc_before', previous_soc)
    controller.db.set(f'{key_prefix}_soc_after', soc)
    controller.db.set(f'{key_prefix}_step_delta_pp', soc - previous_soc)
    controller.db.set(f'{key_prefix}_sample_seconds', elapsed_seconds)
    core.LOG.info(
        'SOC calibration observation: crossed %.0f%% downward %.2f%% -> %.2f%% '
        '(step=%+.2fpp sample=%ss)',
        threshold,
        previous_soc,
        soc,
        soc - previous_soc,
        'unknown' if elapsed_seconds is None else f'{elapsed_seconds:.1f}',
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


async def _sample_with_soc_calibration_observation(self):
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

    now = self.now()
    now_iso = core.iso(now)
    floor = float(self.c.get('deep_cycle_floor_soc', 4))

    # Persist the previous real observation across controller restarts. The first
    # sample after installation/restart establishes a baseline and deliberately
    # does not invent a threshold crossing.
    previous_soc = core.as_float(self.db.get('calibration_previous_soc'))
    previous_at = core.parse_dt(self.db.get('calibration_previous_soc_at'))
    elapsed_seconds = None
    if previous_at is not None:
        try:
            elapsed_seconds = max(0.0, (now - previous_at.astimezone(now.tzinfo)).total_seconds())
        except Exception:
            elapsed_seconds = None

    if previous_soc is not None and soc < previous_soc:
        for threshold, key_prefix in ((40.0, 'soc_crossing_40'), (20.0, 'soc_crossing_20')):
            if previous_soc > threshold and soc <= threshold:
                _store_soc_crossing(
                    self,
                    threshold,
                    key_prefix,
                    previous_soc,
                    soc,
                    now_iso,
                    elapsed_seconds,
                )

    # Any genuine visit to the configured floor is sufficient low-end BMS
    # calibration. This is deliberately visit/latch based rather than crossing
    # based because starting/restarting while genuinely at reserve still counts.
    low_active = bool(self.db.get('low_soc_visit_active'))
    if soc <= floor and not low_active:
        self.db.set('last_low_soc_at', now_iso)
        self.db.set('last_deep_calibration_at', now_iso)
        self.db.set('low_soc_visit_active', True)
        if previous_soc is not None:
            self.db.set('soc_crossing_floor_soc_before', previous_soc)
            self.db.set('soc_crossing_floor_soc_after', soc)
            self.db.set('soc_crossing_floor_step_delta_pp', soc - previous_soc)
            self.db.set('soc_crossing_floor_sample_seconds', elapsed_seconds)
        core.LOG.info(
            'Calibration low SOC observed naturally/actively: soc=%.1f%% floor=%.1f%%; '
            'deep-cycle interval reset',
            soc,
            floor,
        )
    elif soc > floor + 0.5 and low_active:
        # Hysteresis makes a sustained stay at reserve one visit, rather than
        # refreshing the timestamp on every sample.
        self.db.set('low_soc_visit_active', False)

    self.db.set('calibration_previous_soc', soc)
    self.db.set('calibration_previous_soc_at', now_iso)


async def _publish_with_calibration_sensors(self, plan=None):
    await _original_publish(self, plan)
    floor_soc = float(self.c.get('deep_cycle_floor_soc', 4))

    for entity_id, key, friendly_name, icon, fixed_soc, crossing_prefix in _CALIBRATION_SENSORS:
        value = self.db.get(key) if self.db.ok else None
        threshold = floor_soc if key in ('last_deep_calibration_at', 'last_low_soc_at') else fixed_soc
        attrs = {
            'friendly_name': friendly_name,
            'device_class': 'timestamp',
            'icon': icon,
            'calibration_key': key,
            'calibration_soc': threshold,
            'battery_soc_entity': self.c.get('battery_soc_entity') or None,
        }
        if crossing_prefix and self.db.ok:
            attrs.update({
                'soc_before': core.as_float(self.db.get(f'{crossing_prefix}_soc_before')),
                'soc_after': core.as_float(self.db.get(f'{crossing_prefix}_soc_after')),
                'soc_step_delta_pp': core.as_float(self.db.get(f'{crossing_prefix}_step_delta_pp')),
                'sample_seconds': core.as_float(self.db.get(f'{crossing_prefix}_sample_seconds')),
            })
        state = value or 'unknown'
        if not self.mqtt.publish_sensor(entity_id, state, attrs):
            await self.ha.publish(entity_id, state, attrs)


core.Controller.ensure = _ensure_without_charge_target
core.Controller.sample = _sample_with_soc_calibration_observation
core.Controller.publish = _publish_with_calibration_sensors

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
