"""Tariff/off-peak window helpers for the controller.

The Home Energy Forecaster remains authoritative for tariff interpretation.
This controller-local module only validates, persists, restores and identifies
the tariff-derived off-peak window supplied through the forecast interface.
"""

from datetime import datetime, timedelta, timezone

from controller_utils import as_float, iso, parse_dt


def offpeak_from_forecast(controller, state):
    offpeak = state.get('attributes', {}).get('offpeak')
    if not isinstance(offpeak, dict):
        return None
    start = parse_dt(offpeak.get('start'))
    end = parse_dt(offpeak.get('end'))
    rate = as_float(offpeak.get('rate_p'))
    if not start or not end or rate is None or end <= start:
        return None
    # Incomplete next-day tariff data may expose only a cheap 23:00-00:00
    # fragment. Reject it before planning or persisting an inverter window.
    # Compare real UTC duration so spring/autumn transitions remain valid.
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    local_start, local_end = start.astimezone(controller.tz), end.astimezone(controller.tz)
    overnight = local_start.date() < local_end.date() or (
        local_start.date() == local_end.date() and local_start.hour < 3
    )
    if not (timedelta(hours=2) <= elapsed <= timedelta(hours=10)
            and local_end.hour <= 8 and overnight):
        return None
    return {
        'start': start.astimezone(controller.tz),
        'end': end.astimezone(controller.tz),
        'rate_p': rate,
        'source': str(offpeak.get('source') or 'unverified'),
    }


def persist_offpeak(controller, window):
    controller.db.set(
        'next_offpeak',
        {
            'start': iso(window['start']),
            'end': iso(window['end']),
            'rate_p': window['rate_p'],
            'source': window.get('source', 'unverified'),
        },
    )


def fallback_offpeak(controller):
    row = controller.db.get('next_offpeak') if controller.db.ok else None
    if not isinstance(row, dict):
        return None
    start = parse_dt(row.get('start'))
    end = parse_dt(row.get('end'))
    rate = as_float(row.get('rate_p'))
    if not start or not end or rate is None:
        return None
    start = start.astimezone(controller.tz)
    end = end.astimezone(controller.tz)
    now = controller.now()
    while end.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        start_day = start.date() + timedelta(days=1)
        end_day = end.date() + timedelta(days=1)
        start = datetime.combine(start_day, start.timetz().replace(tzinfo=None), controller.tz)
        end = datetime.combine(end_day, end.timetz().replace(tzinfo=None), controller.tz)
    return {'start': start, 'end': end, 'rate_p': rate, 'source': str(row.get('source') or 'unverified')}


def current_active_offpeak(controller):
    now = controller.now()
    candidates = []
    if controller.db.ok:
        for key in ('status_plan', 'next_offpeak'):
            row = controller.db.get(key)
            if key == 'status_plan' and isinstance(row, dict):
                row = row.get('offpeak')
            if not isinstance(row, dict):
                continue
            start = parse_dt(row.get('start'))
            end = parse_dt(row.get('end'))
            rate = as_float(row.get('rate_p'))
            if start and end and rate is not None:
                start = start.astimezone(controller.tz)
                end = end.astimezone(controller.tz)
                if start <= now < end:
                    candidates.append({'start': start, 'end': end, 'rate_p': rate})
    return max(candidates, key=lambda item: item['start']) if candidates else None
