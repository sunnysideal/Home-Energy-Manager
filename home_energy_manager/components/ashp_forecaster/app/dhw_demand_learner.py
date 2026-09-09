"""Learn expected DHW draw demand by weekday/weekend and local 30-minute slot."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import sqlite3
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DemandSlot:
    day_type: str
    slot_index: int
    expected_kwh: float
    probability: float
    typical_kwh: float
    sample_days: int


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted((value, weight) for value, weight in values if weight > 0)
    if not ordered:
        return 0.0
    half = sum(weight for _, weight in ordered) / 2.0
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= half:
            return value
    return ordered[-1][0]


def _day_type(day: date) -> str:
    return "weekend" if day.weekday() >= 5 else "weekday"


def learn_demand_profile(
    db: sqlite3.Connection,
    *,
    timezone_name: str,
    history_days: int = 28,
    minimum_observed_days: int = 3,
    sample_minutes: int = 5,
) -> list[DemandSlot]:
    tz = ZoneInfo(timezone_name)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    cutoff = (now_utc - timedelta(days=history_days)).isoformat()
    sample_minutes = max(1, int(sample_minutes))
    minimum_samples_per_day = max(1, math.ceil((8 * 60) / sample_minutes))

    # Count valid samples by LOCAL date. A day only counts toward zero-use probability
    # when the collector observed at least eight hours on that local calendar day.
    sample_counts: Counter[date] = Counter()
    for (ts_raw,) in db.execute(
        "SELECT timestamp FROM dhw_thermal_samples WHERE timestamp>=? AND valid=1",
        (cutoff,),
    ):
        try:
            local_day = datetime.fromisoformat(str(ts_raw)).astimezone(tz).date()
        except ValueError:
            continue
        sample_counts[local_day] += 1
    observed_days = {
        day for day, count in sample_counts.items()
        if count >= minimum_samples_per_day
    }
    if not observed_days:
        return []

    daily_slot_energy: dict[tuple[date, int], float] = defaultdict(float)
    draw_rows = db.execute(
        "SELECT timestamp,estimated_thermal_kwh,confidence FROM dhw_draw_events "
        "WHERE timestamp>=? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()
    for ts_raw, energy_raw, confidence_raw in draw_rows:
        try:
            ts = datetime.fromisoformat(str(ts_raw)).astimezone(tz)
            energy = max(0.0, float(energy_raw))
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            continue
        if ts.date() not in observed_days or confidence < 0.35 or energy <= 0.0:
            continue
        slot = ts.hour * 2 + (1 if ts.minute >= 30 else 0)
        daily_slot_energy[(ts.date(), slot)] += energy

    by_type = {
        "weekday": sorted(day for day in observed_days if _day_type(day) == "weekday"),
        "weekend": sorted(day for day in observed_days if _day_type(day) == "weekend"),
    }
    out: list[DemandSlot] = []
    for day_type, days in by_type.items():
        if len(days) < minimum_observed_days:
            continue
        for slot in range(48):
            positive: list[tuple[float, float]] = []
            nonzero_days = 0
            for day in days:
                value = daily_slot_energy.get((day, slot), 0.0)
                if value > 0.0:
                    nonzero_days += 1
                    age_days = max(0, (now_local.date() - day).days)
                    weight = 0.5 ** (age_days / 14.0)
                    positive.append((value, weight))
            probability = nonzero_days / len(days)
            typical = _weighted_median(positive) if positive else 0.0
            expected = probability * typical
            out.append(DemandSlot(day_type, slot, expected, probability, typical, len(days)))
    return out


def persist_demand_profile(db: sqlite3.Connection, slots: list[DemandSlot]) -> None:
    if not slots:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db:
        for slot in slots:
            db.execute(
                "INSERT INTO dhw_demand_profile("
                "day_type,slot_index,expected_kwh,probability,typical_kwh,sample_days,updated_at"
                ") VALUES(?,?,?,?,?,?,?) ON CONFLICT(day_type,slot_index) DO UPDATE SET "
                "expected_kwh=excluded.expected_kwh,probability=excluded.probability,"
                "typical_kwh=excluded.typical_kwh,sample_days=excluded.sample_days,"
                "updated_at=excluded.updated_at",
                (
                    slot.day_type, slot.slot_index, slot.expected_kwh, slot.probability,
                    slot.typical_kwh, slot.sample_days, now,
                ),
            )
