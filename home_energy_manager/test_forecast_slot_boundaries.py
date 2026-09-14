from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common.forecast_slots import production_slot_start


def test_production_start_is_previous_half_hour_throughout_slot():
    cases = [
        ("2026-09-13T20:29:59+01:00", "2026-09-13T20:00:00+01:00"),
        ("2026-09-13T20:30:00+01:00", "2026-09-13T20:30:00+01:00"),
        ("2026-09-13T20:30:01+01:00", "2026-09-13T20:30:00+01:00"),
        ("2026-09-13T20:31:01+01:00", "2026-09-13T20:30:00+01:00"),
        ("2026-09-13T20:59:59+01:00", "2026-09-13T20:30:00+01:00"),
        ("2026-09-13T21:00:00+01:00", "2026-09-13T21:00:00+01:00"),
    ]
    for raw_now, expected in cases:
        assert production_slot_start(datetime.fromisoformat(raw_now)).isoformat() == expected


def test_independent_processes_inside_same_half_hour_choose_same_epoch():
    early = datetime.fromisoformat("2026-09-13T20:31:01+01:00")
    late = datetime.fromisoformat("2026-09-13T20:58:59+01:00")
    assert production_slot_start(early) == production_slot_start(late)


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


def test_requires_positive_interval():
    with pytest.raises(ValueError, match="positive"):
        production_slot_start(datetime.fromisoformat("2026-09-13T20:30:00+01:00"), 0)
