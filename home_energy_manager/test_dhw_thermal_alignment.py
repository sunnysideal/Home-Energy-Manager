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
    PRODUCTION_SLOTS,
    THERMAL_STEP_MINUTES,
    _production_starts,
    _published_half_hours,
    _required_simulation_hours,
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
    simulation_start = now.replace(second=0, microsecond=0)
    remainder = simulation_start.minute % THERMAL_STEP_MINUTES
    if remainder or now.second or now.microsecond:
        simulation_start += timedelta(minutes=(THERMAL_STEP_MINUTES - remainder) % THERMAL_STEP_MINUTES)
        if remainder == 0 and (now.second or now.microsecond):
            simulation_start += timedelta(minutes=THERMAL_STEP_MINUTES)
    hours = _required_simulation_hours(simulation_start, production)
    count = int(round(hours * 60 / THERMAL_STEP_MINUTES))
    raw = [FakeSlot(simulation_start + timedelta(minutes=THERMAL_STEP_MINUTES * i)) for i in range(count)]
    return production, raw


def _assert_complete(now: datetime) -> None:
    production, raw = _raw_slots(now)
    published, complete = _published_half_hours(raw, production)
    assert complete is True
    assert len(production) == PRODUCTION_SLOTS == 96
    assert len(published) == 96
    assert [datetime.fromisoformat(row["start"]) for row in published] == production
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
    # The public ASHP forecast also advances on the local production clock. The thermal
    # grid must use the same ZoneInfo-aware arithmetic so its timestamps remain identical.
    london = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 24, 23, 20, tzinfo=london)
    _assert_complete(now)


def test_one_missing_five_minute_slot_marks_horizon_incomplete() -> None:
    now = datetime(2026, 9, 9, 16, 20, tzinfo=timezone.utc)
    production, raw = _raw_slots(now)
    missing = production[10] + timedelta(minutes=15)
    raw = [slot for slot in raw if slot.start != missing]
    published, complete = _published_half_hours(raw, production)
    assert complete is False
    assert len(published) == 10
