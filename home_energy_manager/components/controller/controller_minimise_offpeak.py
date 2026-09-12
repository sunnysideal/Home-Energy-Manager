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
    """Plan regular cheap-rate preservation and charging as one sequence.

    PauseBoth means household load does not consume battery energy. Therefore a
    delayed charge is sized from the SOC being preserved, not from the no-slots
    SOC later in the cheap window. The resulting sequence is PauseBoth -> Charge,
    with either phase allowed to have zero duration.
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

    preserved_soc = await controller.live_soc() if active else None
    if preserved_soc is None:
        preserved_soc = as_float(state.get('attributes', {}).get('overnight_start_soc_no_slots'))
    if preserved_soc is None:
        preserved_soc = controller.soc_at(state, control_window['start'], 'forecast_no_slots')
    if preserved_soc is None:
        return plan
    preserved_soc = clamp(float(preserved_soc), float(reserve), 100.0)
    target = clamp(float(target), float(reserve), 100.0)

    now = controller.now()
    earliest = max(control_window['start'], now) if active else control_window['start']
    charge = dict(plan.get('charge') or {})
    safety_minutes = max(0.0, float(controller.c.get('charge_safety_margin_minutes', 10)))
    needs_charge = target > preserved_soc + 0.5

    if needs_charge:
        rate = controller.choose_rate(preserved_soc, target, capacity, control_window, max_charge)
        charge_minutes = controller.charge_minutes(preserved_soc, target, rate, capacity) + safety_minutes
        charge_start = max(earliest, control_window['end'] - timedelta(minutes=charge_minutes))
        charge_end = control_window['end'].replace(second=0, microsecond=0)
        planned_kwh = capacity * max(0.0, target - preserved_soc) / 100.0
    else:
        fallback_rate = max_charge if max_charge is not None else capacity * 250.0
        rate = int(round(as_float(charge.get('rate_w')) or fallback_rate))
        charge_start = control_window['end'].replace(second=0, microsecond=0)
        charge_end = charge_start
        planned_kwh = 0.0

    pause_start = control_window['start'].replace(second=0, microsecond=0)
    pause_end = charge_start.replace(second=0, microsecond=0)
    if pause_end > pause_start:
        pause = {'mode': 'PauseBoth', 'start': controller.tstr(pause_start), 'end': controller.tstr(pause_end)}
    else:
        pause = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}

    charge.update({
        'start': iso(charge_start.replace(second=0, microsecond=0)),
        'end': iso(charge_end),
        'rate_w': int(round(rate)),
        'target_soc': int(round(target)),
        'planned_kwh': round(planned_kwh, 3),
    })
    plan['pause'] = pause
    plan['charge'] = charge
    forecast = dict(plan.get('forecast') or {})
    forecast['preserved_offpeak_start_soc'] = round(preserved_soc, 1)
    forecast['joint_pause_charge_plan'] = True
    forecast['joint_pause_charge_needs_charge'] = bool(needs_charge)
    plan['forecast'] = forecast
    controller.LOG.info(
        'Minimise export joint offpeak plan: preserved_soc=%.1f%% target=%.1f%% pause=%s-%s charge=%s-%s rate=%dW planned=%.2fkWh',
        preserved_soc, target, pause['start'], pause['end'],
        charge_start.strftime('%H:%M'), charge_end.strftime('%H:%M'), int(round(rate)), planned_kwh,
    )
    return plan
