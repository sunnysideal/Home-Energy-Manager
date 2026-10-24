"""Regression for the EDF 2026-10-24 rollover and partial next-day tariffs."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).parent
TZ = ZoneInfo("Europe/London")
UTC = timezone.utc


def local(day, hour, minute=0, fold=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=TZ, fold=fold)


def rates(start, end, price=6.66):
    cursor, finish = start.astimezone(UTC), end.astimezone(UTC)
    rows = []
    while cursor < finish:
        following = min(cursor + timedelta(minutes=30), finish)
        rows.append({"start": cursor, "end": following, "rate_p": price})
        cursor = following
    return rows


def find_overnight_blocks(rows, tz):
    low = min(x["rate_p"] for x in rows)
    selected = sorted((x for x in rows if x["rate_p"] == low), key=lambda x: x["start"])
    blocks = []
    for row in selected:
        start, end = row["start"], row["end"]
        if blocks and blocks[-1][1] == start:
            blocks[-1] = (blocks[-1][0], end, low)
        else:
            blocks.append((start, end, low))
    return [b for b in blocks if b[0].astimezone(tz).date() != b[1].astimezone(tz).date()
            or b[1].astimezone(tz).hour == 0]


def selector():
    path = ROOT / "components/home_forecaster/app/offpeak_rollover_runtime.py"
    source = ast.parse(path.read_text())
    wanted = {"_valid_regular_window", "select_controller_offpeak"}
    funcs = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(funcs) == 2
    env = {
        "timedelta": timedelta, "timezone": timezone,
        "base": SimpleNamespace(find_overnight_blocks=find_overnight_blocks, HAError=RuntimeError),
        "_original_select_controller_offpeak": lambda *_: None,
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"), env)
    return env["select_controller_offpeak"]


@pytest.mark.parametrize("day,next_day,elapsed", [
    ("2026-10-24", "2026-10-25", 8),
    ("2027-03-27", "2027-03-28", 6),
    ("2026-11-14", "2026-11-15", 7),
])
def test_active_night_kept_after_rollover(day, next_day, elapsed):
    night = rates(local(day, 23), local(next_day, 6))
    assert (night[-1]["end"] - night[0]["start"]) == timedelta(hours=elapsed)
    fragment = rates(local(next_day, 23), local(next_day, 23, 30))
    fragment += rates(local(next_day, 23, 30), local(
        (datetime.fromisoformat(next_day) + timedelta(days=1)).date().isoformat(), 0
    ))
    for at in (local(day, 22, 55), local(day, 23), local(day, 23, 5),
               local(next_day, 0), local(next_day, 1, fold=0),
               local(next_day, 1, fold=1), local(next_day, 5, 55)):
        # For spring, 01:00 does not exist, so omit that synthetic timestamp.
        if day == "2027-03-27" and at.hour == 1:
            continue
        selected = selector()(night + fragment, at, TZ)
        assert selected[0].astimezone(UTC) == night[0]["start"]
        assert selected[1].astimezone(UTC) == night[-1]["end"]


def test_one_hour_next_night_fragment_not_accepted():
    block = rates(local("2026-10-25", 23), local("2026-10-26", 0))
    with pytest.raises(RuntimeError, match="could not be identified"):
        selector()(block, local("2026-10-25", 6, 1), TZ)


def test_complete_following_night_is_accepted():
    block = rates(local("2026-10-25", 23), local("2026-10-26", 6))
    selected = selector()(block, local("2026-10-25", 6, 1), TZ)
    assert selected[0].astimezone(UTC) == block[0]["start"]
    assert selected[1].astimezone(UTC) == block[-1]["end"]


def test_controller_rejects_partial_tariff():
    path = ROOT / "components/controller/controller_tariff.py"
    tree = ast.parse(path.read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "offpeak_from_forecast")
    env = {"parse_dt": datetime.fromisoformat, "as_float": float,
           "timedelta": timedelta, "timezone": timezone}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), env)
    controller = SimpleNamespace(tz=TZ)

    def state(start, end):
        return {"attributes": {"offpeak": {"start": start.isoformat(),
                 "end": end.isoformat(), "rate_p": 6.66, "source": "supplier"}}}

    assert env["offpeak_from_forecast"](
        controller, state(local("2026-10-25", 23), local("2026-10-26", 0))
    ) is None
    assert env["offpeak_from_forecast"](
        controller, state(local("2026-10-24", 23), local("2026-10-25", 6))
    ) is not None


def test_half_hour_after_first_autumn_fold_is_second_fold():
    # The production forecaster must calculate its next boundary in UTC.
    now = local("2026-10-25", 1, 40, fold=0)
    floor_utc = now.astimezone(UTC).replace(minute=30, second=0, microsecond=0)
    boundary = (floor_utc + timedelta(minutes=30)).astimezone(TZ)
    assert boundary.hour == 1 and boundary.fold == 1
    assert boundary.astimezone(UTC) > now.astimezone(UTC)
