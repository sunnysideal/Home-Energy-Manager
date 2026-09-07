#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from common.mqtt import MQTTPublisher

LOG = logging.getLogger("ashp_forecast")
HA_API = "http://supervisor/core/api"
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


@dataclass
class Config:
    ch_energy_entity: str
    outdoor_temperature_entity: str
    weather_entity: str
    base_temperature_c: float = 15.5
    summer_mode_off_entity: str = "number.ecomax360i_summer_mode_off"
    summer_mode_on_entity: str = "number.ecomax360i_summer_mode_on"
    winter_mode_below_c: float = 11.0
    summer_mode_above_c: float = 12.0
    initial_kwh_per_degree_day: float = 1.55
    training_days: int = 30
    minimum_daily_degree_days: float = 2.0
    minimum_daily_ch_kwh: float = 2.0
    forecast_hours: int = 48
    forecast_interval_minutes: int = 30
    update_minutes: int = 15
    dhw_energy_entity: str = "sensor.ashp_electrical_energy_dhw"
    dhw_mode_entity: str = "select.dhw_mode"
    dhw_tank_temperature_entity: str = "sensor.dhw_temperature"
    dhw_target_temperature_entity: str = "number.dhw_target_temperature"
    dhw_hysteresis_entity: str = "number.dhw_hysteresis"
    dhw_schedule_prefix: str = "number.dhw_dhw_schedule_"
    dhw_history_days: int = 28
    dhw_activity_threshold_kwh: float = 0.2

    @classmethod
    def load(cls) -> "Config":
        raw = json.loads(OPTIONS_PATH.read_text())
        cfg = cls(**raw)
        return cfg


def resolve_dynamic_thresholds(client: "HAClient", cfg: Config) -> Config:
    try:
        off_state = client.get_state(cfg.summer_mode_off_entity)
        on_state = client.get_state(cfg.summer_mode_on_entity)
        winter_below = float(off_state["state"])
        summer_above = float(on_state["state"])
    except Exception as exc:
        LOG.warning(
            "Could not read summer-mode threshold entities; using configured fallback %.1f/%.1f C: %s",
            cfg.winter_mode_below_c, cfg.summer_mode_above_c, exc,
        )
        return cfg
    if not (math.isfinite(winter_below) and math.isfinite(summer_above)):
        LOG.warning("Summer-mode threshold entity values are not finite; using configured fallback")
        return cfg
    if winter_below >= summer_above:
        raise ValueError(
            f"Invalid summer-mode thresholds from Home Assistant: off={winter_below}, on={summer_above}"
        )
    return replace(cfg, winter_mode_below_c=winter_below, summer_mode_above_c=summer_above)


class HAClient:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not available")
        self.token = token
        self.mqtt = MQTTPublisher(
            "ashp_forecaster",
            "Home Energy Manager – ASHP Forecaster",
            "ASHP Energy Forecaster",
            os.environ.get("HOME_ENERGY_MANAGER_VERSION", "0.1.3"),
            token,
        )
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = f"{HA_API}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(url, data=body, headers=self.headers, method=method)
        try:
            with urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Home Assistant API {method} {path} failed: {exc.code} {detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Home Assistant API request timed out: {path}") from exc
        except URLError as exc:
            raise RuntimeError(f"Home Assistant API unavailable: {exc}") from exc

    def get_config(self) -> dict[str, Any]:
        return self._request("GET", "/config")

    def get_state(self, entity_id: str) -> dict[str, Any]:
        return self._request("GET", f"/states/{quote(entity_id, safe='.')}")

    def get_history(self, entity_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        params = urlencode({
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
        })
        path = f"/history/period/{quote(start.isoformat(), safe=':+')}?{params}"
        data = self._request("GET", path)
        if not data:
            return []
        return data[0] if isinstance(data, list) and data else []

    def get_history_multi(self, entity_ids: list[str], start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
        params = urlencode({
            "filter_entity_id": ",".join(entity_ids),
            "end_time": end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
        })
        path = f"/history/period/{quote(start.isoformat(), safe=':+')}?{params}"
        data = self._request("GET", path) or []
        out = {entity: [] for entity in entity_ids}
        for series in data:
            if series:
                entity_id = series[0].get("entity_id")
                if entity_id:
                    out.setdefault(entity_id, []).extend(series)
        return out

    def get_hourly_weather(self, entity_id: str) -> list[dict[str, Any]]:
        path = "/services/weather/get_forecasts?return_response"
        data = self._request("POST", path, {"entity_id": entity_id, "type": "hourly"})
        service_response = data.get("service_response", {}) if isinstance(data, dict) else {}
        entity = service_response.get(entity_id, {})
        forecast = entity.get("forecast", []) if isinstance(entity, dict) else []
        if not forecast:
            raise RuntimeError(f"{entity_id} returned no hourly forecast")
        return forecast

    def set_sensor(self, entity_id: str, state: float | int | str, attributes: dict[str, Any]) -> None:
        if self.mqtt.publish_sensor(entity_id, state, attributes):
            return
        self._request("POST", f"/states/{entity_id}", {"state": str(state), "attributes": attributes})


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        # Keep the daily summary for diagnostics/backwards compatibility, but the
        # model itself is trained from 30-minute observations.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS daily_training (
                day TEXT PRIMARY KEY,
                degree_days REAL NOT NULL,
                ch_kwh REAL NOT NULL,
                mean_temperature_c REAL,
                valid INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS slot_training (
                start_ts TEXT PRIMARY KEY,
                day TEXT NOT NULL,
                degree_days REAL NOT NULL,
                ch_kwh REAL NOT NULL,
                mean_temperature_c REAL NOT NULL,
                winter_mode_below_c REAL,
                summer_mode_above_c REAL,
                valid INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Lightweight migration for databases created before v0.1.5.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(slot_training)")}
        if "winter_mode_below_c" not in columns:
            self.db.execute("ALTER TABLE slot_training ADD COLUMN winter_mode_below_c REAL")
        if "summer_mode_above_c" not in columns:
            self.db.execute("ALTER TABLE slot_training ADD COLUMN summer_mode_above_c REAL")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_slot_training_day ON slot_training(day)")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS dhw_run_slots (
                day TEXT NOT NULL,
                run_index INTEGER NOT NULL,
                relative_slot INTEGER NOT NULL,
                energy_kwh REAL NOT NULL,
                PRIMARY KEY(day, run_index, relative_slot)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS dhw_fallback_slots (
                day TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                energy_kwh REAL NOT NULL,
                PRIMARY KEY(day, start_ts)
            )
        """)
        row = self.db.execute("SELECT value FROM metadata WHERE key='training_model_version'").fetchone()
        model_version = "3"  # v3 uses historical controller thresholds for each training slot.
        if not row or row[0] != model_version:
            LOG.info("Training model definition changed; rebuilding cached training observations")
            self.db.execute("DELETE FROM slot_training")
            self.db.execute("DELETE FROM daily_training")
            self.db.execute(
                "INSERT INTO metadata(key, value) VALUES('training_model_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (model_version,),
            )
        self.db.commit()

    def replace_dhw_day(self, day: str, runs: list[list[float]], fallback: list[tuple[str, str, float]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM dhw_run_slots WHERE day=?", (day,))
            self.db.execute("DELETE FROM dhw_fallback_slots WHERE day=?", (day,))
            for run_index, run in enumerate(runs):
                for relative_slot, value in enumerate(run):
                    self.db.execute(
                        "INSERT INTO dhw_run_slots(day,run_index,relative_slot,energy_kwh) VALUES(?,?,?,?)",
                        (day, run_index, relative_slot, value),
                    )
            self.db.executemany(
                "INSERT INTO dhw_fallback_slots(day,slot_key,start_ts,energy_kwh) VALUES(?,?,?,?)",
                [(day, key, start_ts, value) for key, start_ts, value in fallback],
            )

    def dhw_samples(self, relative_slot: int, cutoff_day: str) -> list[tuple[str, float]]:
        return [
            (str(row[0]), float(row[1]))
            for row in self.db.execute(
                "SELECT day,energy_kwh FROM dhw_run_slots WHERE relative_slot=? AND day>=?",
                (relative_slot, cutoff_day),
            )
        ]

    def dhw_max_relative_slot(self, cutoff_day: str) -> int | None:
        row = self.db.execute(
            "SELECT MAX(relative_slot) FROM dhw_run_slots WHERE day>=?", (cutoff_day,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def dhw_fallback(self, day: str, key: str, fold: int = 0) -> float | None:
        rows = list(self.db.execute(
            "SELECT start_ts,energy_kwh FROM dhw_fallback_slots WHERE day=? AND slot_key=? ORDER BY start_ts",
            (day, key),
        ))
        if not rows:
            return None
        idx = min(max(int(fold), 0), len(rows) - 1)
        return float(rows[idx][1])

    def dhw_days(self, cutoff_day: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(DISTINCT day) FROM dhw_run_slots WHERE day>=?", (cutoff_day,)
        ).fetchone()
        return int(row[0]) if row else 0

    def has_dhw_day(self, day: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM dhw_fallback_slots WHERE day=? LIMIT 1", (day,)
        ).fetchone()
        return row is not None

    def ensure_training_signature(self, cfg: Config) -> None:
        signature = json.dumps({
            "model_version": 4,
            "base_temperature_c": round(cfg.base_temperature_c, 4),
            "summer_mode_off_entity": cfg.summer_mode_off_entity,
            "summer_mode_on_entity": cfg.summer_mode_on_entity,
            "interval_minutes": cfg.forecast_interval_minutes,
        }, sort_keys=True)
        row = self.db.execute("SELECT value FROM metadata WHERE key='training_signature'").fetchone()
        if not row or row[0] != signature:
            LOG.info("Training settings changed; rebuilding cached training observations")
            with self.db:
                self.db.execute("DELETE FROM slot_training")
                self.db.execute("DELETE FROM daily_training")
                self.db.execute(
                    "INSERT INTO metadata(key, value) VALUES('training_signature', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (signature,),
                )

    def replace_day(self, day: str, slots: list[dict[str, float | str]], dd: float, ch: float,
                    mean_temp: float | None, valid: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("""
                INSERT INTO daily_training(day, degree_days, ch_kwh, mean_temperature_c, valid, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    degree_days=excluded.degree_days,
                    ch_kwh=excluded.ch_kwh,
                    mean_temperature_c=excluded.mean_temperature_c,
                    valid=excluded.valid,
                    updated_at=excluded.updated_at
            """, (day, dd, ch, mean_temp, int(valid), now))
            self.db.execute("DELETE FROM slot_training WHERE day=?", (day,))
            self.db.executemany("""
                INSERT INTO slot_training(
                    start_ts, day, degree_days, ch_kwh, mean_temperature_c,
                    winter_mode_below_c, summer_mode_above_c, valid, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (slot["start_ts"], day, slot["degree_days"], slot["ch_kwh"],
                 slot["mean_temperature_c"], slot.get("winter_mode_below_c"),
                 slot.get("summer_mode_above_c"),
                 int(valid and float(slot["degree_days"]) > 0.0), now)
                for slot in slots
            ])

    def has_slots_for_day(self, day: str) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) FROM slot_training WHERE day=?",
            (day,),
        ).fetchone()
        return bool(row and row[0] >= 48)

    def model(self, initial: float, cutoff_day: str) -> tuple[float, int, float, float, int]:
        # Weighted 30-minute model: sum(actual slot kWh) / sum(slot DD).
        # This is deliberately NOT the arithmetic mean of individual kWh/DD
        # ratios, because tiny DD slots would otherwise dominate the result.
        row = self.db.execute("""
            SELECT COALESCE(SUM(degree_days), 0),
                   COALESCE(SUM(ch_kwh), 0),
                   COUNT(*),
                   COUNT(DISTINCT day)
            FROM slot_training
            WHERE valid=1 AND day>=? AND degree_days>0
        """, (cutoff_day,)).fetchone()
        dd, ch, slots, days = row if row else (0.0, 0.0, 0, 0)
        if dd <= 0:
            return initial, int(days), float(dd), float(ch), int(slots)
        return float(ch) / float(dd), int(days), float(dd), float(ch), int(slots)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def numeric_states(history: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    values: list[tuple[datetime, float]] = []
    for row in history:
        try:
            value = float(row["state"])
            if not math.isfinite(value):
                continue
            ts = parse_dt(row.get("last_changed") or row.get("last_updated"))
            values.append((ts, value))
        except (KeyError, TypeError, ValueError):
            continue
    values.sort(key=lambda x: x[0])
    return values


def value_at_or_before(series: list[tuple[datetime, float]], at: datetime) -> float | None:
    latest = None
    for ts, value in series:
        if ts <= at:
            latest = value
        else:
            break
    return latest


def energy_delta(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    points = [(ts, v) for ts, v in series if start <= ts <= end]
    start_value = value_at_or_before(series, start)
    end_value = value_at_or_before(series, end)
    if start_value is None or end_value is None:
        return None
    values = [(start, start_value)] + points + [(end, end_value)]
    total = 0.0
    prev = values[0][1]
    for _, value in values[1:]:
        delta = value - prev
        # Treat a drop as a meter reset: only count positive energy after reset.
        total += delta if delta >= 0 else max(value, 0)
        prev = value
    return max(total, 0.0)


def time_weighted_mean(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    current = value_at_or_before(series, start)
    if current is None:
        after = next(((ts, v) for ts, v in series if ts >= start), None)
        if after is None or after[0] >= end:
            return None
        current = after[1]
        cursor = after[0]
    else:
        cursor = start

    weighted = 0.0
    seconds = 0.0
    for ts, value in series:
        if ts <= cursor:
            continue
        if ts >= end:
            break
        duration = (ts - cursor).total_seconds()
        weighted += current * duration
        seconds += duration
        current = value
        cursor = ts
    duration = (end - cursor).total_seconds()
    if duration > 0:
        weighted += current * duration
        seconds += duration
    return weighted / seconds if seconds > 0 else None


def degree_days_for_period(mean_temp: float, base: float, hours: float = 24.0) -> float:
    return max(base - mean_temp, 0.0) * hours / 24.0


def update_heating_mode(mode: str, temp_c: float, cfg: Config) -> str:
    """Apply the EcoMAX-style outdoor-temperature hysteresis.

    Summer -> winter when temperature drops below the lower threshold.
    Winter -> summer when temperature rises above the upper threshold.
    Between the thresholds the previous mode is retained.
    """
    if mode == "summer" and temp_c < cfg.winter_mode_below_c:
        return "winter"
    if mode == "winter" and temp_c > cfg.summer_mode_above_c:
        return "summer"
    return mode


def update_heating_mode_with_thresholds(mode: str, temp_c: float, winter_below_c: float, summer_above_c: float) -> str:
    if mode == "summer" and temp_c < winter_below_c:
        return "winter"
    if mode == "winter" and temp_c > summer_above_c:
        return "summer"
    return mode


def initial_mode_from_temp_with_thresholds(
    temp_c: float, winter_below_c: float, summer_above_c: float, default: str = "summer"
) -> str:
    if temp_c < winter_below_c:
        return "winter"
    if temp_c > summer_above_c:
        return "summer"
    return default


def initial_mode_from_temp(temp_c: float, cfg: Config, default: str = "summer") -> str:
    if temp_c < cfg.winter_mode_below_c:
        return "winter"
    if temp_c > cfg.summer_mode_above_c:
        return "summer"
    return default


def infer_current_heating_mode(client: HAClient, cfg: Config, tz: ZoneInfo) -> str:
    """Infer controller mode from the latest threshold crossing.

    If the current temperature is outside the hysteresis band the answer is
    unambiguous. Inside 11-12 C, inspect the previous 24 hours and replay the
    threshold crossings. If history is unavailable, default conservatively to
    summer mode and log the assumption.
    """
    try:
        current = float(client.get_state(cfg.outdoor_temperature_entity)["state"])
    except (KeyError, TypeError, ValueError, RuntimeError):
        current = float("nan")

    if math.isfinite(current):
        if current < cfg.winter_mode_below_c:
            return "winter"
        if current > cfg.summer_mode_above_c:
            return "summer"

    now = datetime.now(tz)
    try:
        history = numeric_states(client.get_history(cfg.outdoor_temperature_entity, now - timedelta(hours=24), now))
    except RuntimeError as exc:
        LOG.warning("Could not infer hysteresis state from recent history: %s", exc)
        return "summer"
    if not history:
        return "summer"

    mode = initial_mode_from_temp(history[0][1], cfg, "summer")
    for _, temp in history:
        mode = update_heating_mode(mode, temp, cfg)
    return mode


def integrated_degree_days(series: list[tuple[datetime, float]], start: datetime, end: datetime, base: float) -> float | None:
    """Integrate max(base - temperature, 0) through the period.

    History states are treated as step values until the next update. This is a
    better shoulder-season training metric than applying the base temperature
    to one whole-day mean.
    """
    current = value_at_or_before(series, start)
    if current is None:
        after = next(((ts, v) for ts, v in series if ts >= start), None)
        if after is None or after[0] >= end:
            return None
        current = after[1]
        cursor = after[0]
    else:
        cursor = start

    degree_hours = 0.0
    for ts, value in series:
        if ts <= cursor:
            continue
        if ts >= end:
            break
        hours = (ts - cursor).total_seconds() / 3600.0
        degree_hours += max(base - current, 0.0) * hours
        current = value
        cursor = ts

    hours = (end - cursor).total_seconds() / 3600.0
    if hours > 0:
        degree_hours += max(base - current, 0.0) * hours
    return degree_hours / 24.0


def build_training(client: HAClient, store: Store, cfg: Config, tz: ZoneInfo) -> tuple[float, int, int]:
    """Backfill completed days and train from historical 30-minute observations.

    CH energy, outdoor temperature, and both EcoMAX summer-mode thresholds are
    taken from Home Assistant history. Each slot therefore uses the controller
    settings that were actually active at that time. The coefficient remains
    the weighted ratio sum(slot kWh) / sum(slot DD) across qualifying slots.
    """
    now = datetime.now(tz)
    first_day = (now - timedelta(days=cfg.training_days)).date()
    interval = timedelta(minutes=30)

    # Threshold entities change rarely, so fetching their full training-window
    # history is much lighter than doing the same for high-frequency temperature
    # data. Start one day early so value_at_or_before() can resolve the setting
    # active at the beginning of the first training day.
    threshold_start = datetime.combine(first_day - timedelta(days=1), datetime.min.time(), tzinfo=tz)
    try:
        off_history = numeric_states(client.get_history(cfg.summer_mode_off_entity, threshold_start, now))
        on_history = numeric_states(client.get_history(cfg.summer_mode_on_entity, threshold_start, now))
    except RuntimeError as exc:
        LOG.warning(
            "Could not read historical summer-mode thresholds; falling back to current values for training: %s",
            exc,
        )
        off_history = []
        on_history = []

    # Current/fallback values are only used if Recorder cannot provide a value
    # for a historical slot. They are not used to reinterpret history otherwise.
    fallback_off = cfg.winter_mode_below_c
    fallback_on = cfg.summer_mode_above_c
    training_mode: str | None = None

    for offset in range(cfg.training_days):
        day = first_day + timedelta(days=offset)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        day_end = day_start + timedelta(days=1)

        if day_end > now or store.has_slots_for_day(day.isoformat()):
            continue

        try:
            energy = numeric_states(client.get_history(cfg.ch_energy_entity, day_start, day_end))
            temps = numeric_states(client.get_history(cfg.outdoor_temperature_entity, day_start, day_end))
        except RuntimeError as exc:
            LOG.warning("Could not backfill training day %s: %s", day, exc)
            continue

        slots: list[dict[str, float | str]] = []
        cursor = day_start
        while cursor < day_end:
            slot_end = min(cursor + interval, day_end)
            ch_slot = energy_delta(energy, cursor, slot_end)
            temp_slot = time_weighted_mean(temps, cursor, slot_end)
            if ch_slot is None or temp_slot is None:
                slots = []
                break

            winter_below = value_at_or_before(off_history, cursor)
            summer_above = value_at_or_before(on_history, cursor)
            if winter_below is None:
                winter_below = fallback_off
            if summer_above is None:
                summer_above = fallback_on
            if winter_below >= summer_above:
                LOG.warning(
                    "Ignoring training day %s because historical thresholds are invalid at %s: %.2f/%.2f C",
                    day, cursor.isoformat(), winter_below, summer_above,
                )
                slots = []
                break

            if training_mode is None:
                # At the beginning of the backfill window the prior hysteresis
                # state is unknown. Outside the band temperature resolves it;
                # in-band CH activity is a useful fallback clue.
                default_mode = "winter" if ch_slot > 0.02 else "summer"
                training_mode = initial_mode_from_temp_with_thresholds(
                    temp_slot, winter_below, summer_above, default_mode
                )
            training_mode = update_heating_mode_with_thresholds(
                training_mode, temp_slot, winter_below, summer_above
            )
            hours = (slot_end - cursor).total_seconds() / 3600.0
            dd_slot = (
                degree_days_for_period(temp_slot, cfg.base_temperature_c, hours)
                if training_mode == "winter" else 0.0
            )
            slots.append({
                "start_ts": cursor.isoformat(),
                "degree_days": dd_slot,
                "ch_kwh": ch_slot,
                "mean_temperature_c": temp_slot,
                "winter_mode_below_c": winter_below,
                "summer_mode_above_c": summer_above,
            })
            cursor = slot_end

        if len(slots) != 48:
            LOG.debug("Insufficient 30-minute history for training day %s", day)
            continue

        dd = sum(float(slot["degree_days"]) for slot in slots)
        ch = sum(float(slot["ch_kwh"]) for slot in slots)
        mean_temp = time_weighted_mean(temps, day_start, day_end)
        valid = dd >= cfg.minimum_daily_degree_days and ch >= cfg.minimum_daily_ch_kwh
        store.replace_day(day.isoformat(), slots, dd, ch, mean_temp, valid)
        used_slots = sum(1 for slot in slots if float(slot["degree_days"]) > 0.0) if valid else 0
        unique_thresholds = {
            (round(float(slot["winter_mode_below_c"]), 2), round(float(slot["summer_mode_above_c"]), 2))
            for slot in slots
        }
        threshold_note = ", ".join(f"{off:.1f}/{on:.1f}C" for off, on in sorted(unique_thresholds))
        LOG.info(
            "Training day %s: %.2f DD from 48 half-hour slots, %.2f kWh CH, "
            "%.2f kWh/DD, %d heating slots, thresholds %s%s",
            day, dd, ch, (ch / dd if dd > 0 else 0.0), used_slots, threshold_note,
            " (used)" if valid else " (ignored)",
        )

    cutoff = (now.date() - timedelta(days=cfg.training_days)).isoformat()
    coefficient, days, _, _, slots = store.model(cfg.initial_kwh_per_degree_day, cutoff)
    return coefficient, days, slots


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def slot_key(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def schedule_entities(prefix: str) -> list[str]:
    return [f"{prefix}{day}_{half}" for day in WEEKDAYS for half in ("am", "pm")]

def schedule_bit_enabled(value: int, local_dt: datetime) -> bool:
    idx = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
    bit = idx if idx < 24 else idx - 24
    return bool(value & (1 << bit))

def weighted_median(values: list[tuple[float, float]]) -> float | None:
    vals = sorted((v, w) for v, w in values if math.isfinite(v) and w > 0)
    if not vals:
        return None
    half = sum(w for _, w in vals) / 2.0
    acc = 0.0
    for value, weight in vals:
        acc += weight
        if acc >= half:
            return value
    return vals[-1][0]

def dhw_profile_value(store: Store, relative_slot: int, target_day: date, cfg: Config) -> float | None:
    cutoff = (target_day - timedelta(days=cfg.dhw_history_days)).isoformat()
    samples = []
    for sample_day, value in store.dhw_samples(relative_slot, cutoff):
        age = max(0, (target_day - date.fromisoformat(sample_day)).days)
        samples.append((value, 0.5 ** (age / 7.0)))
    return weighted_median(samples)

def build_dhw_training(client: HAClient, store: Store, cfg: Config, tz: ZoneInfo) -> int:
    now = datetime.now(tz)
    prefix = cfg.dhw_schedule_prefix
    sched_entities = schedule_entities(prefix)
    latest = now.date() - timedelta(days=1)
    first = latest - timedelta(days=cfg.dhw_history_days - 1)
    for offset in range(cfg.dhw_history_days):
        day = first + timedelta(days=offset)
        if store.has_dhw_day(day.isoformat()):
            continue
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        end = start + timedelta(days=1)
        entities = [cfg.dhw_energy_entity, *sched_entities]
        try:
            hist = client.get_history_multi(entities, start - timedelta(hours=2), end)
        except Exception as exc:
            LOG.warning("Could not build DHW training day %s: %s", day, exc)
            continue
        energy = numeric_states(hist.get(cfg.dhw_energy_entity, []))
        schedules = {e: numeric_states(hist.get(e, [])) for e in sched_entities}
        slots = []
        fallback = []
        cursor = start
        while cursor < end:
            slot_end = cursor + timedelta(minutes=30)
            dhw = energy_delta(energy, cursor, slot_end)
            if dhw is None:
                dhw = 0.0
            fallback.append((slot_key(cursor), cursor.isoformat(), max(0.0, dhw)))
            weekday = WEEKDAYS[cursor.weekday()]
            half = "am" if cursor.hour < 12 else "pm"
            entity = f"{prefix}{weekday}_{half}"
            sv = value_at_or_before(schedules.get(entity, []), cursor) or 0.0
            slots.append((schedule_bit_enabled(int(sv), cursor), max(0.0, dhw)))
            cursor = slot_end
        runs = []
        current = []
        for enabled, dhw in slots:
            if enabled:
                current.append(dhw)
            elif current:
                if sum(current) >= cfg.dhw_activity_threshold_kwh:
                    runs.append(current)
                current = []
        if current and sum(current) >= cfg.dhw_activity_threshold_kwh:
            runs.append(current)
        store.replace_dhw_day(day.isoformat(), runs, fallback)
    cutoff = (now.date() - timedelta(days=cfg.dhw_history_days)).isoformat()
    return store.dhw_days(cutoff)

def current_schedule_values(client: HAClient, prefix: str) -> dict[str, int]:
    out = {}
    for entity in schedule_entities(prefix):
        try:
            out[entity] = int(float(client.get_state(entity)["state"]))
        except Exception:
            out[entity] = 0
    return out

def build_dhw_forecast(client: HAClient, store: Store, cfg: Config, starts: list[datetime]) -> list[float]:
    if not starts:
        return []
    try:
        mode = str(client.get_state(cfg.dhw_mode_entity).get("state", "Off")).lower()
    except Exception:
        mode = "unknown"
    yesterday = lambda s: store.dhw_fallback((s.date() - timedelta(days=1)).isoformat(), slot_key(s), getattr(s, "fold", 0)) or 0.0
    if mode == "off":
        return [0.0] * len(starts)
    cutoff = (starts[0].date() - timedelta(days=cfg.dhw_history_days)).isoformat()
    max_slot = store.dhw_max_relative_slot(cutoff)
    if mode == "on":
        try:
            temp = float(client.get_state(cfg.dhw_tank_temperature_entity)["state"])
            target = float(client.get_state(cfg.dhw_target_temperature_entity)["state"])
            hyst = float(client.get_state(cfg.dhw_hysteresis_entity)["state"])
        except Exception:
            return [yesterday(s) for s in starts]
        if temp >= target - hyst:
            return [0.0] * len(starts)
        out = []
        for i, s in enumerate(starts):
            if max_slot is not None and i > max_slot:
                out.append(0.0); continue
            val = dhw_profile_value(store, i, s.date(), cfg)
            out.append(yesterday(s) if val is None else max(0.0, val))
        return out
    if mode != "schedule":
        return [yesterday(s) for s in starts]
    schedule = current_schedule_values(client, cfg.dhw_schedule_prefix)
    out = []
    prev_enabled = False
    relative = -1
    for s in starts:
        weekday = WEEKDAYS[s.weekday()]
        half = "am" if s.hour < 12 else "pm"
        entity = f"{cfg.dhw_schedule_prefix}{weekday}_{half}"
        enabled = schedule_bit_enabled(schedule.get(entity, 0), s)
        if enabled and not prev_enabled:
            idx = s.hour * 2 + (1 if s.minute >= 30 else 0)
            bit = idx if idx < 24 else idx - 24
            bits = schedule.get(entity, 0)
            run_start = bit
            while run_start > 0 and (bits & (1 << (run_start - 1))):
                run_start -= 1
            relative = bit - run_start
        elif enabled:
            relative += 1
        else:
            relative = -1
        prev_enabled = enabled
        if not enabled or (max_slot is not None and relative > max_slot):
            out.append(0.0); continue
        val = dhw_profile_value(store, relative, s.date(), cfg)
        out.append(yesterday(s) if val is None else max(0.0, val))
    return out

def interpolate_temperature(points: list[tuple[datetime, float]], at: datetime) -> float:
    if at <= points[0][0]:
        return points[0][1]
    if at >= points[-1][0]:
        return points[-1][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= at <= t1:
            span = (t1 - t0).total_seconds()
            f = (at - t0).total_seconds() / span if span else 0
            return v0 + (v1 - v0) * f
    return points[-1][1]


def ceil_time(dt: datetime, minutes: int) -> datetime:
    discard = timedelta(minutes=dt.minute % minutes, seconds=dt.second, microseconds=dt.microsecond)
    rounded = dt - discard
    if discard:
        rounded += timedelta(minutes=minutes)
    return rounded


def build_forecast(client: HAClient, store: Store, cfg: Config, coefficient: float, tz: ZoneInfo) -> list[dict[str, Any]]:
    raw = client.get_hourly_weather(cfg.weather_entity)
    weather: list[tuple[datetime, float]] = []
    for row in raw:
        try:
            temp = float(row["temperature"])
            dt = parse_dt(row["datetime"]).astimezone(tz)
            weather.append((dt, temp))
        except (KeyError, TypeError, ValueError):
            continue
    weather.sort(key=lambda x: x[0])
    if len(weather) < 2:
        raise RuntimeError("Hourly weather forecast has insufficient temperature points")

    now = datetime.now(tz)
    step = timedelta(minutes=cfg.forecast_interval_minutes)
    start = ceil_time(now, cfg.forecast_interval_minutes)
    requested_end = start + timedelta(hours=cfg.forecast_hours)
    available_end = weather[-1][0]
    end = min(requested_end, available_end)

    starts: list[datetime] = []
    cursor = start
    while cursor < end:
        starts.append(cursor)
        cursor += step
    dhw_values = build_dhw_forecast(client, store, cfg, starts)

    result: list[dict[str, Any]] = []
    hours = cfg.forecast_interval_minutes / 60.0
    mode = infer_current_heating_mode(client, cfg, tz)
    LOG.info(
        "Forecast starts in %s mode (winter below %.1f C, summer above %.1f C)",
        mode, cfg.winter_mode_below_c, cfg.summer_mode_above_c,
    )
    for cursor, dhw_kwh in zip(starts, dhw_values):
        temp = interpolate_temperature(weather, cursor)
        mode = update_heating_mode(mode, temp, cfg)
        heating_enabled = mode == "winter"
        dd = degree_days_for_period(temp, cfg.base_temperature_c, hours) if heating_enabled else 0.0
        raw_ch_kwh = dd * coefficient
        # DHW has controller priority. At 30-minute forecast resolution a slot with
        # forecast DHW activity contains no CH; missed CH is not carried forward.
        ch_kwh = 0.0 if dhw_kwh > 0.0 else raw_ch_kwh
        total_kwh = ch_kwh + dhw_kwh
        result.append({
            "start": cursor.isoformat(),
            "temperature_c": round(temp, 2),
            "heating_mode": mode,
            "heating_enabled": heating_enabled and dhw_kwh <= 0.0,
            "dhw_active": dhw_kwh > 0.0,
            "degree_days": round(dd, 5),
            "ch_kwh": round(ch_kwh, 4),
            "dhw_kwh": round(dhw_kwh, 4),
            "energy_kwh": round(total_kwh, 4),
        })
    return result


def sum_until(forecast: list[dict[str, Any]], end: datetime) -> float:
    return sum(float(slot["energy_kwh"]) for slot in forecast if parse_dt(slot["start"]) < end)


def publish(client: HAClient, cfg: Config, coefficient: float, training_days: int, training_slots: int, forecast: list[dict[str, Any]], tz: ZoneInfo) -> None:
    now = datetime.now(tz)
    next_30 = forecast[0]["energy_kwh"] if forecast else 0.0
    next_24_end = now + timedelta(hours=24)
    next_48_end = now + timedelta(hours=48)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    common = {
        "model": "heating_degree_days",
        "base_temperature_c": cfg.base_temperature_c,
        "winter_mode_below_c": cfg.winter_mode_below_c,
        "summer_mode_above_c": cfg.summer_mode_above_c,
        "kwh_per_degree_day": round(coefficient, 4),
        "training_days_used": training_days,
        "training_slots_used": training_slots,
        "last_updated": now.isoformat(),
    }

    def sensor(entity: str, state: float, name: str, extra: dict[str, Any] | None = None):
        attrs = {
            "friendly_name": name,
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            **common,
        }
        if extra:
            attrs.update(extra)
        client.set_sensor(entity, round(state, 3), attrs)

    sensor("sensor.ashp_forecast_next_30m", next_30, "ASHP Forecast Next 30m")
    sensor("sensor.ashp_forecast_remaining_today", sum_until(forecast, midnight), "ASHP Forecast Remaining Today")
    sensor("sensor.ashp_forecast_next_24h", sum_until(forecast, next_24_end), "ASHP Forecast Next 24h", {"forecast": forecast})
    sensor("sensor.ashp_forecast_next_48h", sum_until(forecast, next_48_end), "ASHP Forecast Next 48h", {"forecast": forecast})
    ch_48 = sum(float(slot.get("ch_kwh", 0.0)) for slot in forecast if parse_dt(slot["start"]) < next_48_end)
    dhw_48 = sum(float(slot.get("dhw_kwh", 0.0)) for slot in forecast if parse_dt(slot["start"]) < next_48_end)
    sensor("sensor.ashp_forecast_ch_next_48h", ch_48, "ASHP CH Forecast Next 48h")
    sensor("sensor.ashp_forecast_dhw_next_48h", dhw_48, "ASHP DHW Forecast Next 48h")

    client.set_sensor(
        "sensor.ashp_forecast_kwh_per_degree_day",
        round(coefficient, 4),
        {
            "friendly_name": "ASHP Forecast kWh per Degree Day",
            "unit_of_measurement": "kWh/DD",
            "base_temperature_c": cfg.base_temperature_c,
            "training_days_used": training_days,
            "training_slots_used": training_slots,
            "last_updated": now.isoformat(),
        },
    )


def run_once(client: HAClient, store: Store, cfg: Config, tz: ZoneInfo) -> None:
    cfg = resolve_dynamic_thresholds(client, cfg)
    store.ensure_training_signature(cfg)
    LOG.info(
        "Using summer-mode thresholds from HA: off %.1f C (%s), on %.1f C (%s)",
        cfg.winter_mode_below_c, cfg.summer_mode_off_entity,
        cfg.summer_mode_above_c, cfg.summer_mode_on_entity,
    )
    coefficient, training_days, training_slots = build_training(client, store, cfg, tz)
    dhw_days = build_dhw_training(client, store, cfg, tz)
    forecast = build_forecast(client, store, cfg, coefficient, tz)
    publish(client, cfg, coefficient, training_days, training_slots, forecast, tz)
    total24 = sum(float(x["energy_kwh"]) for x in forecast[: int(24 * 60 / cfg.forecast_interval_minutes)])
    LOG.info(
        "Forecast published: coefficient=%.3f kWh/DD, training_days=%d, training_slots=%d, forecast_slots=%d, approx_next_24h=%.2f kWh",
        coefficient, training_days, training_slots, len(forecast), total24,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load()
    client = HAClient(os.environ.get("SUPERVISOR_TOKEN", ""))
    ha_cfg = client.get_config()
    tz = ZoneInfo(ha_cfg.get("time_zone", "UTC"))
    store = Store(DB_PATH)

    LOG.info("ASHP Energy Forecaster starting in timezone %s", tz.key)
    LOG.info("CH=%s temperature=%s weather=%s", cfg.ch_energy_entity, cfg.outdoor_temperature_entity, cfg.weather_entity)

    while True:
        try:
            run_once(client, store, cfg, tz)
        except Exception:
            LOG.exception("Forecast update failed")
        time.sleep(cfg.update_minutes * 60)


if __name__ == "__main__":
    main()
