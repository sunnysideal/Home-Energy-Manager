"""EDF 6 November: temporary adjacent cheap slots are not regular off-peak."""
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/London")
UTC = timezone.utc
PATH = Path(__file__).parent / "components/home_forecaster/app/tariff_calendar.py"
spec = importlib.util.spec_from_file_location("test_regular_tariff_calendar", PATH)
calendar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calendar)


class Store:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key):
        return self.meta.get(key)

    def meta_set(self, key, value):
        self.meta[key] = value


def local(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(
        hour=hour, minute=minute)


def source_rates(day, cheap_end_hour=6, cheap_end_minute=0, smart_end_hour=0):
    rows = []
    cutoff = cheap_end_hour * 60 + cheap_end_minute
    for start, end, at in calendar._day_slots(day, TZ):
        minutes = at.hour * 60 + at.minute
        cheap = minutes < cutoff or minutes >= 23 * 60
        if smart_end_hour and 6 * 60 <= minutes < smart_end_hour * 60:
            cheap = True
        rows.append({"start": start, "end": end, "rate_p": 6.66 if cheap else 29.634})
    return rows


def test_original_regular_boundary_remains_fixed_during_morning_ev_discount():
    day = date(2026, 11, 6)
    store = Store()
    normal = source_rates(day - timedelta(days=1)) + source_rates(day)
    # The previously validated regular window remains authoritative.
    picked, reason = calendar.stable_regular_offpeak(
        store, normal, local(day, 2), TZ,
        (local(day - timedelta(days=1), 23), local(day, 6), 6.66))
    # First candidate should be based on a fully covered window.
    assert picked is not None, reason
    assert json.loads(store.meta_get(calendar.REGULAR_PROFILE_KEY))["end_minute"] == 360

    # Model both the 06:30 and 07:30 false extensions seen in the logs.
    for extra in (30, 90):
        extended = source_rates(day, 6, extra)
        future = source_rates(day + timedelta(days=1), 6, extra)
        rows = source_rates(day - timedelta(days=1)) + extended + future
        selected, reason = calendar.stable_regular_offpeak(
            store, rows, local(day, 5, 35), TZ,
            (local(day, 23), local(day + timedelta(days=1), 7, 30), 6.66))
        assert selected is not None, reason
        assert selected[0] == local(day - timedelta(days=1), 23)
        assert selected[1] == local(day, 6)


def test_future_projection_drops_0630_and_0730_discounts():
    day = date(2026, 11, 6)
    profile = {"start_minute": 23 * 60, "end_minute": 6 * 60, "rate_p": 6.66}
    for extension in (30, 90):
        rows = source_rates(day, 6, extension)
        projected, reason = calendar.project_next_local_day(rows, day, TZ, regular_profile=profile)
        assert projected, reason
        assert all(row["rate_p"] == 29.634 for row in projected
                   if 6 * 60 <= row["start"].astimezone(TZ).hour * 60 +
                   row["start"].astimezone(TZ).minute < 8 * 60)
        assert all(row["rate_p"] == 6.66 for row in projected
                   if row["start"].astimezone(TZ).hour < 6)


@pytest.mark.parametrize("start_day", [date(2026, 10, 24), date(2027, 3, 27)])
def test_saved_recurring_window_retains_dst_real_duration(start_day):
    profile = {"start_minute": 1380, "end_minute": 360, "rate_p": 6.66}
    a, b, _ = calendar._profile_window(profile, start_day, TZ)
    expected = 8 if start_day.month == 10 else 6
    assert b.astimezone(UTC) - a.astimezone(UTC) == timedelta(hours=expected)


def test_no_verified_profile_refuses_new_charge_but_does_not_throw():
    store = Store()
    selected, reason = calendar.stable_regular_offpeak(
        store, [], local(date(2026, 11, 6), 7, 10), TZ, None)
    assert selected is None
    assert reason == "regular_profile_unavailable"


def test_first_run_during_0730_ev_extension_uses_frozen_midnight_profile():
    day = date(2026, 11, 6)
    store = Store()
    predicted = source_rates(day)
    frozen = (local(day - timedelta(days=1), 23, 55).isoformat(),
              [{"start": item["start"].isoformat(), "import_rate_p": item["rate_p"]}
               for item in predicted])
    store.forecast_snapshot = lambda query_day: frozen if query_day == day else None
    actual = source_rates(day, 7, 30)
    tomorrow = source_rates(day + timedelta(days=1), 7, 30)
    rows = actual + tomorrow
    window, reason = calendar.stable_regular_offpeak(
        store, rows, local(day, 7, 55), TZ,
        (local(day, 23), local(day + timedelta(days=1), 7, 30), 6.66))
    assert window is not None, reason
    assert window[0] == local(day, 23)
    assert window[1] == local(day + timedelta(days=1), 6)
    assert json.loads(store.meta_get(calendar.REGULAR_PROFILE_KEY))["end_minute"] == 360


def test_no_frozen_profile_does_not_learn_0730_as_recurring():
    day = date(2026, 11, 6)
    store = Store()
    store.forecast_snapshot = lambda _day: None
    rows = source_rates(day, 7, 30) + source_rates(day + timedelta(days=1), 7, 30)
    window, reason = calendar.stable_regular_offpeak(
        store, rows, local(day, 7, 55), TZ,
        (local(day, 23), local(day + timedelta(days=1), 7, 30), 6.66))
    assert window is None
    assert store.meta_get(calendar.REGULAR_PROFILE_KEY) is None
