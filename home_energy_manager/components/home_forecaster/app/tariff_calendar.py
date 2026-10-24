"""DST-safe projection of a *validated* recurring daily tariff, not EV dispatches.

Only used when the supplier's next-day rate entity is unavailable. Settlement
periods are generated in UTC then mapped to Europe/London clock times; thus the
autumn duplicate hour is represented twice and the spring missing hour is never
manufactured. Unrecognised or incomplete patterns fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from statistics import median

UTC = timezone.utc
HALF_HOUR = timedelta(minutes=30)
EPS = 0.001


def _day_slots(day, tz):
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    current = start
    while current < end:
        yield current, current + HALF_HOUR, current.astimezone(tz)
        current += HALF_HOUR


def _profile(rates, source_day, tz):
    profile = {}
    for start, end, local in _day_slots(source_day, tz):
        matches = [float(row["rate_p"]) for row in rates
                   if row["start"].astimezone(UTC) <= start
                   and row["end"].astimezone(UTC) >= end]
        if not matches or max(matches) - min(matches) > EPS:
            return None, "incomplete_or_conflicting_source_tariff"
        key = (local.hour, local.minute)
        if key in profile and abs(profile[key] - matches[0]) > EPS:
            return None, "different_prices_in_repeated_hour"
        profile[key] = matches[0]
    return profile, None


def project_next_local_day(rates, source_day, tz):
    """Return (projected_slots, reason), or ([], reason) when unsafe.

    A recurring overnight pattern must contain a sustained cheap period at the
    start and at the end of the source calendar day. Additional isolated cheap
    prices are one-off smart discounts and MUST NOT be carried into tomorrow.
    """
    source, reason = _profile(rates, source_day, tz)
    if source is None:
        return [], reason
    cheap = source.get((0, 0))
    if cheap is None:
        return [], "midnight_rate_missing"
    early = 0
    for hour in range(24):
        for minute in (0, 30):
            if abs(source.get((hour, minute), float("inf")) - cheap) > EPS:
                break
            early += 1
        else:
            continue
        break
    late = 0
    for hour in range(23, -1, -1):
        for minute in (30, 0):
            if abs(source.get((hour, minute), float("inf")) - cheap) > EPS:
                break
            late += 1
        else:
            continue
        break
    if early < 4 or late < 1 or early + late >= 48:
        return [], "recurring_overnight_pattern_not_established"

    regular = {(h // 2, (h % 2) * 30) for h in range(early)}
    regular.update({(23 - h // 2, 30 if h % 2 == 0 else 0) for h in range(late)})
    peak = [price for key, price in source.items()
            if key not in regular and price > cheap + EPS]
    if not peak:
        return [], "daytime_rate_not_established"
    nominal_day_price = float(median(peak))
    projected = []
    missing = 0
    target_day = source_day + timedelta(days=1)
    for start, end, local in _day_slots(target_day, tz):
        key = (local.hour, local.minute)
        value = source.get(key)
        if value is None:
            # Source was a spring DST date: the absent hour can be reconstructed
            # only when both adjacent clock-rate periods agree.
            previous = source.get((local.hour - 1, local.minute))
            following = source.get((local.hour + 1, local.minute))
            if previous is None or following is None or abs(previous - following) > EPS:
                return [], "unresolvable_spring_missing_hour"
            value = previous
            missing += 1
        if key not in regular and value <= cheap + EPS:
            value = nominal_day_price
        projected.append({"start": start, "end": end, "rate_p": value})

    if not projected:
        return [], "empty_target_day"
    return projected, ("projected_with_spring_hour_interpolation" if missing
                       else "projected_local_calendar")


def covers_window(rates, start, end, expected_rate):
    """Reject gaps, overlaps with other prices, or a synthetic short block."""
    cursor = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    if cursor >= stop or (stop - cursor) > timedelta(hours=10):
        return False
    while cursor < stop:
        next_time = min(stop, cursor + HALF_HOUR)
        matches = [r for r in rates if r["start"].astimezone(UTC) <= cursor
                   and r["end"].astimezone(UTC) >= next_time]
        if not matches or any(abs(float(r["rate_p"]) - expected_rate) > EPS
                              for r in matches):
            return False
        cursor = next_time
    return True
