# Runtime policy layer for the active Home Energy Controller.
#
# The core controller remains the common implementation. This layer adds:
#   * minimise_export peak-period energy protection; and
#   * a qualifying Axle Export-event overlay for every active optimisation mode.
#
# Axle is deliberately a plan overlay rather than a second controller. When the
# event disappears, the next cycle simply applies a fresh underlying normal plan.

import asyncio
from datetime import datetime, timedelta
import math
import os

import app_core as core

core.VERSION = os.environ.get('HOME_ENERGY_MANAGER_VERSION', core.VERSION).strip() or core.VERSION
_ORIGINAL_PLAN = core.Controller.plan


def _shift_local_day(controller, value, days):
    """Shift a tariff boundary by local calendar days, preserving wall clock."""
    local = value.astimezone(controller.tz)
    day = local.date() + timedelta(days=days)
    naive_time = local.timetz().replace(tzinfo=None)
    return datetime.combine(day, naive_time, controller.tz)


def _current_regular_offpeak(controller, next_window):
    """Return the active regular cheap window when the forecast skipped it."""
    active = controller.current_active_offpeak()
    now = controller.now()
    if active and active['start'] <= now < active['end']:
        return active
    previous = {
        'start': _shift_local_day(controller, next_window['start'], -1),
        'end': _shift_local_day(controller, next_window['end'], -1),
        'rate_p': next_window['rate_p'],
    }
    if previous['start'] <= now < previous['end']:
        return previous
    return None


def _event_from_state(controller, state):
    attrs = state.get('attributes', {}) if isinstance(state, dict) else {}
    event_type = str(attrs.get('event_type') or '').strip().lower()
    start = core.parse_dt(attrs.get('start'))
    end = core.parse_dt(attrs.get('end'))
    available = bool(attrs.get('event_available'))
    if not available or event_type != 'export' or not start or not end or end <= start:
        return None
    return {
        'start': start.astimezone(controller.tz),
        'end': end.astimezone(controller.tz),
        'event_type': event_type,
    }


def _forecast_net_kwh(controller, forecast, start, end):
    """Return load-minus-PV energy and whether forecast fully covers the interval."""
    if not start or not end or end <= start:
        return 0.0, True
    segments = controller.forecast_net_segments(forecast, start, end, 'forecast_no_slots')
    covered = sum(max(0.0, (b - a).total_seconds()) for a, b, _load, _pv in segments)
    wanted = (end - start).total_seconds()
    net = sum(float(load) - float(pv) for _a, _b, load, pv in segments)
    return net, covered >= wanted - 1.0


def _disabled_discharge(controller, plan, anchor):
    """Suppress normal forced discharge without changing the reserve target."""
    t = anchor.replace(second=0, microsecond=0)
    plan['discharge'] = {
        'start': core.iso(t),
        'end': core.iso(t),
        'rate_w': plan.get('discharge', {}).get('rate_w', 0),
        'target_soc': plan.get('discharge', {}).get('target_soc'),
        'planned_kwh': 0.0,
        'kind': 'axle_protection',
    }


async def _apply_axle_overlay(controller, forecast, plan, window, capacity, reserve, max_charge, max_discharge):
    """Overlay Axle preparation/export on a freshly calculated normal plan."""
    axle_entity = str(controller.c.get('axle_entity', 'sensor.home_energy_manager_axle')).strip()
    axle_state = await controller.ha.state(axle_entity)
    event = _event_from_state(controller, axle_state)
    if not event:
        return plan

    now = controller.now()
    start, end = event['start'], event['end']
    if end <= now:
        return plan

    discharge_eff = max(0.5, min(1.0, float(controller.c.get('axle_discharge_efficiency', 0.95))))
    charge_eff = 0.95
    duration_h = (end - start).total_seconds() / 3600.0
    required_kwh = (float(max_discharge) / 1000.0) * duration_h / discharge_eff
    raw_required_soc = float(reserve) + required_kwh / float(capacity) * 100.0
    required_soc = min(100.0, raw_required_soc)
    full_event_possible = raw_required_soc <= 100.0

    diagnostics = {
        'entity': axle_entity,
        'event_type': 'export',
        'event_start': core.iso(start),
        'event_end': core.iso(end),
        'required_event_start_soc': round(required_soc, 1),
        'full_event_possible': bool(full_event_possible),
        'max_discharge_rate_w': int(round(max_discharge)),
        'action': 'protecting',
    }

    # A confirmed EV Smart Charging settlement half-hour is always a no-export
    # period. It may charge if the ordinary controller decides that is useful;
    # Axle export resumes on the next fresh plan after the half-hour.
    intelligent = await controller.intelligent_go_info()
    confirmed_intelligent = bool(intelligent.get('confirmed'))

    if start <= now < end:
        if confirmed_intelligent:
            _disabled_discharge(controller, plan, now)
            diagnostics['action'] = 'suspended_for_confirmed_ev_smart_charging'
        else:
            # Active paid event: no simultaneous scheduled charge and no pause
            # mode that could inhibit forced discharge.
            plan['charge'] = dict(plan.get('charge', {}))
            plan['charge']['start'] = core.iso(now.replace(second=0, microsecond=0))
            plan['charge']['end'] = plan['charge']['start']
            plan['charge']['planned_kwh'] = 0.0
            plan['pause'] = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}
            plan['discharge'] = {
                'start': core.iso(start),
                'end': core.iso(end),
                'rate_w': int(round(max_discharge)),
                'target_soc': int(round(reserve)),
                'planned_kwh': round((float(max_discharge) / 1000.0) * max(0.0, (end - now).total_seconds()) / 3600.0, 3),
                'kind': 'axle_export',
            }
            diagnostics['action'] = 'axle_export'
        plan['axle'] = diagnostics
        return plan

    # Upcoming Axle event. Normal forced export is lower priority than retaining
    # enough battery energy to deliver the paid event, so suppress it while the
    # event requirement is being protected.
    _disabled_discharge(controller, plan, now)

    # If the regular cheap window completes before the event, make that cheap
    # charge do the preparation. Include forecast load-minus-PV between cheap end
    # and event start so required SOC is available at the event, not merely at
    # cheap-window end.
    cheap_before_event = window['start'] < start and window['end'] <= start
    if cheap_before_event:
        net_kwh, complete = _forecast_net_kwh(controller, forecast, window['end'], start)
        if complete:
            bridge_stored_kwh = max(0.0, net_kwh) / discharge_eff
            cheap_target = min(100.0, required_soc + bridge_stored_kwh / float(capacity) * 100.0)
        else:
            cheap_target = 100.0
        existing_target = core.as_float(plan.get('charge', {}).get('target_soc')) or 0.0
        plan['charge']['target_soc'] = int(math.ceil(max(existing_target, cheap_target)))
        diagnostics.update({
            'action': 'prepare_in_regular_offpeak',
            'pre_event_bridge_net_kwh': round(net_kwh, 3),
            'forecast_coverage_complete': bool(complete),
            'charge_target_soc': plan['charge']['target_soc'],
        })
        plan['axle'] = diagnostics
        return plan

    # Event occurs before the next regular cheap opportunity. Use forecast SOC at
    # event start to decide whether intervention is needed. Do not peak-charge
    # early: wait until the latest practical start, then charge only the shortfall.
    forecast_event_soc = controller.soc_at(forecast, start, 'forecast_no_slots')
    live_soc = await controller.live_soc()
    if forecast_event_soc is None:
        forecast_event_soc = live_soc
        coverage_complete = False
    else:
        coverage_complete = True
    if forecast_event_soc is None:
        forecast_event_soc = float(reserve)
        coverage_complete = False

    shortfall_pct = max(0.0, required_soc - float(forecast_event_soc))
    shortfall_kwh = float(capacity) * shortfall_pct / 100.0
    stored_kw = max(0.1, (float(max_charge) / 1000.0) * charge_eff)
    charge_hours = shortfall_kwh / stored_kw
    margin = max(0, int(controller.c.get('axle_charge_margin_minutes', 10)))
    latest_charge_start = start - timedelta(hours=charge_hours, minutes=margin)
    diagnostics.update({
        'forecast_event_start_soc': round(float(forecast_event_soc), 1),
        'forecast_coverage_complete': bool(coverage_complete),
        'shortfall_kwh': round(shortfall_kwh, 3),
        'latest_charge_start': core.iso(latest_charge_start),
    })

    if shortfall_pct <= 0.5:
        diagnostics['action'] = 'ready'
    elif now < latest_charge_start:
        diagnostics['action'] = 'waiting_to_prepare'
    else:
        charge_end = min(start, now + timedelta(hours=charge_hours, minutes=2))
        plan['charge'] = {
            'start': core.iso(now.replace(second=0, microsecond=0)),
            'end': core.iso(charge_end),
            'rate_w': int(round(max_charge)),
            'target_soc': int(math.ceil(required_soc)),
            'planned_kwh': round(shortfall_kwh / charge_eff, 3),
        }
        plan['pause'] = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}
        diagnostics['action'] = 'peak_prepare_last_resort'
        diagnostics['charge_target_soc'] = plan['charge']['target_soc']

    plan['axle'] = diagnostics
    return plan


async def _plan_with_policies(self, forecast, soc, window, fallback=False):
    original_floor = self.c.get('minimise_export_min_soc', 25)
    minimise_diagnostics = None
    capacity = reserve = max_charge = max_discharge = None

    if not fallback and self.operation_mode() == 'minimise_export':
        capacity, _ = await self.num('battery_capacity_entity', 'battery_capacity', True)
        reserve, _ = await self.num('battery_reserve_entity', 'battery_reserve', True)
        if capacity is not None and reserve is not None and capacity > 0:
            active = _current_regular_offpeak(self, window)
            if active:
                bridge_start = active['end']
                next_offpeak_start = window['start']
            else:
                bridge_start = window['end']
                next_offpeak_start = _shift_local_day(self, window['start'], 1)

            protected_soc, required_peak_kwh, complete = self.power_down_protected_soc(
                forecast, bridge_start, next_offpeak_start, capacity, reserve
            )
            configured_floor = float(original_floor)
            dynamic_floor = int(round(core.clamp(
                max(configured_floor, protected_soc), float(reserve), 100.0
            )))
            self.c['minimise_export_min_soc'] = dynamic_floor
            minimise_diagnostics = {
                'configured_floor_soc': int(round(configured_floor)),
                'required_target_soc': dynamic_floor,
                'protected_arrival_soc': int(round(max(float(reserve), float(self.c.get('safety_buffer_soc', 20))))),
                'peak_energy_required_kwh': round(float(required_peak_kwh), 3),
                'bridge_start': core.iso(bridge_start),
                'next_offpeak_start': core.iso(next_offpeak_start),
                'forecast_coverage_complete': bool(complete),
                'strategy': 'avoid_peak_import_to_next_regular_offpeak',
            }
            core.LOG.info(
                'Minimise export overnight protection: configured_floor=%d%% target=%d%% bridge=%s->%s peak_energy=%.2fkWh coverage=%s',
                int(round(configured_floor)), dynamic_floor,
                bridge_start.strftime('%Y-%m-%d %H:%M'),
                next_offpeak_start.strftime('%Y-%m-%d %H:%M'),
                float(required_peak_kwh), 'complete' if complete else 'incomplete'
            )

    try:
        result = await _ORIGINAL_PLAN(self, forecast, soc, window, fallback=fallback)
    finally:
        self.c['minimise_export_min_soc'] = original_floor

    if result is None:
        return None
    if minimise_diagnostics is not None:
        result['minimise_export'] = minimise_diagnostics

    # Launcher routes forecast_only and axle_only elsewhere, so this runtime only
    # sees the three active optimisation modes. Keep the mode check explicit as a
    # regression guard against future launcher changes.
    if not fallback and self.operation_mode() in ('minimise_export', 'maximise_export', 'export_generated'):
        if capacity is None:
            capacity, _ = await self.num('battery_capacity_entity', 'battery_capacity', True)
        if reserve is None:
            reserve, _ = await self.num('battery_reserve_entity', 'battery_reserve', True)
        max_charge, _ = await self.num('inverter_max_charge_rate_entity', 'max_charge_rate', False)
        max_discharge, _ = await self.num('inverter_max_discharge_rate_entity', 'max_discharge_rate', False)
        if None not in (capacity, reserve, max_charge, max_discharge) and capacity > 0 and max_charge > 0 and max_discharge > 0:
            result = await _apply_axle_overlay(
                self, forecast, result, window,
                float(capacity), float(reserve), float(max_charge), float(max_discharge)
            )

    return result


core.Controller.plan = _plan_with_policies

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
