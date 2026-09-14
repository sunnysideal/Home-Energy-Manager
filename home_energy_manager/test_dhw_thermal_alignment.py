from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dhw_shadow_runner import (
    PRODUCTION_COVERAGE_SLOTS,
    PRODUCTION_SLOTS,
    THERMAL_STEP_MINUTES,
    _production_starts,
    _published_half_hours,
    _required_simulation_hours,
    _simulation_start,
)


@dataclass(frozen=True)
class FakeSlot:
    start: datetime
    upper_temp_c: float = 50.0
    lower_temp_c: float = 45.0
    draw_kwh: float = 0.0
    dhw_kwh: float = 0.01


def _raw_slots(now: datetime) -> tuple[list[datetime], list[FakeSlot]]:
    production = _production_starts(now)
    simulation_start = _simulation_start(now, production)
    hours = _required_simulation_hours(simulation_start, production)
    count = int(round(hours * 60 / THERMAL_STEP_MINUTES))
    raw = [
        FakeSlot(datetime.fromtimestamp(simulation_start.timestamp() + THERMAL_STEP_MINUTES * 60 * i, tz=simulation_start.tzinfo))
        for i in range(count)
    ]
    return production, raw


def _assert_complete(now: datetime) -> None:
    production, raw = _raw_slots(now)
    published, complete = _published_half_hours(
        raw, production, now=now, actual_current_dhw_kwh=0.0,
        current_upper_c=50.0, current_lower_c=45.0,
    )
    assert complete is True
    assert PRODUCTION_SLOTS == 96
    assert PRODUCTION_COVERAGE_SLOTS == 97
    assert len(production) == PRODUCTION_COVERAGE_SLOTS
    assert len(published) == PRODUCTION_COVERAGE_SLOTS
    assert [row["start"] for row in published] == [item.isoformat() for item in production]
    assert published[0]["current_slot"] is True
    assert published[-1]["start"] == production[-1].isoformat()


def test_alignment_from_exact_hour() -> None:
    _assert_complete(datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc))


def test_alignment_from_five_past() -> None:
    _assert_complete(datetime(2026, 9, 9, 16, 5, tzinfo=timezone.utc))


def test_alignment_from_twenty_past() -> None:
    _assert_complete(datetime(2026, 9, 9, 16, 20, tzinfo=timezone.utc))


def test_alignment_from_twenty_five_past() -> None:
    _assert_complete(datetime(2026, 9, 9, 16, 25, tzinfo=timezone.utc))


def test_alignment_from_half_past() -> None:
    _assert_complete(datetime(2026, 9, 9, 16, 30, tzinfo=timezone.utc))


def test_alignment_crosses_midnight() -> None:
    _assert_complete(datetime(2026, 9, 9, 23, 20, tzinfo=timezone.utc))


def test_alignment_across_europe_london_dst_change() -> None:
    london = ZoneInfo("Europe/London")
    _assert_complete(datetime(2026, 10, 24, 23, 20, tzinfo=london))


def test_one_missing_future_five_minute_slot_marks_horizon_incomplete() -> None:
    now = datetime(2026, 9, 9, 16, 20, tzinfo=timezone.utc)
    production, raw = _raw_slots(now)
    missing = production[10] + timedelta(minutes=15)
    raw = [slot for slot in raw if slot.start != missing]
    published, complete = _published_half_hours(
        raw, production, now=now, current_upper_c=50.0, current_lower_c=45.0,
    )
    assert complete is False
    assert len(published) == 10
