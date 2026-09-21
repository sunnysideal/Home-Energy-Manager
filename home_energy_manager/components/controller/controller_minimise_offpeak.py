"""Minimise-export regular off-peak pause/charge coordination."""
from datetime import datetime, timedelta

from controller_utils import as_float, clamp, iso


def active_or_next_offpeak(controller, window):
    active = controller.current_active_offpeak()
    now = controller.now()
    if active and active['start'] <= now < active['end']:
        return active, True
    local_start = window['start'].astimezone(controller.tz)
    local_end = window['end'].astimezone(controller.tz)
    previous_day = local_start.date() - timedelta(days=1)
    previous = {
        'start': datetime.combine(previous_day, local_start.timetz().replace(tzinfo=None), controller.tz),
        'end': datetime.combine(local_end.date() - timedelta(days=1), local_end.timetz().replace(tzinfo=None), controller.tz),
        'rate_p': window['rate_p'],
    }
    if previous['start'] <= now < previous['end']:
        return previous, True
    return window, False


async def coordinate_minimise_offpeak(controller, state, plan, window, fallback=False):
    """Schedule cheap-rate charging while ordinary Eco discharge remains enabled.

    The no-slots forecast includes household load/PV before charge start; when
    already in the cheap period, correct that forecast using current live SOC.
    """
    if fallback or not plan or controller.operation_mode() != 'minimise_export':
        return plan
    if plan.get('intelligent_go', {}).get('confirmed'):
        return plan
    if controller.calibration_state() in ('awaiting_deep_low', 'deep_recharge'):
        return plan

    control_window, active = active_or_next_offpeak(controller, window)
    target = as_float((plan.get('charge') or {}).get('target_soc'))
    if target is None:
        return plan

    capacity, _ = await controller.num('battery_capacity_entity', 'battery_capacity', True)
    max_charge, _ = await controller.num('inverter_max_charge_rate_entity', 'max_charge_rate', False)
    reserve, _ = await controller.num('battery_reserve_entity', 'battery_reserve', True)
    if capacity is None or capacity <= 0 or reserve is None:
        return plan

    now = controller.now()
    earliest = max(control_window['start'], now) if active else control_window['start']
    charge = dict(plan.get('charge') or {})
    target = clamp(float(target), float(reserve), 100.0)
    charge_end = control_window['end'].replace(second=0, microsecond=0)

    # Correct the no-forced-slots trajectory with current battery SOC during
    # the active cheap period, including real household consumption to date.
    adjustment = 0.0
    observed_soc = await controller.live_soc() if active else None
    forecast_now = controller.soc_at(state, now, 'forecast_no_slots') if active else None
    if observed_soc is not None and forecast_now is not None:
        adjustment = float(forecast_now) - float(observed_soc)

    projected_end_soc = controller.projected_charge_start_soc(
        state, charge_end, adjustment, float(reserve)
    )
    # No reliable projection: charge promptly instead of assuming the battery
    # will retain energy without a preservation pause.
    forecast_missing = projected_end_soc is None or (active and (observed_soc is None or forecast_now is None))
    needs_charge = forecast_missing or target > projected_end_soc + 0.5
    if not needs_charge:
        fallback_rate = max_charge if max_charge is not None else capacity * 250.0
        rate = int(round(as_float(charge.get('rate_w')) or fallback_rate))
        charge_start = charge_end
        start_soc = projected_end_soc
        planned_kwh = 0.0
    elif forecast_missing:
        # Missing forecast requires a conservative immediate cheap-rate charge.
        start_soc = clamp(float(observed_soc) if observed_soc is not None else float(reserve), float(reserve), 100.0)
        rate = controller.choose_rate(float(reserve), target, capacity, control_window, max_charge)
        charge_start = earliest.replace(second=0, microsecond=0)
        planned_kwh = capacity * max(0.0, target - start_soc) / 100.0
    else:
        # Solve start and rate against projected SOC at that actual start,
        # accounting for household load and solar prior to charging.
        rate, charge_start, start_soc, feasible = controller.choose_rate_and_start(
            state, control_window, target, capacity, float(reserve), adjustment,
            max_charge, earliest=earliest,
        )
        if start_soc is None:
            start_soc = float(reserve)
        if not feasible:
            # An unachievable target must not result in a late charge slot.
            charge_start = earliest
        charge_start = charge_start.replace(second=0, microsecond=0)
        planned_kwh = capacity * max(0.0, target - start_soc) / 100.0

    charge.update({
        'start': iso(charge_start),
        'end': iso(charge_end),
        'rate_w': int(round(rate)),
        'target_soc': int(round(target)),
        'planned_kwh': round(planned_kwh, 3),
    })
    plan['pause'] = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}
    plan['charge'] = charge
    forecast = dict(plan.get('forecast') or {})
    forecast['joint_pause_charge_plan'] = True
    forecast['joint_pause_charge_needs_charge'] = bool(needs_charge)
    forecast['projected_cheap_end_soc_no_charge'] = round(projected_end_soc, 1) if projected_end_soc is not None else None
    forecast['projected_charge_start_soc'] = round(start_soc, 1) if start_soc is not None else None
    plan['forecast'] = forecast
    controller.LOG.info(
        'Minimise export Eco offpeak plan: live_soc=%s projected_end_soc=%s target=%.1f%% charge=%s-%s rate=%dW planned=%.2fkWh',
        observed_soc, projected_end_soc, target,
        charge_start.strftime('%H:%M'), charge_end.strftime('%H:%M'), int(round(rate)), planned_kwh,
    )
    return plan
