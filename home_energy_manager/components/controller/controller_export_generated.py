"""Export Generated planning helpers.

This module is deliberately controller-local. It preserves the established
controller semantics while separating the generation-matched export calculations
from the controller composition/runtime code.
"""

import logging
from datetime import timedelta

from controller_utils import as_float, iso

LOG = logging.getLogger('home_energy_controller')


async def update_effective_export_accounting(controller):
    """Track fallback house-side export, excluding EV flow."""
    if not controller.db.ok or not controller.discovery_ready:
        return None
    if str(controller.c.get('grid_export_meter_source') or 'battery') == 'true_meter':
        return None

    export_entity = str(controller.c.get('grid_export_energy_total_entity', '')).strip()
    car_entity = str(controller.c.get('ev_smart_charging_active_entity', '')).strip()
    if not export_entity:
        return None

    est = await controller.ha.state(export_entity)
    if not est:
        return controller.db.get('effective_export_accounting')
    total = as_float(est.get('state'))
    if total is None:
        return controller.db.get('effective_export_accounting')

    cst = await controller.ha.state(car_entity) if car_entity else None
    car_on = bool(cst and str(cst.get('state', '')).lower() == 'on')
    now = controller.now()
    day = now.date().isoformat()

    accounting = controller.db.get('effective_export_accounting')
    if not isinstance(accounting, dict) or accounting.get('day') != day:
        accounting = {
            'day': day,
            'last_total_kwh': float(total),
            'last_at': iso(now),
            'last_car_on': bool(car_on),
            'credited_export_kwh': 0.0,
            'excluded_while_car_charging_kwh': 0.0,
            'tracking_started_at': iso(now),
            'pre_tracking_export_credited': False,
        }
        controller.db.set('effective_export_accounting', accounting)
        LOG.info(
            'Effective export accounting started: day=%s baseline_total=%.3fkWh car_on=%s; pre-tracking export not credited',
            day, float(total), 'yes' if car_on else 'no'
        )
        return accounting

    prev_total = as_float(accounting.get('last_total_kwh'))
    prev_car = bool(accounting.get('last_car_on'))
    credited = float(accounting.get('credited_export_kwh') or 0.0)
    excluded = float(accounting.get('excluded_while_car_charging_kwh') or 0.0)

    if prev_total is not None:
        delta = float(total) - float(prev_total)
        if 0.0 <= delta <= 10.0 and delta > 0:
            if car_on or prev_car:
                excluded += delta
                LOG.info(
                    'Effective export accounting: excluded %.3fkWh apparent export while car charging (credited=%.3f excluded=%.3f)',
                    delta, credited, excluded
                )
            else:
                credited += delta

    accounting.update({
        'last_total_kwh': float(total),
        'last_at': iso(now),
        'last_car_on': bool(car_on),
        'credited_export_kwh': round(credited, 6),
        'excluded_while_car_charging_kwh': round(excluded, 6),
    })
    controller.db.set('effective_export_accounting', accounting)
    return accounting


def effective_export_accounting(controller):
    if not controller.db.ok:
        return None
    accounting = controller.db.get('effective_export_accounting')
    if not isinstance(accounting, dict) or accounting.get('day') != controller.now().date().isoformat():
        return None
    return accounting


def export_generated_inputs(controller, state):
    attrs = state.get('attributes', {}) if state else {}
    today = attrs.get('today', {}) if isinstance(attrs, dict) else {}
    actual = today.get('actual', {}) if isinstance(today, dict) else {}
    total = today.get('total', {}) if isinstance(today, dict) else {}
    no_slots = attrs.get('no_slots_totals', {}) if isinstance(attrs, dict) else {}
    no_slots_today = no_slots.get('today', {}) if isinstance(no_slots, dict) else {}

    pv = as_float(total.get('pv_kwh')) if isinstance(total, dict) else None
    raw_house_export = as_float(actual.get('export_kwh')) if isinstance(actual, dict) else None
    natural_future = as_float(no_slots_today.get('export_kwh')) if isinstance(no_slots_today, dict) else None

    # Meter provenance belongs to this forecast payload, not mutable controller
    # discovery state. Keep the original precedence exactly.
    metering = attrs.get('metering') if isinstance(attrs.get('metering'), dict) else {}
    ci = attrs.get('controller_inputs') if isinstance(attrs.get('controller_inputs'), dict) else {}
    vals = ci.get('values') if isinstance(ci.get('values'), dict) else {}
    meter_source = str(
        metering.get('export_source')
        or vals.get('grid_export_meter_source')
        or controller.c.get('grid_export_meter_source')
        or 'battery'
    ).strip()
    meter_entity = str(
        metering.get('export_entity')
        or ((ci.get('entities') or {}).get('grid_export_energy_total') if isinstance(ci.get('entities'), dict) else '')
        or controller.c.get('grid_export_energy_total_entity')
        or ''
    ).strip()
    accounting = None if meter_source == 'true_meter' else effective_export_accounting(controller)

    if pv is None:
        return None
    if natural_future is None:
        natural_future = 0.0

    if meter_source == 'true_meter':
        credited = max(0.0, raw_house_export or 0.0)
    else:
        credited = as_float((accounting or {}).get('credited_export_kwh'))
        if credited is None:
            credited = 0.0

    solar_target = max(0.0, pv)
    exported_so_far = max(0.0, credited)
    forecast_natural_export = max(0.0, natural_future)
    remaining = max(0.0, solar_target - exported_so_far - forecast_natural_export)

    return {
        'solar_target_kwh': solar_target,
        'exported_so_far_kwh': exported_so_far,
        'raw_house_export_so_far_kwh': max(0.0, raw_house_export or 0.0),
        'excluded_while_car_charging_kwh': 0.0 if meter_source == 'true_meter' else max(0.0, as_float((accounting or {}).get('excluded_while_car_charging_kwh')) or 0.0),
        'export_meter_source': meter_source,
        'export_meter_entity': meter_entity,
        'raw_meter_export_so_far_kwh': max(0.0, raw_house_export or 0.0),
        'forecast_natural_export_kwh': forecast_natural_export,
        'remaining_kwh': remaining,
        'accounting_started_at': (accounting or {}).get('tracking_started_at'),
    }


def forecast_export_kwh_between(controller, state, start, end, attr='forecast_no_slots'):
    if not start or not end or end <= start:
        return 0.0
    total = 0.0
    for a, b, _rate, row in controller.intervals(state, attr):
        ov_start = max(a, start)
        ov_end = min(b, end)
        if ov_end <= ov_start:
            continue
        dur = max(1e-9, (b - a).total_seconds())
        frac = (ov_end - ov_start).total_seconds() / dur
        total += max(0.0, (as_float(row.get('export_kwh')) or 0.0) * frac)
    return total


def forced_incremental_grid_export_kwh(controller, state, start, end, rate_w):
    """Forecast extra grid export caused by a forced-discharge window."""
    if not start or not end or end <= start or rate_w <= 0:
        return 0.0
    total = 0.0
    covered = 0.0
    for a, b, _rate, row in controller.intervals(state, 'forecast_no_slots'):
        ov_start = max(a, start)
        ov_end = min(b, end)
        if ov_end <= ov_start:
            continue
        dur = max(1e-9, (b - a).total_seconds())
        ov = (ov_end - ov_start).total_seconds()
        frac = ov / dur
        load = (as_float(row.get('load_kwh')) or 0.0) * frac
        pv = (as_float(row.get('pv_kwh')) or 0.0) * frac
        natural_export = (as_float(row.get('export_kwh')) or 0.0) * frac
        forced_battery = (rate_w / 1000.0) * (ov / 3600.0)
        forced_export = max(0.0, forced_battery + pv - load)
        total += max(0.0, forced_export - natural_export)
        covered += ov

    wanted = (end - start).total_seconds()
    if covered < wanted - 1.0:
        gap = max(0.0, wanted - covered)
        _ = gap
    return total


def export_generated_end(controller, state, start, latest, remaining_kwh, rate_w):
    """Find the earliest minute-boundary end that meets remaining grid export."""
    if not start or not latest or latest <= start or remaining_kwh <= 0 or rate_w <= 0:
        return start, 0.0, False

    max_export = forced_incremental_grid_export_kwh(controller, state, start, latest, rate_w)
    if max_export + 1e-6 < remaining_kwh:
        return latest, max_export, False

    lo = start
    hi = latest
    for _ in range(28):
        mid = lo + (hi - lo) / 2
        got = forced_incremental_grid_export_kwh(controller, state, start, mid, rate_w)
        if got >= remaining_kwh:
            hi = mid
        else:
            lo = mid

    end = hi
    if end.second or end.microsecond:
        end = (end + timedelta(minutes=1)).replace(second=0, microsecond=0)
    else:
        end = end.replace(second=0, microsecond=0)
    end = min(latest.replace(second=0, microsecond=0), end)
    got = forced_incremental_grid_export_kwh(controller, state, start, end, rate_w)
    return end, got, got + 0.02 >= remaining_kwh


def export_generated_guardrail_end(controller, state, start, latest, rate_w, arrival_soc, cap, reserve):
    """Latest forced-export end that still preserves the arrival SOC guardrail."""
    if not start or not latest or latest <= start or rate_w <= 0 or cap <= 0:
        return start, 0.0
    buffer = max(float(reserve), float(controller.c.get('safety_buffer_soc', 20)))
    available_pct = max(0.0, float(arrival_soc) - buffer)
    if available_pct <= 1e-6:
        return start, 0.0

    def depletion(end):
        return controller.planned_discharge_soc_adjustment(state, start, end, rate_w, cap, reserve)

    if depletion(latest) <= available_pct + 1e-6:
        end = latest.replace(second=0, microsecond=0)
        return end, forced_incremental_grid_export_kwh(controller, state, start, end, rate_w)

    lo = start
    hi = latest
    for _ in range(28):
        mid = lo + (hi - lo) / 2
        if depletion(mid) <= available_pct:
            lo = mid
        else:
            hi = mid

    end = lo.replace(second=0, microsecond=0)
    if end < start:
        end = start.replace(second=0, microsecond=0)
    got = forced_incremental_grid_export_kwh(controller, state, start, end, rate_w)
    return end, got


def export_generated_topup_need(controller, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window):
    """Return extra stored battery kWh needed for the full export target."""
    if not export_info or cap <= 0 or discharge_rate_w <= 0 or not window:
        return {'needed_stored_kwh': 0.0, 'safe_export_kwh': 0.0, 'shortfall_kwh': 0.0,
                'target_reachable_without_topup': True}
    remaining = max(0.0, float(export_info.get('remaining_kwh') or 0.0))
    if remaining <= 0:
        return {'needed_stored_kwh': 0.0, 'safe_export_kwh': 0.0, 'shortfall_kwh': 0.0,
                'target_reachable_without_topup': True}

    buffer = max(float(reserve), float(controller.c.get('safety_buffer_soc', 20)))
    export_start = controller.export_start(window)
    latest = window['start']
    if latest <= export_start:
        return {'needed_stored_kwh': remaining, 'safe_export_kwh': 0.0, 'shortfall_kwh': remaining,
                'target_reachable_without_topup': False, 'target_reachable_with_full_topup': False}

    _safe_end, safe_export = export_generated_guardrail_end(
        controller, state, export_start, latest, discharge_rate_w, arrival_soc, cap, reserve
    )
    shortfall = max(0.0, remaining - safe_export)
    if shortfall <= 0.02:
        return {'needed_stored_kwh': 0.0, 'safe_export_kwh': safe_export, 'shortfall_kwh': 0.0,
                'target_reachable_without_topup': True}

    target_end, target_export, time_reachable = export_generated_end(
        controller, state, export_start, latest, remaining, discharge_rate_w
    )
    if not time_reachable:
        return {'needed_stored_kwh': 0.0, 'safe_export_kwh': safe_export,
                'shortfall_kwh': shortfall, 'target_reachable_without_topup': False,
                'target_reachable_with_full_topup': False,
                'max_export_with_topup_kwh': target_export}

    required_depletion_pct = controller.planned_discharge_soc_adjustment(
        state, export_start, target_end, discharge_rate_w, cap, reserve
    )
    required_arrival_soc = buffer + required_depletion_pct
    needed_pct = max(0.0, required_arrival_soc - float(arrival_soc))
    headroom_pct = max(0.0, 100.0 - float(arrival_soc))
    achievable_pct = min(needed_pct, headroom_pct)
    needed_stored = cap * achievable_pct / 100.0
    reachable_with_topup = needed_pct <= headroom_pct + 0.05

    return {'needed_stored_kwh': needed_stored, 'safe_export_kwh': safe_export,
            'shortfall_kwh': shortfall, 'target_reachable_without_topup': False,
            'target_reachable_with_full_topup': reachable_with_topup,
            'required_arrival_soc': min(100.0, required_arrival_soc),
            'max_export_with_topup_kwh': remaining if reachable_with_topup else safe_export}
