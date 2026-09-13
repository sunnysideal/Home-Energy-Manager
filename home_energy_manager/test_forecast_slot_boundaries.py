from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common.forecast_slots import production_slot_start


def test_processes_straddling_half_hour_choose_same_epoch():
    before = datetime.fromisoformat("2026-09-13T20:29:59+01:00")
    after = datetime.fromisoformat("2026-09-13T20:30:01+01:00")

    assert production_slot_start(before).isoformat() == "2026-09-13T20:30:00+01:00"
    assert production_slot_start(after).isoformat() == "2026-09-13T20:30:00+01:00"


def test_after_grace_window_selects_next_epoch():
    now = datetime.fromisoformat("2026-09-13T20:31:01+01:00")
    assert production_slot_start(now).isoformat() == "2026-09-13T21:00:00+01:00"


def test_exact_boundary_uses_that_boundary():
    now = datetime.fromisoformat("2026-09-13T20:30:00+01:00")
    assert production_slot_start(now).isoformat() == "2026-09-13T20:30:00+01:00"


def test_dst_fold_boundaries_are_unambiguous():
    tz = ZoneInfo("Europe/London")
    first_0130 = datetime(2026, 10, 25, 1, 30, 1, tzinfo=tz, fold=0)
    second_0130 = datetime(2026, 10, 25, 1, 30, 1, tzinfo=tz, fold=1)

    first = production_slot_start(first_0130)
    second = production_slot_start(second_0130)

    assert first.fold == 0
    assert second.fold == 1
    assert first.timestamp() != second.timestamp()
    assert first.minute == second.minute == 30


def test_requires_timezone_aware_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        production_slot_start(datetime(2026, 9, 13, 20, 30, 0))
