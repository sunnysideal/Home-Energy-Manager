"""Test-only DST regression matrix: supplier rates, off-peak selection, controller state.

Do not change production paths to make these tests pass. The two known missing-tomorrow
fallback regressions are xfailed pending a separate production fix.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, time, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent
HOME = ROOT / "components" / "home_forecaster" / "app"
CONTROLLER = ROOT / "components" / "controller"
for path in (str(ROOT), str(CONTROLLER)):
    if path not in sys.path:
        sys.path.insert(0, path)
TZ = ZoneInfo("Europe/London")
UTC = timezone.utc


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


home = load_module(HOME / "main.py", "dst_matrix_home")
tariff = load_module(CONTROLLER / "controller_tariff.py", "dst_matrix_controller_tariff")
minimise = load_module(CONTROLLER / "controller_minimise_offpeak.py", "dst_matrix_minimise")


def transition(year, month):
    # UK transitions are on the final Sunday of March and October.
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


TRANSITIONS = [(year, month, transition(year, month)) for year in (2025, 2026, 2027) for month in (3, 10)]
TIMES = ("00:00", "00:05", "00:30", "01:30", "02:30", "05:30", "06:00", "12:00", "18:00", "23:55")


def local(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), TZ)


def utc_equal(actual, expected):
    assert actual.astimezone(UTC) == expected.astimezone(UTC), (actual, expected)


def half_hours(start, end):
    t = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    while t < stop:
        next_t = t + timedelta(minutes=30)
        yield t.astimezone(TZ), next_t.astimezone(TZ)
        t = next_t


def supplier_rates(first_day, number_of_days=5):
    """EDF-like fixed wall-clock 23:00-06:00 supplier data as offset-aware ISO rows."""
    a = datetime.combine(first_day, time.min, TZ)
    b = datetime.combine(first_day + timedelta(days=number_of_days), time.min, TZ)
    raw = []
    for st, en in half_hours(a, b):
        cheap = st.hour >= 23 or st.hour < 6
        raw.append({"start": st.isoformat(), "end": en.isoformat(), "value": 0.0699 if cheap else 0.3112})
    return home.parse_rates({"attributes": {"rates": raw}})


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
def test_transition_calendar_reference(year, month, day):
    assert day.weekday() == 6
    assert (day + timedelta(days=7)).month != month
    before = local(day - timedelta(days=1), "12:00")
    after = local(day + timedelta(days=1), "12:00")
    assert before.utcoffset().total_seconds() - after.utcoffset().total_seconds() == (
        3600 if month == 10 else -3600
    )


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-3, 4))
def test_full_local_day_contains_all_real_settlement_slots(year, month, day, offset):
    target = day + timedelta(days=offset)
    slots = home.real_half_hours(local(target, "00:00"), local(target + timedelta(days=1), "00:00"))
    assert len(slots) == (51 if offset == 0 and month == 10 else 47 if offset == 0 else 49)
    assert slots[0].date() == target
    assert slots[-1].date() == target + timedelta(days=1)
    assert len({x.astimezone(UTC) for x in slots}) == len(slots)
    for a, b in zip(slots, slots[1:]):
        assert (b.astimezone(UTC) - a.astimezone(UTC)) == timedelta(minutes=30)


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
def test_fold_or_gap_is_visible_in_real_half_hour_stream(year, month, day):
    slots = home.real_half_hours(local(day, "00:00"), local(day + timedelta(days=1), "00:00"))
    clock_hours = [x.strftime("%H:%M") for x in slots[:-1]]
    if month == 10:
        assert clock_hours.count("01:00") == 2
        assert clock_hours.count("01:30") == 2
        folds = [x for x in slots if x.hour == 1]
        assert sorted(x.fold for x in folds) == [0, 0, 1, 1]
    else:
        assert "01:00" not in clock_hours
        assert "01:30" not in clock_hours


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-4, 5))
def test_supplier_overnight_blocks_have_local_boundaries(year, month, day, offset):
    night = day + timedelta(days=offset)
    rates = supplier_rates(night - timedelta(days=1), 4)
    blocks = home.find_overnight_blocks(rates, TZ)
    expected_start = local(night, "23:00")
    expected_end = local(night + timedelta(days=1), "06:00")
    block = next((b for b in blocks if b[0].astimezone(TZ).date() == night), None)
    assert block is not None, (night, blocks)
    utc_equal(block[0], expected_start)
    utc_equal(block[1], expected_end)
    assert block[2] == pytest.approx(6.99)


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-4, 5))
@pytest.mark.parametrize("clock", ("12:00", "18:00", "22:55"))
def test_forecaster_next_known_window_includes_both_clock_changes(year, month, day, offset, clock):
    today = day + timedelta(days=offset)
    now = local(today, clock)
    rates = supplier_rates(today - timedelta(days=1), 5)
    selected = home.select_controller_offpeak(rates, now, TZ)
    assert selected is not None
    utc_equal(selected[0], local(today, "23:00"))
    utc_equal(selected[1], local(today + timedelta(days=1), "06:00"))


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-4, 5))
@pytest.mark.parametrize("clock", ("23:55", "00:00", "00:05", "00:30", "05:30"))
def test_active_overnight_end_not_replaced_by_following_night(year, month, day, offset, clock):
    # This tests the entire vulnerable predecessor/successor range, including Friday->Saturday.
    current_day = day + timedelta(days=offset)
    night = current_day if clock == "23:55" else current_day - timedelta(days=1)
    now = local(current_day, clock)
    active = {"start": local(night, "23:00"), "end": local(night + timedelta(days=1), "06:00"), "rate_p": 6.99}
    following = {"start": local(night + timedelta(days=1), "23:00"),
                 "end": local(night + timedelta(days=2), "06:00"), "rate_p": 6.99}
    controller = SimpleNamespace(now=lambda: now, tz=TZ, current_active_offpeak=lambda: active)
    chosen, is_active = minimise.active_or_next_offpeak(controller, following)
    assert is_active
    utc_equal(chosen["start"], active["start"])
    utc_equal(chosen["end"], active["end"])


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-4, 5))
@pytest.mark.parametrize("clock", ("00:05", "00:30", "05:30"))
def test_backward_reconstruction_uses_previous_wall_clock_day(year, month, day, offset, clock):
    current_day = day + timedelta(days=offset)
    night = current_day - timedelta(days=1)
    future = {"start": local(night + timedelta(days=1), "23:00"),
              "end": local(night + timedelta(days=2), "06:00"), "rate_p": 6.99}
    controller = SimpleNamespace(now=lambda: local(current_day, clock),
                                 tz=TZ, current_active_offpeak=lambda: None)
    chosen, active = minimise.active_or_next_offpeak(controller, future)
    assert active
    utc_equal(chosen["start"], local(night, "23:00"))
    utc_equal(chosen["end"], local(night + timedelta(days=1), "06:00"))


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", range(-3, 4))
def test_controller_persisted_fallback_keeps_wall_clock_hours(year, month, day, offset):
    today = day + timedelta(days=offset)
    saved = {"start": local(today - timedelta(days=2), "23:00").isoformat(),
             "end": local(today - timedelta(days=1), "06:00").isoformat(), "rate_p": 6.99}
    database = SimpleNamespace(ok=True, get=lambda key: saved if key == "next_offpeak" else None)
    controller = SimpleNamespace(db=database, tz=TZ, now=lambda: local(today, "12:00"))
    got = tariff.fallback_offpeak(controller)
    assert got is not None
    utc_equal(got["start"], local(today, "23:00"))
    utc_equal(got["end"], local(today + timedelta(days=1), "06:00"))


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("clock", ("00:05", "05:30", "23:55"))
def test_active_persisted_offpeak_wins_over_future_forecast(year, month, day, clock):
    today = day
    night = today if clock == "23:55" else today - timedelta(days=1)
    active = {"start": local(night, "23:00").isoformat(),
              "end": local(night + timedelta(days=1), "06:00").isoformat(), "rate_p": 6.99}
    database = SimpleNamespace(ok=True, get=lambda key: {"offpeak": active} if key == "status_plan" else None)
    controller = SimpleNamespace(db=database, tz=TZ, now=lambda: local(today, clock))
    got = tariff.current_active_offpeak(controller)
    assert got is not None
    utc_equal(got["end"], local(night + timedelta(days=1), "06:00"))


def _actual_import_copy(today_rates):
    """Execute the unmodified AST statement from make_forecast, not a reimplementation."""
    source = (HOME / "main.py").read_text()
    module = ast.parse(source)
    func = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "make_forecast")
    branch = next(n for n in func.body if isinstance(n, ast.If) and n.lineno == 1550)
    statement = ast.fix_missing_locations(ast.Module(body=[branch], type_ignores=[]))
    scope = dict(import_rates=list(today_rates), import_current_day={"attributes": {"rates": [
        {"start": r["start"].isoformat(), "end": r["end"].isoformat(), "value": r["rate_p"] / 100}
        for r in today_rates
    ]}}, import_next_day=None, tariff_fallback=False, reasons=[],
        parse_rates=home.parse_rates, timedelta=timedelta)
    exec(compile(statement, str(HOME / "main.py"), "exec"), scope, scope)
    assert scope["tariff_fallback"] and "today pattern carried forward" in scope["reasons"][0]
    return scope["import_rates"][len(today_rates):]


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("offset", (-1, 0, 1))
def test_missing_tomorrow_copy_preserves_local_clock_and_real_instants(year, month, day, offset):
    yesterday = day + timedelta(days=offset)
    # A full local day, including 46/50 settlement half-hours during DST.
    today_rates = supplier_rates(yesterday, 1)
    copied = _actual_import_copy(today_rates)
    expected_rates = supplier_rates(yesterday + timedelta(days=1), 1)
    assert len(copied) == len(expected_rates)
    for actual, expected in zip(copied, expected_rates):
        utc_equal(actual["start"], expected["start"])
        utc_equal(actual["end"], expected["end"])


@pytest.mark.parametrize("year,month,day", TRANSITIONS)
@pytest.mark.parametrize("night_offset", (-2, -1, 0, 1))
def test_tariff_price_and_regular_window_remain_consistent_for_entire_overnight(year, month, day, night_offset):
    night = day + timedelta(days=night_offset)
    rates = supplier_rates(night - timedelta(days=1), 4)
    expected_start = local(night, "23:00")
    expected_end = local(night + timedelta(days=1), "06:00")
    for begin, end in half_hours(expected_start, expected_end):
        assert home.rate_at(rates, begin, end) == pytest.approx(6.99)
    before = expected_start.astimezone(UTC) - timedelta(minutes=30)
    after = expected_end
    assert home.rate_at(rates, before, expected_start) == pytest.approx(31.12)
    assert home.rate_at(rates, after, after.astimezone(UTC) + timedelta(minutes=30)) == pytest.approx(31.12)
