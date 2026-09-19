"""Conservative manual-price override policy.

Manual windows are supplied by Home Forecaster; this module does not interpret
supplier tariffs or simulate the physical battery. Never overrides paid events.
"""
from datetime import timedelta
import math
from controller_utils import parse_dt, iso, as_float


def qualifying_windows(state, normal_rate, now):
    rows = (state.get('attributes') or {}).get('manual_import_overrides') or []
    if not isinstance(rows, list) or normal_rate is None:
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        a, b = parse_dt(row.get('start')), parse_dt(row.get('end'))
        price = as_float(row.get('rate_p'))
        if (a is None or b is None or a.utcoffset() is None or
                b.utcoffset() is None or b <= a or b <= now or
                price is None or not math.isfinite(price) or price < 0 or
                price >= normal_rate):
            continue
        parsed.append((a, b, price))
    if not parsed:
        return []
    # Split overlaps at every boundary and choose the cheapest explicit price.
    edges = sorted({point for a, b, _ in parsed for point in (max(a, now), b)})
    result = []
    for a, b in zip(edges, edges[1:]):
        prices = [p for start, end, p in parsed if start <= a and end >= b]
        if not prices:
            continue
        price = min(prices)
        if result and result[-1]['end'] == a and result[-1]['rate_p'] == price:
            result[-1]['end'] = b
        else:
            result.append({'start': a, 'end': b, 'rate_p': price})
    return result


def covered_segments(controller, state, start, end):
    segments = controller.forecast_net_segments(state, start, end, 'forecast_no_slots')
    if not segments or segments[0][0] > start + timedelta(seconds=1):
        return None
    cursor = start
    for a, b, _load, _pv in segments:
        if a > cursor + timedelta(seconds=1):
            return None
        cursor = max(cursor, b)
    return segments if cursor >= end - timedelta(seconds=1) else None


async def apply_cheaper_manual_override(controller, state, plan, window, forecast_soc):
    now = controller.now()
    normal = as_float(window.get('rate_p'))
    windows = qualifying_windows(state, normal, now)
    diag = {'qualifying_windows': [
        {'start': iso(x['start']), 'end': iso(x['end']), 'rate_p': x['rate_p']}
        for x in windows], 'normal_offpeak_rate_p': normal, 'action': 'none'}
    plan['manual_import_override'] = diag
    if not windows:
        return plan
    mode = controller.operation_mode()
    if mode not in ('minimise_export', 'maximise_export', 'export_generated'):
        diag['reason'] = 'passive_mode'
        return plan
    active = next((x for x in windows if x['start'] <= now < x['end']), None)
    # Paid events, calibration and confirmed EV charging retain their priority.
    if (plan.get('intelligent_go') or {}).get('confirmed') or (plan.get('power_down') or {}).get('active'):
        diag['reason'] = 'higher_priority_event'
        return plan
    calibration = plan.get('calibration') or {}
    if calibration.get('state') in ('awaiting_deep_low', 'deep_recharge', 'top_due'):
        diag['reason'] = 'calibration'
        return plan
    discharge = plan.get('discharge') or {}
    ds, de = parse_dt(discharge.get('start')), parse_dt(discharge.get('end'))
    if active and ds and de and ds <= now < de and (as_float(discharge.get('planned_kwh')) or 0) > 0:
        diag['reason'] = 'active_forced_discharge'
        return plan
    capacity, _ = await controller.num('battery_capacity_entity', 'battery_capacity', True)
    reserve, _ = await controller.num('battery_reserve_entity', 'battery_reserve', True)
    max_charge, _ = await controller.num('inverter_max_charge_rate_entity', 'max_charge_rate', False)
    if not active:
        # Never reduce the overnight target on an incomplete forecast or without
        # a validated charge-power budget. Retain the established safe plan.
        diag['reason'] = 'future_override_regular_charge_retained'
        return plan
    diag.update(start=iso(active['start']), end=iso(active['end']),
                rate_p=active['rate_p'])
    if None in (capacity, reserve, max_charge) or capacity <= 0 or max_charge <= 0:
        diag['reason'] = 'missing_battery_inputs'
        return plan
    soc_state = await controller.ha.state(controller.c.get('battery_soc_entity', ''))
    live_soc = as_float(soc_state.get('state')) if soc_state else None
    if live_soc is None:
        diag['reason'] = 'missing_live_soc'
        return plan
    # Do not overlap a normal overnight slot: preserve its existing schedule.
    regular_start, regular_end = parse_dt(window.get('start')), parse_dt(window.get('end'))
    if regular_start and regular_end and regular_start <= now < regular_end:
        diag['reason'] = 'regular_offpeak_active'
        return plan
    safety = max(float(reserve), float(controller.c.get('safety_buffer_soc', 20)))
    headroom = max(0., float(capacity) * (100. - live_soc) / 100.)
    remaining = max(0., (active['end'] - now).total_seconds() / 3600.)
    possible = min(headroom, float(max_charge) * remaining / 1000. * .9)
    # Only top up an evidenced shortfall at the next regular cheap start.
    bridge_end = regular_start
    if bridge_end is None or bridge_end <= now:
        diag['reason'] = 'next_regular_offpeak_unknown'
        return plan
    segments = covered_segments(controller, state, now, bridge_end)
    if segments is None:
        diag['reason'] = 'incomplete_bridge_forecast'
        return plan
    demand = sum(max(0., load - pv) for _a, _b, load, pv in segments)
    required = min(headroom, max(0., demand + capacity * safety / 100. -
                                   capacity * live_soc / 100.))
    useful = min(possible, required)
    diag.update(bridge_demand_kwh=round(demand, 3),
                useful_charge_kwh=round(useful, 3),
                live_soc=live_soc, safety_soc=safety)
    if useful <= .05:
        # A temporary PauseDischarge avoids consuming stored energy at the
        # cheaper grid price; do not erase a future normal charge slot.
        plan['pause'] = {'mode': 'PauseDischarge',
                         'start': controller.tstr(now),
                         'end': controller.tstr(active['end'])}
        diag['action'] = 'preserve_grid_supply'
        return plan
    rate = min(float(max_charge), float(capacity) * 1000. *
               float(controller.c.get('max_charge_c_rate', .4)))
    if rate <= 0:
        diag['reason'] = 'no_charge_rate'
        return plan
    duration = timedelta(hours=useful / (.9 * rate / 1000.))
    end = min(active['end'], now + duration)
    if end <= now + timedelta(minutes=1):
        diag['reason'] = 'charge_duration_too_short'
        return plan
    plan['charge'] = {'start': iso(now.replace(second=0, microsecond=0)),
                      'end': iso(end.replace(second=0, microsecond=0)),
                      'rate_w': int(rate),
                      'target_soc': int(min(100, math.ceil(live_soc + useful * 100. / capacity))),
                      'planned_kwh': round(useful, 3),
                      'kind': 'manual_import_override'}
    plan['pause'] = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}
    diag['action'] = 'charge'
    return plan
