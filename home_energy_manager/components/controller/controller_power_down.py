"""Power Down planning helpers.

This controller-local module preserves the established paid Power Down event
behaviour while separating its forecast protection and event interpretation
from the compatibility core.
"""

from controller_utils import as_float, clamp, parse_dt


def power_down_protected_soc(controller, state, session_end, next_offpeak_start, cap, reserve):
    """SOC required at Power Down end to avoid later peak import."""
    buffer = max(float(reserve), float(controller.c.get('safety_buffer_soc', 20)))
    if not session_end or not next_offpeak_start or session_end >= next_offpeak_start:
        return buffer, 0.0, True
    reserve_kwh = cap * float(reserve) / 100.0
    required = cap * buffer / 100.0
    segs = controller.forecast_net_segments(
        state, session_end, next_offpeak_start, 'forecast_no_slots'
    )
    covered = sum((b - a).total_seconds() for a, b, _, _ in segs)
    wanted = (next_offpeak_start - session_end).total_seconds()
    if wanted > 60 and covered < wanted - 60:
        return 100.0, max(0.0, cap * (100.0 - buffer) / 100.0), False
    for _a, _b, load, pv in reversed(segs):
        net = load - pv
        if net >= 0:
            required += net / 0.95
        else:
            required = max(reserve_kwh, required - (-net) * 0.95)
    achievable = required <= cap + 1e-6
    required = min(cap, required)
    extra = max(0.0, required - cap * buffer / 100.0)
    return clamp(required / cap * 100.0, reserve, 100.0), extra, achievable


async def power_down_info(controller, state, window, cap, reserve, discharge_rate_w):
    """Read joined Power Down sessions and build the established plan input."""
    if not controller.c.get('power_down_enabled', True):
        return None
    event_entity = str(controller.c.get('power_down_events_entity', '')).strip()
    if not event_entity:
        return None
    event_state = await controller.ha.state(event_entity)
    if not event_state:
        controller.err('input', 'power_down_events', 'Configured Power Down events entity unavailable', 35)
        return None
    attrs = event_state.get('attributes', {}) if isinstance(event_state, dict) else {}
    joined = attrs.get('joined_events')
    if not isinstance(joined, list):
        controller.err('input', 'power_down_events', 'Power Down events entity has no joined_events attribute', 35)
        return None
    controller.clear('input', 'power_down_events')
    now = controller.now()
    candidates = []
    for event in joined:
        if not isinstance(event, dict):
            continue
        start = parse_dt(event.get('start'))
        end = parse_dt(event.get('end'))
        if not start or not end or end <= start:
            continue
        start = start.astimezone(controller.tz)
        end = end.astimezone(controller.tz)
        if end <= now:
            continue
        candidates.append((start, end, event))
    if not candidates:
        return None
    start, end, event = min(candidates, key=lambda item: item[0])

    import_baseline_total = None
    export_baseline_total = None
    import_baseline_incomplete = None
    export_baseline_incomplete = None

    import_baseline_entity = str(controller.c.get('power_down_import_baseline_entity', '')).strip()
    export_baseline_entity = str(controller.c.get('power_down_export_baseline_entity', '')).strip()

    if import_baseline_entity:
        baseline_state = await controller.ha.state(import_baseline_entity)
        if baseline_state:
            baseline_attrs = baseline_state.get('attributes', {}) if isinstance(baseline_state, dict) else {}
            import_baseline_total = as_float(baseline_attrs.get('total_baseline'))
            import_baseline_incomplete = baseline_attrs.get('is_incomplete_calculation')

    if export_baseline_entity:
        baseline_state = await controller.ha.state(export_baseline_entity)
        if baseline_state:
            baseline_attrs = baseline_state.get('attributes', {}) if isinstance(baseline_state, dict) else {}
            export_baseline_total = as_float(baseline_attrs.get('total_baseline'))
            export_baseline_incomplete = baseline_attrs.get('is_incomplete_calculation')

    net_baseline_total = None
    if import_baseline_total is not None or export_baseline_total is not None:
        net_baseline_total = (import_baseline_total or 0.0) - (export_baseline_total or 0.0)

    if end <= window['start']:
        protected, post_kwh, avoidable = power_down_protected_soc(
            controller, state, end, window['start'], cap, reserve
        )
    elif start < window['start'] < end:
        protected = max(float(reserve), float(controller.c.get('safety_buffer_soc', 20)))
        post_kwh = 0.0
        avoidable = True
    else:
        protected = None
        post_kwh = None
        avoidable = True

    start_soc = None
    if start <= now < end:
        start_soc = await controller.live_soc()
    if start_soc is None:
        start_soc = controller.soc_at(state, max(start, now), 'forecast_no_slots')

    available_ac = 0.0
    session_limit_ac = max(
        0.0,
        (end - max(start, now)).total_seconds() / 3600.0 * (discharge_rate_w / 1000.0),
    )
    if protected is not None and start_soc is not None:
        stored = max(0.0, cap * (start_soc - protected) / 100.0)
        available_ac = min(stored * 0.95, session_limit_ac)

    reward = as_float(event.get('octopoints_per_kwh'))
    return {
        'entity_id': event_entity,
        'event_id': event.get('id'),
        'start': start,
        'end': end,
        'active': start <= now < end,
        'reward_octopoints_per_kwh': reward,
        'import_baseline_total_kwh': import_baseline_total,
        'export_baseline_total_kwh': export_baseline_total,
        'net_baseline_total_kwh': net_baseline_total,
        'import_baseline_incomplete': import_baseline_incomplete,
        'export_baseline_incomplete': export_baseline_incomplete,
        'protected_soc': protected,
        'post_session_required_kwh': post_kwh,
        'post_session_peak_import_avoidable': avoidable,
        'forecast_session_start_soc': start_soc,
        'max_safe_battery_output_kwh': available_ac,
        'before_next_offpeak': end <= window['start'],
        'after_next_offpeak': start >= window['end'],
    }
