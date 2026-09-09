"""One-time Home Assistant Recorder bootstrap for the DHW thermal model.

This module is deliberately passive: it imports historical observations into the same
ASHP-owned thermal-model tables used by the live collector. It does not publish or
select forecasts and cannot make the thermal model authoritative.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from dhw_cycle_tracker import CycleSample, build_cycle
from dhw_draw_detector import ThermalSample, detect_draw

HA_API = "http://supervisor/core/api"
BOOTSTRAP_VERSION = 1
BOOTSTRAP_COMPLETE_KEY = f"dhw_history_bootstrap_v{BOOTSTRAP_VERSION}_complete"
BOOTSTRAP_COUNT_KEY = f"dhw_history_bootstrap_v{BOOTSTRAP_VERSION}_samples"
BOOTSTRAP_DAYS_KEY = f"dhw_history_bootstrap_v{BOOTSTRAP_VERSION}_days"


@dataclass(frozen=True)
class BootstrapResult:
    days_requested: int
    days_imported: int
    samples_inserted: int
    draws_inserted: int
    cycles_inserted: int


def _history(token: str, entities: list[str], start: datetime, end: datetime) -> dict[str, list[dict]]:
    entities = [e for e in entities if e]
    params = urlencode({"end_time": end.isoformat(), "filter_entity_id": ",".join(entities)})
    path = f"/history/period/{quote(start.isoformat(), safe='')}?{params}&minimal_response&no_attributes"
    req = Request(
        f"{HA_API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET",
    )
    with urlopen(req, timeout=90) as response:
        payload = json.loads(response.read())
    result: dict[str, list[dict]] = {entity: [] for entity in entities}
    for series in payload or []:
        if not isinstance(series, list) or not series:
            continue
        entity_id = str(series[0].get("entity_id") or "")
        if entity_id:
            result.setdefault(entity_id, []).extend(x for x in series if isinstance(x, dict))
    return result


def _timestamp(item: dict) -> datetime | None:
    raw = item.get("last_changed") or item.get("last_updated")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _series_points(items: list[dict]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for item in items:
        ts = _timestamp(item)
        if ts is None:
            continue
        try:
            value = float(item.get("state"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append((ts, value))
    out.sort(key=lambda x: x[0])
    return out


def _value_at(points: list[tuple[datetime, float]], boundary: datetime) -> float | None:
    value = None
    for ts, candidate in points:
        if ts > boundary:
            break
        value = candidate
    return value


def _valid_temp(value: float | None) -> bool:
    return value is not None and 5.0 <= value <= 80.0


def _insert_cycle(db: sqlite3.Connection, cycle) -> bool:
    exists = db.execute(
        "SELECT 1 FROM dhw_heating_cycles WHERE start_ts=? AND end_ts=? LIMIT 1",
        (cycle.start_ts.isoformat(), cycle.end_ts.isoformat()),
    ).fetchone()
    if exists:
        return False
    db.execute(
        "INSERT INTO dhw_heating_cycles("
        "start_ts,end_ts,start_upper_c,start_lower_c,end_upper_c,end_lower_c,"
        "target_temp_c,electrical_kwh,outdoor_temp_c,cycle_type,valid"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            cycle.start_ts.isoformat(), cycle.end_ts.isoformat(),
            cycle.start_upper_c, cycle.start_lower_c, cycle.end_upper_c, cycle.end_lower_c,
            cycle.target_temp_c, cycle.electrical_kwh, cycle.outdoor_temp_c,
            cycle.cycle_type, int(cycle.valid),
        ),
    )
    return True


def _persist_cycles(db: sqlite3.Connection, samples: list[CycleSample]) -> int:
    inserted = 0
    ordered = sorted(samples, key=lambda s: s.timestamp)
    idx = 1
    while idx < len(ordered):
        if ordered[idx - 1].dhw_heating or not ordered[idx].dhw_heating:
            idx += 1
            continue
        start = idx - 1
        end = None
        for j in range(idx + 1, len(ordered)):
            if ordered[j - 1].dhw_heating and not ordered[j].dhw_heating:
                end = j
                break
        if end is None:
            break
        cycle = build_cycle(ordered[start:end + 1])
        if cycle is not None and _insert_cycle(db, cycle):
            inserted += 1
        idx = end + 1
    return inserted


def _import_day(
    db: sqlite3.Connection,
    token: str,
    cfg: dict,
    day: date,
    tz: ZoneInfo,
    sample_minutes: int,
) -> tuple[int, int, int]:
    upper_entity = str(cfg.get("dhw_tank_upper_temperature_entity") or cfg.get("dhw_tank_temperature_entity") or "")
    lower_entity = str(cfg.get("dhw_tank_lower_temperature_entity") or "")
    energy_entity = str(cfg.get("dhw_energy_entity") or "")
    target_entity = str(cfg.get("dhw_target_temperature_entity") or "")
    ambient_entity = str(cfg.get("dhw_ambient_temperature_entity") or "")
    outdoor_entity = str(cfg.get("outdoor_temperature_entity") or "")
    entities = [upper_entity, lower_entity, energy_entity, target_entity, ambient_entity, outdoor_entity]

    local_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    local_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    # Lead-in provides a state immediately before midnight for cumulative differencing
    # and for sensors that did not change exactly at the day boundary.
    history_start = local_start - timedelta(hours=2)
    raw = _history(token, entities, history_start, local_end)
    points = {entity: _series_points(raw.get(entity, [])) for entity in entities if entity}

    activity_threshold = max(0.0, float(cfg.get("dhw_activity_threshold_kwh", 0.2)))
    volume_l = float(cfg.get("dhw_tank_volume_l", 250))
    inserted_samples = 0
    inserted_draws = 0
    cycle_samples: list[CycleSample] = []
    previous: ThermalSample | None = None
    previous_energy: float | None = None

    cursor = local_start.astimezone(timezone.utc)
    finish = local_end.astimezone(timezone.utc)
    step = timedelta(minutes=max(1, sample_minutes))
    while cursor < finish:
        upper = _value_at(points.get(upper_entity, []), cursor)
        lower = _value_at(points.get(lower_entity, []), cursor)
        energy = _value_at(points.get(energy_entity, []), cursor) if energy_entity else None
        target = _value_at(points.get(target_entity, []), cursor) if target_entity else None
        ambient = _value_at(points.get(ambient_entity, []), cursor) if ambient_entity else None
        outdoor = _value_at(points.get(outdoor_entity, []), cursor) if outdoor_entity else None
        valid = _valid_temp(upper) and _valid_temp(lower)

        energy_delta = None
        if energy is not None and previous_energy is not None:
            delta = energy - previous_energy
            if 0.0 <= delta <= 10.0:
                energy_delta = delta
        heating = bool(energy_delta is not None and energy_delta >= min(activity_threshold, 0.05))
        ts = cursor.isoformat()
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO dhw_thermal_samples("
            "timestamp,upper_temp_c,lower_temp_c,dhw_heating,immersion_heating,"
            "dhw_energy_delta_kwh,dhw_energy_total_kwh,target_temp_c,ambient_temp_c,"
            "outdoor_temp_c,valid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, upper, lower, int(heating), 0, energy_delta, energy, target, ambient, outdoor, int(valid)),
        )
        if db.total_changes > before:
            inserted_samples += 1

        current = None
        if valid and upper is not None and lower is not None:
            current = ThermalSample(cursor, upper, lower, heating, True)
            if previous is not None:
                draw = detect_draw(previous, current, volume_l=volume_l)
                if draw is not None:
                    before = db.total_changes
                    db.execute(
                        "INSERT OR IGNORE INTO dhw_draw_events("
                        "timestamp,estimated_thermal_kwh,confidence,upper_before_c,lower_before_c,"
                        "upper_after_c,lower_after_c) VALUES(?,?,?,?,?,?,?)",
                        (
                            draw.timestamp.isoformat(), draw.estimated_thermal_kwh, draw.confidence,
                            draw.upper_before_c, draw.lower_before_c, draw.upper_after_c, draw.lower_after_c,
                        ),
                    )
                    if db.total_changes > before:
                        inserted_draws += 1
            cycle_samples.append(
                CycleSample(
                    timestamp=cursor,
                    upper_temp_c=upper,
                    lower_temp_c=lower,
                    dhw_heating=heating,
                    dhw_energy_total_kwh=energy,
                    outdoor_temp_c=outdoor,
                    target_temp_c=target,
                    immersion_heating=False,
                    valid=True,
                )
            )
        previous = current
        if energy is not None:
            previous_energy = energy
        cursor += step

    inserted_cycles = _persist_cycles(db, cycle_samples)
    db.commit()
    return inserted_samples, inserted_draws, inserted_cycles


def bootstrap_history(
    db: sqlite3.Connection,
    token: str,
    cfg: dict,
    *,
    timezone_name: str,
) -> BootstrapResult | None:
    existing = db.execute("SELECT value FROM metadata WHERE key=?", (BOOTSTRAP_COMPLETE_KEY,)).fetchone()
    if existing and str(existing[0]) == "1":
        return None

    upper = str(cfg.get("dhw_tank_upper_temperature_entity") or cfg.get("dhw_tank_temperature_entity") or "")
    lower = str(cfg.get("dhw_tank_lower_temperature_entity") or "")
    energy = str(cfg.get("dhw_energy_entity") or "")
    if not upper or not lower or not energy:
        return None

    tz = ZoneInfo(timezone_name)
    days_requested = max(1, min(90, int(cfg.get("dhw_history_days", 28))))
    sample_minutes = max(1, int(cfg.get("dhw_thermal_sample_minutes", 5)))
    today = datetime.now(tz).date()
    days = [today - timedelta(days=offset) for offset in range(days_requested, 0, -1)]

    samples = draws = cycles = imported_days = 0
    for day in days:
        day_samples, day_draws, day_cycles = _import_day(db, token, cfg, day, tz, sample_minutes)
        samples += day_samples
        draws += day_draws
        cycles += day_cycles
        imported_days += 1

    with db:
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (BOOTSTRAP_COMPLETE_KEY, "1"),
        )
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (BOOTSTRAP_COUNT_KEY, str(samples)),
        )
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (BOOTSTRAP_DAYS_KEY, str(imported_days)),
        )
    return BootstrapResult(days_requested, imported_days, samples, draws, cycles)
