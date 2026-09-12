# Runtime policy layer for the active Home Energy Controller.
#
# The core controller remains the common implementation for all normal modes.
# In minimise_export, this layer raises the ordinary overnight SOC floor to the
# amount required to carry the house from the end of the cheap window to the
# following regular cheap window while retaining the configured safety buffer.
# This is deliberately calculated with the core controller's existing reverse
# load/PV energy model, so time spent pinned at inverter reserve is not lost just
# because the no-slots arrival SOC cannot fall any further.

import asyncio
from datetime import datetime, timedelta

import app_core as core


_ORIGINAL_PLAN = core.Controller.plan


def _shift_local_day(controller, value, days):
    """Shift a tariff boundary by local calendar days, preserving wall clock.

    Using calendar-day reconstruction rather than timedelta(hours=24) keeps the
    regular tariff clock time correct across UK DST transitions.
    """
    local = value.astimezone(controller.tz)
    day = local.date() + timedelta(days=days)
    naive_time = local.timetz().replace(tzinfo=None)
    return datetime.combine(day, naive_time, controller.tz)


def _current_regular_offpeak(controller, next_window):
    """Return the active regular cheap window when the forecast skipped it.

    Home Energy Forecaster intentionally exposes the *next* regular off-peak
    window. While already inside today's cheap block that is tomorrow's block,
    so infer today's matching block by one local calendar day. Prefer the core
    controller's persisted active window when available.
    """
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


async def _plan_with_minimise_peak_protection(self, forecast, soc, window, fallback=False):
    original_floor = self.c.get('minimise_export_min_soc', 25)
    diagnostics = None

    if not fallback and self.operation_mode() == 'minimise_export':
        capacity, _ = await self.num('battery_capacity_entity', 'battery_capacity', True)
        reserve, _ = await self.num('battery_reserve_entity', 'battery_reserve', True)

        if capacity is not None and reserve is not None and capacity > 0:
            active = _current_regular_offpeak(self, window)
            if active:
                # We are charging/preserving energy in the currently active
                # cheap block. Protect the peak interval from this block's end
                # until the already tariff-derived next off-peak start.
                bridge_start = active['end']
                next_offpeak_start = window['start']
            else:
                # Before the upcoming cheap block, size tonight's target for the
                # *following* peak period, not the peak period that is already in
                # progress. The forecast horizon is 48h, so this boundary is
                # available in normal operation.
                bridge_start = window['end']
                next_offpeak_start = _shift_local_day(self, window['start'], 1)

            protected_soc, required_peak_kwh, complete = self.power_down_protected_soc(
                forecast, bridge_start, next_offpeak_start, capacity, reserve
            )

            configured_floor = float(original_floor)
            dynamic_floor = int(round(core.clamp(
                max(configured_floor, protected_soc),
                float(reserve),
                100.0,
            )))
            self.c['minimise_export_min_soc'] = dynamic_floor

            diagnostics = {
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

    if result is not None and diagnostics is not None:
        result['minimise_export'] = diagnostics
    return result


core.Controller.plan = _plan_with_minimise_peak_protection


if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
