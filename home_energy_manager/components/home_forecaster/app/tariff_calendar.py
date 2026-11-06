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


def project_next_local_day(rates, source_day, tz, regular_profile=None):
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
    # Walk actual settlement intervals, not an assumed 48 wall-clock slots.
    # Spring omits 01:00; autumn repeats 01:00. Both are regular overnight.
    slot_keys = [(local.hour, local.minute) for _, _, local
                 in _day_slots(source_day, tz)]
    early = 0
    for key in slot_keys:
        if abs(source[key] - cheap) > EPS:
            break
        early += 1
    late = 0
    for key in reversed(slot_keys):
        if abs(source[key] - cheap) > EPS:
            break
        late += 1
    if early < 4 or late < 1 or early + late >= 48:
        return [], "recurring_overnight_pattern_not_established"

    morning_end = slot_keys[early]
    evening_start = slot_keys[-late]
    if regular_profile:
        try:
            end_minutes = int(regular_profile["end_minute"])
            start_minutes = int(regular_profile["start_minute"])
            morning_end = (end_minutes // 60, end_minutes % 60)
            evening_start = (start_minutes // 60, start_minutes % 60)
            if not (morning_end < evening_start):
                return [], "invalid_recurring_tariff_profile"
            # Never copy one-off cheap discounts at the edges of the night.
            if any(abs(source[key] - cheap) > EPS for key in slot_keys
                   if key < morning_end or key >= evening_start):
                return [], "stored_recurring_profile_conflicts_with_tariff"
        except (KeyError, TypeError, ValueError):
            return [], "invalid_recurring_tariff_profile"
    def is_regular(key):
        return key < morning_end or key >= evening_start

    peak = [price for key, price in source.items()
            if not is_regular(key) and price > cheap + EPS]
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
        if not is_regular(key) and value <= cheap + EPS:
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


REGULAR_PROFILE_KEY = "validated_regular_offpeak_profile"


def saved_regular_profile(store):
    import json
    try:
        raw = store.meta_get(REGULAR_PROFILE_KEY)
        return json.loads(raw) if raw else None
    except (ValueError, TypeError, AttributeError):
        return None


def snapshot_regular_profile(store, day, tz):
    """Bootstrap a trusted clock from yesterday's frozen forecast for today.

    This predates today's EV smart-discount changes. A current-day forecast
    cannot establish the baseline because its cheap periods may already have
    been extended by an Intelligent dispatch.
    """
    try:
        frozen = store.forecast_snapshot(day)
        if not frozen:
            return None
        captured_at, slots = frozen
        if datetime.fromisoformat(captured_at).astimezone(tz).date() >= day:
            return None
        rates = []
        for slot in slots:
            start = datetime.fromisoformat(slot["start"]).astimezone(UTC)
            rate = float(slot["import_rate_p"])
            rates.append({"start": start, "end": start + HALF_HOUR, "rate_p": rate})
        by_clock, reason = _profile(rates, day, tz)
        if by_clock is None:
            return None
        keys = [(at.hour, at.minute) for _, _, at in _day_slots(day, tz)]
        cheap = min(by_clock.values())
        first_peak = next((i for i, key in enumerate(keys)
                           if by_clock[key] > cheap + EPS), None)
        last_peak = next((i for i in range(len(keys) - 1, -1, -1)
                          if by_clock[keys[i]] > cheap + EPS), None)
        if first_peak is None or last_peak is None:
            return None
        if not (4 <= first_peak <= 16 and last_peak < len(keys) - 1):
            return None
        if any(abs(by_clock[key] - cheap) > EPS for key in keys[:first_peak]):
            return None
        if any(abs(by_clock[key] - cheap) > EPS for key in keys[last_peak + 1:]):
            return None
        end_hour, end_minute = keys[first_peak]
        start_hour, start_minute = keys[last_peak + 1]
        if start_hour < 17 or end_hour > 8:
            return None
        return {"start_minute": start_hour * 60 + start_minute,
                "end_minute": end_hour * 60 + end_minute,
                "rate_p": cheap}
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def _profile_window(profile, day, tz):
    """Construct one local recurring overnight window across either DST change."""
    start_minute = int(profile["start_minute"])
    end_minute = int(profile["end_minute"])
    start = datetime.combine(day, time(start_minute // 60, start_minute % 60), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1),
                           time(end_minute // 60, end_minute % 60), tzinfo=tz)
    return start, end, float(profile["rate_p"])


def stable_regular_offpeak(store, rates, now, tz, candidate):
    """Keep a validated recurring supplier boundary separate from EV discounts.

    The first fully covered regular block establishes the daily clock profile.
    Later same-price extensions cannot move that boundary. Unknown or changed
    rates make the scheduling window unavailable without blocking forecasting.
    """
    import json

    profile = saved_regular_profile(store)
    if profile:
        try:
            if not (0 <= int(profile["start_minute"]) < 1440
                    and 0 <= int(profile["end_minute"]) < 1440
                    and float(profile["rate_p"]) >= 0):
                profile = None
        except (ValueError, TypeError, KeyError):
            profile = None

    if profile is None:
        # Prefer pre-dispatch evidence. On an upgraded running system the
        # today's supplier projection may already contain a 06:30/07:30
        # discount; learning it as a new regular schedule would be unsafe.
        profile = snapshot_regular_profile(store, now.astimezone(tz).date(), tz)
        if profile is not None:
            store.meta_set(REGULAR_PROFILE_KEY, json.dumps(profile))

    if profile is None and candidate is not None and not hasattr(store, "forecast_snapshot"):
        start, end, rate = candidate
        local_start, local_end = start.astimezone(tz), end.astimezone(tz)
        elapsed = end.astimezone(UTC) - start.astimezone(UTC)
        if (local_start.date() < local_end.date()
                and local_start.hour >= 17 and local_end.hour <= 8
                and timedelta(hours=2) <= elapsed <= timedelta(hours=10)
                and covers_window(rates, start, end, float(rate))):
            profile = {"start_minute": local_start.hour * 60 + local_start.minute,
                       "end_minute": local_end.hour * 60 + local_end.minute,
                       "rate_p": float(rate)}
            store.meta_set(REGULAR_PROFILE_KEY, json.dumps(profile))

    if profile is None:
        return None, "regular_profile_unavailable"

    local_day = now.astimezone(tz).date()
    possible = []
    for offset in (-1, 0, 1):
        start, end, rate = _profile_window(profile, local_day + timedelta(days=offset), tz)
        if end.astimezone(UTC) <= now.astimezone(UTC):
            continue
        if covers_window(rates, start, end, rate):
            possible.append((start, end, rate))
    if not possible:
        return None, "regular_profile_tariff_coverage_unavailable"
    active = [w for w in possible if w[0].astimezone(UTC) <= now.astimezone(UTC)]
    return (active[0] if active else min(possible, key=lambda w: w[0].astimezone(UTC))), "validated_regular_profile"
