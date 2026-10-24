"""Regression coverage for EDF daytime discounts and London DST boundaries."""
import ast
from datetime import datetime, timedelta, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

ROOT = Path(__file__).parent
TZ = ZoneInfo("Europe/London")


def selector():
    path = ROOT / "components/home_forecaster/app/main.py"
    source = ast.parse(path.read_text(encoding="utf-8"))
    selected = next(node for node in source.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "select_controller_offpeak")
    module = ast.Module(body=[selected], type_ignores=[])
    env = {"datetime": datetime, "timedelta": timedelta,
           "timezone": timezone, "dtime": dtime, "Any": object, "ZoneInfo": ZoneInfo}
    exec(compile(module, str(path), "exec"), env)
    return env["select_controller_offpeak"]


def local(date, hour, minute=0, fold=0):
    return datetime.fromisoformat(date).replace(
        hour=hour, minute=minute, tzinfo=TZ, fold=fold)


def rates(start, end, price, step_minutes=30):
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    result = []
    while start < end:
        following = min(start + timedelta(minutes=step_minutes), end)
        result.append({"start": start, "end": following, "rate_p": price})
        start = following
    return result


def test_daytime_edf_smart_discount_does_not_become_regular_window():
    now = local("2026-10-24", 9, 35)
    smart = rates(local("2026-10-25", 8, 30), local("2026-10-25", 9, 30), 6.66)
    overnight = rates(local("2026-10-24", 23), local("2026-10-25", 6), 6.66)
    normal = rates(local("2026-10-25", 6), local("2026-10-25", 12), 29.634)
    chosen = selector()(smart + overnight + normal, now, TZ)
    assert chosen is not None
    assert chosen[0].astimezone(TZ) == local("2026-10-24", 23)
    assert chosen[1].astimezone(TZ) == local("2026-10-25", 6)


def test_daytime_only_smart_discount_is_not_an_offpeak_window():
    now = local("2026-10-24", 9, 35)
    smart = rates(local("2026-10-25", 8, 30), local("2026-10-25", 9, 30), 6.66)
    assert selector()(smart, now, TZ) is None


@pytest.mark.parametrize("date,following_date", [
    ("2026-10-24", "2026-10-25"),
    ("2027-03-27", "2027-03-28"),
])
def test_dst_overnight_window_retains_real_instants(date, following_date):
    start = local(date, 23)
    end = local(following_date, 6)
    block = rates(start, end, 6.99)
    chosen = selector()(block, local(date, 21), TZ)
    assert chosen is not None
    assert chosen[0].astimezone(timezone.utc) == start.astimezone(timezone.utc)
    assert chosen[1].astimezone(timezone.utc) == end.astimezone(timezone.utc)
    assert chosen[0].astimezone(TZ).hour == 23
    assert chosen[1].astimezone(TZ).hour == 6
    expected_hours = 8 if date == "2026-10-24" else 6
    assert (chosen[1].astimezone(timezone.utc) -
            chosen[0].astimezone(timezone.utc)) == timedelta(hours=expected_hours)


def test_early_morning_regular_offpeak_is_allowed():
    block = rates(local("2026-10-24", 0), local("2026-10-24", 5), 6.99)
    chosen = selector()(block, local("2026-10-23", 20), TZ)
    assert chosen is not None
    assert chosen[0].astimezone(TZ).hour == 0


def test_same_price_smart_discount_after_regular_window_is_excluded():
    block = rates(local("2026-10-24", 23), local("2026-10-25", 6), 6.66)
    smart = rates(local("2026-10-25", 7), local("2026-10-25", 7, 30), 6.66)
    chosen = selector()(block + smart, local("2026-10-24", 20), TZ)
    assert chosen is not None
    assert chosen[1].astimezone(TZ).hour == 6
