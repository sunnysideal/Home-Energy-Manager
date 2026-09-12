#!/usr/bin/env python3
"""Active-controller entrypoint enforcing user-owned charge target SOC."""
import asyncio
import json
from collections import deque
from datetime import timedelta
from pathlib import Path

import minimise_export_runtime as runtime

core = runtime.core
_original_ensure = core.Controller.ensure
_original_publish = core.Controller.publish
_original_sample = core.Controller.sample

_SOC_WINDOW_HALF_SECONDS = 5 * 60
_SOC_HISTORY_SECONDS = 7 * 60
_HOME_FORECASTER_OPTIONS = Path('/data/options_home_forecaster.json')
_soc_energy_history = deque()

_CALIBRATION_SENSORS = (
    ('sensor.home_energy_manager_last_full_charge', 'last_full_soc_at', 'Home Energy Manager Last Full Charge', 'mdi:battery-high', 100.0, None),
    ('sensor.home_energy_manager_last_deep_cycle', 'last_deep_calibration_at', 'Home Energy Manager Last Deep Cycle', 'mdi:battery-sync', None, None),
    ('sensor.home_energy_manager_last_below_40_soc', 'last_below_40_soc_at', 'Home Energy Manager Last Below 40% SOC', 'mdi:battery-40', 40.0, 'soc_crossing_40'),
    ('sensor.home_energy_manager_last_below_20_soc', 'last_below_20_soc_at', 'Home Energy Manager Last Below 20% SOC', 'mdi:battery-20', 20.0, 'soc_crossing_20'),
    ('sensor.home_energy_manager_last_low_soc', 'last_low_soc_at', 'Home Energy Manager Last Low SOC', 'mdi:battery-low', None, 'soc_crossing_floor'),
)


def _battery_energy_entities(controller):
    """Resolve package-owned Home Forecaster battery meter configuration.

    The controller's normal discovery contract intentionally contains only the
    entities required for control. These two cumulative meters are observation-
    only inputs, so read the already-normalised Home Forecaster runtime options
    rather than expanding the controller's hard dependency set.
    """
    cached = getattr(controller, '_soc_energy_entities', None)
    if cached is not None:
        return cached
    charge = str(controller.c.get('battery_charge_energy_total_entity') or '').strip()
    discharge = str(controller.c.get('battery_discharge_energy_total_entity') or '').strip()
    if (not charge or not discharge) and _HOME_FORECASTER_OPTIONS.exists():
        try:
            raw = json.loads(_HOME_FORECASTER_OPTIONS.read_text())
            battery = raw.get('battery') if isinstance(raw.get('battery'), dict) else {}
            charge = charge or str(battery.get('battery_charge_energy_total_kwh') or '').strip()
            discharge = discharge or str(battery.get('battery_discharge_energy_total_kwh') or '').strip()
        except Exception as exc:
            core.LOG.warning('Could not read battery energy meter configuration: %s', exc)
    cached = (charge, discharge)
    controller._soc_energy_entities = cached
    return cached


def _store_soc_crossing(controller, threshold, key_prefix, previous_soc, soc, now_iso, elapsed_seconds):
    controller.db.set(f'last_below_{int(threshold)}_soc_at', now_iso)
    controller.db.set(f'{key_prefix}_soc_before', previous_soc)
    controller.db.set(f'{key_prefix}_soc_after', soc)
    controller.db.set(f'{key_prefix}_step_delta_pp', soc - previous_soc)
    controller.db.set(f'{key_prefix}_sample_seconds', elapsed_seconds)
    core.LOG.info(
        'SOC calibration observation: crossed %.0f%% downward %.2f%% -> %.2f%% (step=%+.2fpp sample=%ss)',
        threshold, previous_soc, soc, soc - previous_soc,
        'unknown' if elapsed_seconds is None else f'{elapsed_seconds:.1f}',
    )


def _history_start_for(crossing_at):
    target = crossing_at - timedelta(seconds=_SOC_WINDOW_HALF_SECONDS)
    candidates = [sample for sample in _soc_energy_history if sample['at'] <= target]
    return candidates[-1] if candidates else None


def _queue_energy_window(controller, threshold, key_prefix, crossing_at):
    start = _history_start_for(crossing_at)
    pending = {
        'threshold': threshold,
        'key_prefix': key_prefix,
        'crossing_at': core.iso(crossing_at),
        'complete_after': core.iso(crossing_at + timedelta(seconds=_SOC_WINDOW_HALF_SECONDS)),
        'start': None,
    }
    if start is not None:
        pending['start'] = {
            'at': core.iso(start['at']), 'soc': start['soc'],
            'charge_kwh': start['charge_kwh'], 'discharge_kwh': start['discharge_kwh'],
        }
    controller.db.set(f'{key_prefix}_energy_window_pending', pending)


def _valid_counter_delta(end_value, start_value):
    if end_value is None or start_value is None:
        return None
    delta = end_value - start_value
    return delta if delta >= 0 else None


def _complete_energy_window(controller, pending, sample, capacity_kwh):
    key_prefix = str(pending.get('key_prefix') or '')
    start = pending.get('start') if isinstance(pending.get('start'), dict) else None
    if not key_prefix or start is None:
        return False
    start_soc = core.as_float(start.get('soc'))
    end_soc = core.as_float(sample.get('soc'))
    charge_delta = _valid_counter_delta(core.as_float(sample.get('charge_kwh')), core.as_float(start.get('charge_kwh')))
    discharge_delta = _valid_counter_delta(core.as_float(sample.get('discharge_kwh')), core.as_float(start.get('discharge_kwh')))
    start_at = core.parse_dt(start.get('at'))
    end_at = sample.get('at')
    duration_seconds = None
    if start_at is not None and end_at is not None:
        try:
            duration_seconds = max(0.0, (end_at - start_at.astimezone(end_at.tzinfo)).total_seconds())
        except Exception:
            pass
    actual_delta_pp = None if start_soc is None or end_soc is None else end_soc - start_soc
    net_discharge_kwh = None if charge_delta is None or discharge_delta is None else discharge_delta - charge_delta
    expected_delta_pp = None
    if net_discharge_kwh is not None and capacity_kwh is not None and capacity_kwh > 0:
        expected_delta_pp = -(net_discharge_kwh / capacity_kwh) * 100.0
    correction_pp = None if actual_delta_pp is None or expected_delta_pp is None else actual_delta_pp - expected_delta_pp
    values = {
        'window_started_at': start.get('at'), 'window_ended_at': core.iso(end_at),
        'window_seconds': duration_seconds, 'window_soc_start': start_soc, 'window_soc_end': end_soc,
        'window_actual_soc_delta_pp': actual_delta_pp, 'window_charge_kwh': charge_delta,
        'window_discharge_kwh': discharge_delta, 'window_net_discharge_kwh': net_discharge_kwh,
        'window_capacity_kwh': capacity_kwh, 'window_expected_soc_delta_pp': expected_delta_pp,
        'window_correction_pp': correction_pp,
    }
    for suffix, value in values.items():
        controller.db.set(f'{key_prefix}_{suffix}', value)
    core.LOG.info(
        'SOC calibration energy window %.0f%%: duration=%ss SOC %.2f%% -> %.2f%% charge=%s kWh discharge=%s kWh expected=%spp actual=%spp correction=%spp',
        float(pending.get('threshold') or 0),
        'unknown' if duration_seconds is None else f'{duration_seconds:.0f}',
        start_soc if start_soc is not None else float('nan'), end_soc if end_soc is not None else float('nan'),
        'unknown' if charge_delta is None else f'{charge_delta:.4f}',
        'unknown' if discharge_delta is None else f'{discharge_delta:.4f}',
        'unknown' if expected_delta_pp is None else f'{expected_delta_pp:+.3f}',
        'unknown' if actual_delta_pp is None else f'{actual_delta_pp:+.3f}',
        'unknown' if correction_pp is None else f'{correction_pp:+.3f}',
    )
    return True


async def _entity_float(controller, entity_id):
    if not entity_id:
        return None
    st = await controller.ha.state(entity_id)
    return core.as_float(st.get('state')) if st else None


async def _ensure_without_charge_target(self, field, entity, desired, window_end):
    if field == 'charge_target':
        core.LOG.info('Charge target write suppressed: logical_target=%s entity=%s; charge quantity is controlled by duration/rate', desired, entity)
        return True
    return await _original_ensure(self, field, entity, desired, window_end)


async def _sample_with_soc_calibration_observation(self):
    await _original_sample(self)
    if not self.db.ok or not self.discovery_ready or not self.c.get('calibration_enabled', True):
        return
    st = await self.ha.state(self.c.get('battery_soc_entity', ''))
    soc = core.as_float(st.get('state')) if st else None
    if soc is None:
        return
    now = self.now(); now_iso = core.iso(now)
    floor = float(self.c.get('deep_cycle_floor_soc', 4))
    charge_entity, discharge_entity = _battery_energy_entities(self)
    charge_kwh = await _entity_float(self, charge_entity)
    discharge_kwh = await _entity_float(self, discharge_entity)
    capacity_kwh = await _entity_float(self, self.c.get('battery_capacity_entity', ''))
    current_sample = {'at': now, 'soc': soc, 'charge_kwh': charge_kwh, 'discharge_kwh': discharge_kwh}

    previous_soc = core.as_float(self.db.get('calibration_previous_soc'))
    previous_at = core.parse_dt(self.db.get('calibration_previous_soc_at'))
    elapsed_seconds = None
    if previous_at is not None:
        try: elapsed_seconds = max(0.0, (now - previous_at.astimezone(now.tzinfo)).total_seconds())
        except Exception: pass

    if previous_soc is not None and soc < previous_soc:
        for threshold, key_prefix in ((40.0, 'soc_crossing_40'), (20.0, 'soc_crossing_20')):
            if previous_soc > threshold and soc <= threshold:
                _store_soc_crossing(self, threshold, key_prefix, previous_soc, soc, now_iso, elapsed_seconds)
                _queue_energy_window(self, threshold, key_prefix, now)

    _soc_energy_history.append(current_sample)
    cutoff = now - timedelta(seconds=_SOC_HISTORY_SECONDS)
    while _soc_energy_history and _soc_energy_history[0]['at'] < cutoff:
        _soc_energy_history.popleft()

    for key_prefix in ('soc_crossing_40', 'soc_crossing_20'):
        pending_key = f'{key_prefix}_energy_window_pending'
        pending = self.db.get(pending_key)
        if not isinstance(pending, dict): continue
        complete_after = core.parse_dt(pending.get('complete_after'))
        if complete_after is None:
            self.db.set(pending_key, None); continue
        try: ready = now >= complete_after.astimezone(now.tzinfo)
        except Exception: ready = False
        if ready:
            if not _complete_energy_window(self, pending, current_sample, capacity_kwh):
                core.LOG.warning('SOC calibration energy window %s could not be completed; pre-crossing history or energy metering was unavailable', pending.get('threshold'))
            self.db.set(pending_key, None)

    low_active = bool(self.db.get('low_soc_visit_active'))
    if soc <= floor and not low_active:
        self.db.set('last_low_soc_at', now_iso); self.db.set('last_deep_calibration_at', now_iso); self.db.set('low_soc_visit_active', True)
        if previous_soc is not None:
            self.db.set('soc_crossing_floor_soc_before', previous_soc)
            self.db.set('soc_crossing_floor_soc_after', soc)
            self.db.set('soc_crossing_floor_step_delta_pp', soc - previous_soc)
            self.db.set('soc_crossing_floor_sample_seconds', elapsed_seconds)
        core.LOG.info('Calibration low SOC observed naturally/actively: soc=%.1f%% floor=%.1f%%; deep-cycle interval reset', soc, floor)
    elif soc > floor + 0.5 and low_active:
        self.db.set('low_soc_visit_active', False)
    self.db.set('calibration_previous_soc', soc); self.db.set('calibration_previous_soc_at', now_iso)


async def _publish_with_calibration_sensors(self, plan=None):
    await _original_publish(self, plan)
    floor_soc = float(self.c.get('deep_cycle_floor_soc', 4))
    charge_entity, discharge_entity = _battery_energy_entities(self)
    for entity_id, key, friendly_name, icon, fixed_soc, crossing_prefix in _CALIBRATION_SENSORS:
        value = self.db.get(key) if self.db.ok else None
        threshold = floor_soc if key in ('last_deep_calibration_at', 'last_low_soc_at') else fixed_soc
        attrs = {'friendly_name': friendly_name, 'device_class': 'timestamp', 'icon': icon, 'calibration_key': key, 'calibration_soc': threshold, 'battery_soc_entity': self.c.get('battery_soc_entity') or None}
        if crossing_prefix and self.db.ok:
            attrs.update({
                'soc_before': core.as_float(self.db.get(f'{crossing_prefix}_soc_before')),
                'soc_after': core.as_float(self.db.get(f'{crossing_prefix}_soc_after')),
                'soc_step_delta_pp': core.as_float(self.db.get(f'{crossing_prefix}_step_delta_pp')),
                'sample_seconds': core.as_float(self.db.get(f'{crossing_prefix}_sample_seconds')),
            })
            if crossing_prefix in ('soc_crossing_40', 'soc_crossing_20'):
                attrs.update({
                    'energy_window_minutes': 10,
                    'window_started_at': self.db.get(f'{crossing_prefix}_window_started_at'),
                    'window_ended_at': self.db.get(f'{crossing_prefix}_window_ended_at'),
                    'window_seconds': core.as_float(self.db.get(f'{crossing_prefix}_window_seconds')),
                    'window_soc_start': core.as_float(self.db.get(f'{crossing_prefix}_window_soc_start')),
                    'window_soc_end': core.as_float(self.db.get(f'{crossing_prefix}_window_soc_end')),
                    'window_actual_soc_delta_pp': core.as_float(self.db.get(f'{crossing_prefix}_window_actual_soc_delta_pp')),
                    'window_charge_kwh': core.as_float(self.db.get(f'{crossing_prefix}_window_charge_kwh')),
                    'window_discharge_kwh': core.as_float(self.db.get(f'{crossing_prefix}_window_discharge_kwh')),
                    'window_net_discharge_kwh': core.as_float(self.db.get(f'{crossing_prefix}_window_net_discharge_kwh')),
                    'window_capacity_kwh': core.as_float(self.db.get(f'{crossing_prefix}_window_capacity_kwh')),
                    'window_expected_soc_delta_pp': core.as_float(self.db.get(f'{crossing_prefix}_window_expected_soc_delta_pp')),
                    'window_correction_pp': core.as_float(self.db.get(f'{crossing_prefix}_window_correction_pp')),
                    'battery_charge_energy_total_entity': charge_entity or None,
                    'battery_discharge_energy_total_entity': discharge_entity or None,
                })
        state = value or 'unknown'
        if not self.mqtt.publish_sensor(entity_id, state, attrs):
            await self.ha.publish(entity_id, state, attrs)


core.Controller.ensure = _ensure_without_charge_target
core.Controller.sample = _sample_with_soc_calibration_observation
core.Controller.publish = _publish_with_calibration_sensors

if __name__ == '__main__':
    try: asyncio.run(core.main())
    except KeyboardInterrupt: pass
