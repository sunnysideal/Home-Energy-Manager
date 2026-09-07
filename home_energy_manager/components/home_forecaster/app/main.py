from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from common.mqtt import MQTTPublisher

VERSION = "0.2.14"
LOG = logging.getLogger("home_energy_forecaster")
HA_API = "http://supervisor/core/api"
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
DB_PATH = Path(os.environ.get("HOME_FORECASTER_DB_PATH", "/data/home_energy_forecaster.db"))
LAST_FORECAST_PATH = Path(os.environ.get("HOME_FORECASTER_LAST_FORECAST_PATH", "/data/last_forecast.json"))
OUTPUT_ENTITY = "sensor.home_energy_forecast"
HEALTH_ENTITY = "sensor.home_energy_forecast_health"
COMPARISON_ENTITY = "sensor.home_energy_forecast_comparison"

# Fixed v1 behaviour agreed during design.
RECENT_LOOKBACK_HOURS = 2
RECENT_FADE_HOURS = 3
RECENT_MULTIPLIER_MIN = 0.85
RECENT_MULTIPLIER_MAX = 1.15
RECENCY_HALF_LIFE_DAYS = 7.0
ASHP_STALE_HOURS = 2
SOLCAST_STALE_HOURS = 26
FALLBACK_RESERVE_SOC = 4.0
PERSISTED_FORECAST_STALE_MINUTES = 30
DAILY_REBUILD_TIME = dtime(0, 15)


@dataclass
class Config:
    raw: dict[str, Any]

    @classmethod
    def load(cls) -> "Config":
        return cls(json.loads(OPTIONS_PATH.read_text()))

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def setting(self, name: str, default: Any = None) -> Any:
        return self.section("settings").get(name, default)


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self) -> None:
        token = os.getenv("SUPERVISOR_TOKEN")
        if not token:
            raise HAError("SUPERVISOR_TOKEN is unavailable")
        self.token = token
        self.mqtt = MQTTPublisher(
            "home_forecaster",
            "Home Energy Manager – Home Forecaster",
            "Home Energy Forecaster",
            os.environ.get("HOME_ENERGY_MANAGER_VERSION", "0.1.3"),
            token,
        )
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, payload: Any | None = None, timeout: int = 60) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(f"{HA_API}{path}", data=data, method=method, headers=self.headers)
        try:
            with urlopen(req, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode()) if raw else None
        except (HTTPError, URLError, TimeoutError) as exc:
            raise HAError(f"{method} {path} failed: {exc}") from exc

    def state(self, entity_id: str) -> dict[str, Any]:
        return self.request("GET", f"/states/{quote(entity_id, safe='._')}")

    def state_optional(self, entity_id: str) -> dict[str, Any] | None:
        if not entity_id:
            return None
        try:
            return self.state(entity_id)
        except HAError:
            return None

    def publish(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
        if self.mqtt.publish_sensor(entity_id, state, attributes):
            return
        self.request("POST", f"/states/{quote(entity_id, safe='._')}", {"state": str(state), "attributes": attributes})

    def history(self, entities: list[str], start: datetime, end: datetime, timeout: int = 60) -> dict[str, list[dict[str, Any]]]:
        entities = [e for e in entities if e]
        if not entities:
            return {}
        params = urlencode({"end_time": end.isoformat(), "filter_entity_id": ",".join(entities)})
        path = f"/history/period/{quote(start.isoformat(), safe='')}?{params}&minimal_response&no_attributes"
        data = self.request("GET", path, timeout=timeout)
        result: dict[str, list[dict[str, Any]]] = {e: [] for e in entities}
        for series in data or []:
            if not series:
                continue
            entity_id = series[0].get("entity_id")
            if not entity_id:
                # minimal_response may omit entity_id after the first, but first normally retains it.
                continue
            result.setdefault(entity_id, []).extend(series)
        return result


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS baseline_slots (
                day TEXT NOT NULL,
                start_iso TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                baseline_kwh REAL NOT NULL,
                PRIMARY KEY(day, start_iso)
            );
            CREATE TABLE IF NOT EXISTS dhw_run_slots (
                day TEXT NOT NULL,
                run_index INTEGER NOT NULL,
                relative_slot INTEGER NOT NULL,
                energy_kwh REAL NOT NULL,
                PRIMARY KEY(day, run_index, relative_slot)
            );
            CREATE TABLE IF NOT EXISTS fallback_slots (
                day TEXT NOT NULL,
                profile TEXT NOT NULL,
                start_iso TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                energy_kwh REAL NOT NULL,
                PRIMARY KEY(day, profile, start_iso)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forecast_snapshots (
                day TEXT PRIMARY KEY,
                captured_at TEXT NOT NULL,
                forecast_json TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def clear_day(self, day: date) -> None:
        ds = day.isoformat()
        self.db.execute("DELETE FROM baseline_slots WHERE day=?", (ds,))
        self.db.execute("DELETE FROM dhw_run_slots WHERE day=?", (ds,))
        self.db.execute("DELETE FROM fallback_slots WHERE day=?", (ds,))
        self.db.commit()

    def save_baseline(self, day: date, start: datetime, key: str, value: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO baseline_slots(day,start_iso,slot_key,baseline_kwh) VALUES(?,?,?,?)", (day.isoformat(), start.isoformat(), key, value))

    def save_fallback(self, day: date, profile: str, start: datetime, key: str, value: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO fallback_slots(day,profile,start_iso,slot_key,energy_kwh) VALUES(?,?,?,?,?)", (day.isoformat(), profile, start.isoformat(), key, value))

    def commit(self) -> None:
        self.db.commit()

    def baseline_samples(self, slot_key: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT day,baseline_kwh FROM baseline_slots WHERE slot_key=?", (slot_key,)))

    def fallback(self, day: date, profile: str, key: str, fold: int = 0) -> float | None:
        rows = list(self.db.execute("SELECT start_iso,energy_kwh FROM fallback_slots WHERE day=? AND profile=? AND slot_key=? ORDER BY start_iso", (day.isoformat(), profile, key)))
        if not rows:
            return None
        idx = min(max(int(fold), 0), len(rows) - 1)
        return float(rows[idx]["energy_kwh"])

    def count_days(self, table: str) -> int:
        row = self.db.execute(f"SELECT COUNT(DISTINCT day) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def prune_before(self, day: date) -> None:
        ds = day.isoformat()
        for table in ("baseline_slots", "dhw_run_slots", "fallback_slots"):
            self.db.execute(f"DELETE FROM {table} WHERE day < ?", (ds,))
        self.db.commit()

    def save_forecast_snapshot(self, day: date, captured_at: datetime, forecast_slots: list[dict[str, Any]]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO forecast_snapshots(day,captured_at,forecast_json) VALUES(?,?,?)",
            (day.isoformat(), captured_at.isoformat(), json.dumps(forecast_slots, separators=(",", ":"))),
        )
        self.db.commit()

    def forecast_snapshot(self, day: date) -> tuple[str, list[dict[str, Any]]] | None:
        row = self.db.execute(
            "SELECT captured_at,forecast_json FROM forecast_snapshots WHERE day=?", (day.isoformat(),)
        ).fetchone()
        if not row:
            return None
        try:
            slots = json.loads(row["forecast_json"])
            if not isinstance(slots, list):
                return None
            return str(row["captured_at"]), slots
        except Exception:
            return None

    def prune_forecast_snapshots_before(self, day: date) -> None:
        self.db.execute("DELETE FROM forecast_snapshots WHERE day < ?", (day.isoformat(),))
        self.db.commit()


# ---------- Generic helpers ----------

def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        raise ValueError("empty datetime")
    s = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def numeric_state(state: dict[str, Any] | None, default: float | None = None) -> float | None:
    if not state:
        return default
    try:
        return float(state.get("state"))
    except (TypeError, ValueError):
        return default


def latest_text_state_before(states: list[dict[str, Any]], boundary: datetime) -> str | None:
    """Return the last textual state at or before boundary."""
    best_time: datetime | None = None
    best_value: str | None = None
    b_utc = boundary.astimezone(timezone.utc)
    for item in states:
        stamp = item.get("last_changed") or item.get("last_updated")
        try:
            t = parse_dt(stamp).astimezone(timezone.utc)
        except Exception:
            continue
        if t <= b_utc and (best_time is None or t > best_time):
            best_time = t
            best_value = str(item.get("state", "")).lower()
    return best_value


def ev_active_during(states: list[dict[str, Any]], start: datetime, end: datetime) -> bool | None:
    """Return True if EV charging was active at any point in [start,end).

    None means the history cannot establish the EV state for the interval.
    When the configured battery house-load includes EV load, callers treat
    None conservatively and do not use that interval for forecasting/learning.
    """
    state_at_start = latest_text_state_before(states, start)
    if state_at_start is None:
        return None
    if state_at_start in {"on", "true", "1", "yes", "enabled"}:
        return True
    a_utc = start.astimezone(timezone.utc)
    b_utc = end.astimezone(timezone.utc)
    for item in states:
        stamp = item.get("last_changed") or item.get("last_updated")
        try:
            t = parse_dt(stamp).astimezone(timezone.utc)
        except Exception:
            continue
        if a_utc < t < b_utc and str(item.get("state", "")).lower() in {"on", "true", "1", "yes", "enabled"}:
            return True
    return False


def effective_grid_energy_entities(client: "HAClient", cfg: "Config") -> tuple[str, str, str, str]:
    """Select cumulative grid-energy entities for forecasting and control.

    A meter entity explicitly configured by the user is authoritative.  Do not
    silently switch the accounting model to the inverter/battery entity merely
    because one instantaneous Home Assistant state read fails.  Availability is
    validated by ``make_forecast`` after selection, which produces a visible
    forecast error instead of silently crediting export from the wrong source.

    The inverter/battery cumulative entities are fallbacks only when the
    corresponding ``meter`` option is not configured at all.
    """
    meter = cfg.section("meter")
    tariff = cfg.section("tariff")

    true_import = str(meter.get("import_energy_total_kwh") or "").strip()
    true_export = str(meter.get("export_energy_total_kwh") or "").strip()
    fallback_import = str(tariff.get("import_energy_total_kwh") or "").strip()
    fallback_export = str(tariff.get("export_energy_total_kwh") or "").strip()

    def choose(preferred: str, fallback: str, direction: str) -> tuple[str, str]:
        if preferred:
            LOG.info("Grid %s energy source: true_meter entity=%s", direction, preferred)
            return preferred, "true_meter"
        LOG.info("Grid %s energy source: battery fallback entity=%s (no true meter configured)",
                 direction, fallback or "<none>")
        return fallback, "battery"

    imp, imp_source = choose(true_import, fallback_import, "import")
    exp, exp_source = choose(true_export, fallback_export, "export")
    return imp, exp, imp_source, exp_source


def bool_state(state: dict[str, Any] | None, default: bool = False) -> bool:
    if not state:
        return default
    return str(state.get("state", "")).lower() in {"on", "true", "1", "yes", "enabled"}


def state_age_hours(state: dict[str, Any] | None, now: datetime) -> float:
    if not state:
        return math.inf
    stamp = state.get("last_updated") or state.get("last_changed")
    try:
        return max(0.0, (now.astimezone(timezone.utc) - parse_dt(stamp).astimezone(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return math.inf


def slot_key(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=tz)
    return start, end


def real_half_hours(start: datetime, end: datetime) -> list[datetime]:
    """Real 30-minute boundaries. UTC stepping preserves duplicate/missing local DST slots."""
    out: list[datetime] = []
    cursor = start.astimezone(timezone.utc)
    finish = end.astimezone(timezone.utc)
    while cursor <= finish:
        out.append(cursor.astimezone(start.tzinfo))
        cursor += timedelta(minutes=30)
    return out


def latest_before(states: list[dict[str, Any]], boundary: datetime) -> float | None:
    best_time: datetime | None = None
    best_value: float | None = None
    b_utc = boundary.astimezone(timezone.utc)
    for item in states:
        stamp = item.get("last_changed") or item.get("last_updated")
        try:
            t = parse_dt(stamp).astimezone(timezone.utc)
            if t <= b_utc and (best_time is None or t > best_time):
                v = float(item.get("state"))
                best_time, best_value = t, v
        except (TypeError, ValueError):
            continue
    return best_value


def diff_cumulative(states: list[dict[str, Any]], start: datetime, end: datetime) -> float | None:
    a = latest_before(states, start)
    b = latest_before(states, end)
    if a is None or b is None or b < a:
        return None
    return b - a


def weighted_median(values: list[tuple[float, float]]) -> float | None:
    values = [(v, w) for v, w in values if math.isfinite(v) and w > 0]
    if not values:
        return None
    values.sort(key=lambda x: x[0])
    total = sum(w for _, w in values)
    acc = 0.0
    for v, w in values:
        acc += w
        if acc >= total / 2:
            return v
    return values[-1][0]


def weighted_quantile(values: list[tuple[float, float]], quantile: float) -> float | None:
    """Return a recency-weighted quantile for (value, weight) samples."""
    cleaned = [(v, w) for v, w in values if math.isfinite(v) and math.isfinite(w) and w > 0]
    if not cleaned:
        return None
    cleaned.sort(key=lambda x: x[0])
    q = clamp(float(quantile), 0.0, 1.0)
    total = sum(w for _, w in cleaned)
    threshold = q * total
    acc = 0.0
    for v, w in cleaned:
        acc += w
        if acc >= threshold:
            return v
    return cleaned[-1][0]


def weighted_winsorized_mean(
    values: list[tuple[float, float]], lower_q: float = 0.10, upper_q: float = 0.90
) -> float | None:
    """Recency-weighted mean after winsorising the tails.

    The 10th/90th percentile caps make the baseline robust to occasional unusual
    half-hours while still allowing the 0.1 kWh cumulative-meter quantisation
    to average into useful hundredths over many historical days.
    """
    cleaned = [(v, w) for v, w in values if math.isfinite(v) and math.isfinite(w) and w > 0]
    if not cleaned:
        return None
    lo = weighted_quantile(cleaned, lower_q)
    hi = weighted_quantile(cleaned, upper_q)
    if lo is None or hi is None:
        return None
    if hi < lo:
        lo, hi = hi, lo
    total_weight = sum(w for _, w in cleaned)
    if total_weight <= 0:
        return None
    return sum(clamp(v, lo, hi) * w for v, w in cleaned) / total_weight


def recency_weight(sample_day: date, target_day: date) -> float:
    age = max(0.0, float((target_day - sample_day).days))
    return 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalise_rate_p(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # BottlecapDave values are normally GBP/kWh; tolerate p/kWh sources too.
    return v * 100.0 if abs(v) <= 2.0 else v


# ---------- Historical model building ----------

def model_day(client: HAClient, store: Store, cfg: Config, day: date, tz: ZoneInfo) -> None:
    load_e = cfg.section("load")["energy_total_kwh"]
    ch_e = cfg.section("ashp")["ch_energy_total_kwh"]
    dhw_e = cfg.section("ashp")["dhw_energy_total_kwh"]
    pv_e = cfg.section("solar")["energy_total_kwh"]
    load_cfg = cfg.section("load")
    ev_included = bool(load_cfg.get("ev_included_in_battery_load", False))
    ev_e = str(load_cfg.get("ev_charging_entity") or "").strip()
    entities = [load_e, ch_e, dhw_e, pv_e]
    if ev_included:
        if not ev_e:
            raise HAError("EV is configured as included in battery house load but load.ev_charging_entity is blank")
        entities.append(ev_e)
    start, end = day_bounds(day, tz)
    # Ask for a little lead-in so a state before midnight is available for nearest-before differencing.
    hist = client.history(entities, start - timedelta(hours=2), end, timeout=90)
    store.clear_day(day)
    boundaries = real_half_hours(start, end)
    min_slot_kwh = float(cfg.setting("minimum_baseline_w", 200)) / 1000.0 * 0.5

    slots: list[dict[str, Any]] = []
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        load = diff_cumulative(hist.get(load_e, []), a, b)
        ch_raw = diff_cumulative(hist.get(ch_e, []), a, b)
        dhw_raw = diff_cumulative(hist.get(dhw_e, []), a, b)
        pv = diff_cumulative(hist.get(pv_e, []), a, b)
        ch = 0.0 if ch_raw is None else ch_raw
        dhw = 0.0 if dhw_raw is None else dhw_raw

        # Baseline learning requires total load; CH/DHW may safely fall back to zero if absent.
        # EV load is never learned/forecast. If the configured battery load includes
        # the EV, omit any interval where EV charging was active or cannot be ruled out.
        ev_clean = True
        if ev_included:
            ev_state = ev_active_during(hist.get(ev_e, []), a, b)
            ev_clean = (ev_state is False)
        if load is not None and ev_clean:
            baseline = max(min_slot_kwh, load - ch - dhw)
            store.save_baseline(day, a, slot_key(a), baseline)

        # Fallback profiles and DHW learning are independent of total-load availability.
        if pv is not None:
            store.save_fallback(day, "pv", a, slot_key(a), pv)
        if ch_raw is not None:
            store.save_fallback(day, "ch", a, slot_key(a), ch)
        if dhw_raw is not None:
            store.save_fallback(day, "dhw", a, slot_key(a), dhw)

    # DHW forecasting is owned by ASHP Energy Forecaster. Home Energy Forecaster
    # retains only yesterday DHW fallback values for resilience and actual/baseline accounting.
    store.commit()


def rebuild_models(client: HAClient, store: Store, cfg: Config, now: datetime, force: bool = False) -> tuple[int, int]:
    history_days = int(cfg.setting("history_days", 28))
    latest_complete = now.date() - timedelta(days=1)
    start_day = latest_complete - timedelta(days=history_days - 1)
    store.prune_before(start_day)
    days = [start_day + timedelta(days=i) for i in range(history_days)] if force or store.count_days("baseline_slots") == 0 else [latest_complete]
    failures: list[str] = []
    for i, d in enumerate(days):
        LOG.info("Model rebuild: processing %s (%d/%d)", d, i + 1, len(days))
        try:
            model_day(client, store, cfg, d, now.tzinfo)  # type: ignore[arg-type]
        except Exception as exc:
            msg = f"{d}: {exc}"
            failures.append(msg)
            LOG.warning("Model rebuild: skipping %s", msg)
    if failures and not force:
        raise HAError("Daily model rebuild failed: " + "; ".join(failures))
    store.meta_set("last_model_rebuild", now.isoformat())
    return store.count_days("baseline_slots"), store.count_days("fallback_slots")


def baseline_for(store: Store, cfg: Config, target: datetime, min_slot_kwh: float) -> float:
    """Historical baseline with recency decay plus same-day-of-week weighting.

    The existing 28-day history and 7-day recency half-life remain authoritative.
    Day-of-week weighting is an additional multiplier only: samples from the same
    weekday as the target receive ``same_weekday_weight``; every other day keeps
    its normal recency weight. There is deliberately no weekday/weekend class.
    """
    enabled = bool(cfg.setting("day_of_week_weighting", True))
    same_weekday_weight = max(1.0, float(cfg.setting("same_weekday_weight", 1.5)))

    samples: list[tuple[float, float]] = []
    for row in store.baseline_samples(slot_key(target)):
        d = date.fromisoformat(row["day"])
        weight = recency_weight(d, target.date())
        if enabled and d.weekday() == target.date().weekday():
            weight *= same_weekday_weight
        samples.append((float(row["baseline_kwh"]), weight))

    result = weighted_winsorized_mean(samples)
    return max(min_slot_kwh, result if result is not None else min_slot_kwh)


def parse_forecast_list_attribute(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not state:
        return []
    attrs = state.get("attributes", {})
    for key in ("forecast", "detailedForecast"):
        val = attrs.get(key)
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                val = None
        if isinstance(val, list):
            return val
    return []


def parse_ashp_slots(state: dict[str, Any] | None, field: str) -> dict[datetime, float]:
    result: dict[datetime, float] = {}
    for item in parse_forecast_list_attribute(state):
        try:
            start = parse_dt(item.get("start") or item.get("period_start"))
            # ch_kwh/dhw_kwh are authoritative in ASHP Forecaster v0.2+.
            # energy_kwh remains a compatibility fallback for CH only.
            raw = item.get(field)
            if raw is None and field == "ch_kwh":
                raw = item.get("energy_kwh")
            if raw is None:
                continue
            value = float(raw)
            result[start.astimezone(timezone.utc)] = max(0.0, value)
        except (TypeError, ValueError):
            continue
    return result


def parse_solcast_slots(state: dict[str, Any] | None) -> dict[datetime, float]:
    # detailedForecast pv_estimate is average kW over a half-hour; convert to kWh.
    result: dict[datetime, float] = {}
    for item in parse_forecast_list_attribute(state):
        try:
            start = parse_dt(item.get("period_start") or item.get("start"))
            kw = float(item.get("pv_estimate"))
            result[start.astimezone(timezone.utc)] = max(0.0, kw * 0.5)
        except (TypeError, ValueError):
            continue
    return result


def interpolate_slot(mapping: dict[datetime, float], target: datetime) -> float | None:
    target = target.astimezone(timezone.utc)
    if target in mapping:
        return mapping[target]
    keys = sorted(mapping)
    before = max((k for k in keys if k < target), default=None)
    after = min((k for k in keys if k > target), default=None)
    if before is None or after is None:
        return None
    span = (after - before).total_seconds()
    if span <= 0:
        return None
    f = (target - before).total_seconds() / span
    return mapping[before] + f * (mapping[after] - mapping[before])


def parse_rates(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not state:
        return []
    attrs = state.get("attributes", {})
    rates = attrs.get("rates") or attrs.get("all_rates") or []
    if isinstance(rates, str):
        try:
            rates = json.loads(rates)
        except Exception:
            rates = []
    out: list[dict[str, Any]] = []
    for r in rates if isinstance(rates, list) else []:
        try:
            start = parse_dt(r.get("start") or r.get("valid_from") or r.get("from"))
            end_raw = r.get("end") or r.get("valid_to") or r.get("to")
            end = parse_dt(end_raw) if end_raw else start + timedelta(minutes=30)
            value = normalise_rate_p(r.get("value_inc_vat", r.get("value", r.get("rate"))))
            if value is None:
                continue
            out.append({"start": start, "end": end, "rate_p": value})
        except Exception:
            continue
    return out


def parse_dispatches(state: dict[str, Any] | None, attr: str) -> list[tuple[datetime, datetime]]:
    if not state:
        return []
    val = state.get("attributes", {}).get(attr, [])
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = []
    out: list[tuple[datetime, datetime]] = []
    for x in val if isinstance(val, list) else []:
        try:
            s = parse_dt(x.get("start") or x.get("start_time") or x.get("dispatch_start"))
            e = parse_dt(x.get("end") or x.get("end_time") or x.get("dispatch_end"))
            out.append((s, e))
        except Exception:
            continue
    return out


def interval_overlaps(a: datetime, b: datetime, x: datetime, y: datetime) -> bool:
    return a.astimezone(timezone.utc) < y.astimezone(timezone.utc) and x.astimezone(timezone.utc) < b.astimezone(timezone.utc)


def rate_at(rates: list[dict[str, Any]], start: datetime, end: datetime) -> float | None:
    for r in rates:
        if interval_overlaps(start, end, r["start"], r["end"]):
            return float(r["rate_p"])
    return None

def closest_rate_at(rates: list[dict[str, Any]], start: datetime, end: datetime) -> float | None:
    direct = rate_at(rates, start, end)
    if direct is not None:
        return direct
    if not rates:
        return None
    target = start.astimezone(timezone.utc)
    nearest = min(rates, key=lambda r: abs((r["start"].astimezone(timezone.utc) - target).total_seconds()))
    return float(nearest["rate_p"])


def find_overnight_blocks(import_rates: list[dict[str, Any]], tz: ZoneInfo) -> list[tuple[datetime, datetime, float]]:
    if not import_rates:
        return []
    min_rate = min(float(r["rate_p"]) for r in import_rates)
    lows = sorted([r for r in import_rates if abs(float(r["rate_p"]) - min_rate) < 0.001], key=lambda r: r["start"])
    blocks: list[tuple[datetime, datetime, float]] = []
    if not lows:
        return blocks
    cur_s, cur_e = lows[0]["start"], lows[0]["end"]
    for r in lows[1:]:
        if abs((r["start"].astimezone(timezone.utc) - cur_e.astimezone(timezone.utc)).total_seconds()) <= 1:
            cur_e = r["end"]
        else:
            local_s, local_e = cur_s.astimezone(tz), cur_e.astimezone(tz)
            if local_s.date() != local_e.date() or local_e.time() == dtime.min:
                blocks.append((cur_s, cur_e, min_rate))
            cur_s, cur_e = r["start"], r["end"]
    local_s, local_e = cur_s.astimezone(tz), cur_e.astimezone(tz)
    if local_s.date() != local_e.date() or local_e.time() == dtime.min:
        blocks.append((cur_s, cur_e, min_rate))
    return blocks


def select_controller_offpeak(import_rates: list[dict[str, Any]], now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime, float] | None:
    """Select the next lowest-rate morning block for controller use.

    Rules:
      * lowest import rate in the morning tariff window
      * adjacent equal-rate segments are merged
      * block may start before or after midnight
      * block must end no later than 12:00 local
      * if several lowest-rate blocks qualify, choose the one ending latest
      * if already inside a block, skip it and select the following morning's block
    """
    if not import_rates:
        return None
    rates = sorted(import_rates, key=lambda r: r["start"].astimezone(timezone.utc))
    # Search today and the following two local mornings. The forecast normally
    # carries today+tomorrow rates, but the extra day keeps copied-rate fallbacks harmless.
    for offset in range(0, 3):
        day = now.astimezone(tz).date() + timedelta(days=offset)
        midnight = datetime.combine(day, dtime.min, tzinfo=tz)
        noon = datetime.combine(day, dtime(12, 0), tzinfo=tz)

        morning = [
            r for r in rates
            if r["start"].astimezone(timezone.utc) < noon.astimezone(timezone.utc)
            and r["end"].astimezone(timezone.utc) > midnight.astimezone(timezone.utc)
        ]
        if not morning:
            continue

        min_rate = min(float(r["rate_p"]) for r in morning)
        lows = [
            r for r in rates
            if abs(float(r["rate_p"]) - min_rate) < 0.001
            and r["start"].astimezone(timezone.utc) < noon.astimezone(timezone.utc)
            and r["end"].astimezone(timezone.utc) > (midnight - timedelta(hours=12)).astimezone(timezone.utc)
        ]
        lows.sort(key=lambda r: r["start"].astimezone(timezone.utc))
        blocks: list[list[datetime]] = []
        for r in lows:
            rs, re = r["start"], r["end"]
            if not blocks or abs((rs.astimezone(timezone.utc) - blocks[-1][1].astimezone(timezone.utc)).total_seconds()) > 1:
                blocks.append([rs, re])
            else:
                blocks[-1][1] = max(blocks[-1][1], re, key=lambda x: x.astimezone(timezone.utc))

        qualifying: list[tuple[datetime, datetime, float]] = []
        for bs, be in blocks:
            local_end = be.astimezone(tz)
            if be.astimezone(timezone.utc) <= midnight.astimezone(timezone.utc):
                continue
            if local_end.date() != day or local_end.time() > dtime(12, 0):
                continue
            # Preserve existing forecast-state semantics: once the selected
            # overnight has started, target the following night's block.
            if bs.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                continue
            qualifying.append((bs, be, min_rate))
        if qualifying:
            return max(qualifying, key=lambda x: x[1].astimezone(timezone.utc))
    return None


def active_pause_mode(dt: datetime, batt: dict[str, Any]) -> str:
    mode = str(batt.get("pause_mode", "Disabled"))
    if mode == "Disabled":
        return "Disabled"
    if in_daily_window(dt, str(batt.get("pause_start", "00:00")), str(batt.get("pause_end", "00:00"))):
        return mode
    return "Disabled"


def pause_blocks_charge(dt: datetime, batt: dict[str, Any]) -> bool:
    return active_pause_mode(dt, batt) in {"PauseCharge", "PauseBoth"}


def pause_blocks_discharge(dt: datetime, batt: dict[str, Any]) -> bool:
    return active_pause_mode(dt, batt) in {"PauseDischarge", "PauseBoth"}


def battery_mode_for_slot(start: datetime, batt: dict[str, Any], ignore_forced_slots: bool = False) -> str:
    if not ignore_forced_slots:
        pause = active_pause_mode(start, batt)
        if pause == "PauseBoth":
            return "pause_both"
        if pause == "PauseCharge":
            return "pause_charge"
        if pause == "PauseDischarge":
            return "pause_discharge"
        if active_target(start, batt["charge_enabled"], batt["charge_slots"]) is not None:
            return "forced_charge"
        if active_target(start, batt["discharge_enabled"], batt["discharge_slots"]) is not None:
            return "forced_discharge"
    return "eco" if batt["eco"] else "idle"


def battery_without_forced_slots(batt: dict[str, Any]) -> dict[str, Any]:
    copy = dict(batt)
    copy["charge_enabled"] = False
    copy["discharge_enabled"] = False
    copy["charge_slots"] = []
    copy["discharge_slots"] = []
    copy["pause_mode"] = "Disabled"
    copy["pause_start"] = "00:00:00"
    copy["pause_end"] = "00:00:00"
    return copy


# ---------- Live history and recent correction ----------

def recent_baseline_multiplier(client: HAClient, store: Store, cfg: Config, now: datetime) -> float:
    load_e = cfg.section("load")["energy_total_kwh"]
    ch_e = cfg.section("ashp")["ch_energy_total_kwh"]
    dhw_e = cfg.section("ashp")["dhw_energy_total_kwh"]
    load_cfg = cfg.section("load")
    ev_included = bool(load_cfg.get("ev_included_in_battery_load", False))
    ev_e = str(load_cfg.get("ev_charging_entity") or "").strip()
    start = now - timedelta(hours=RECENT_LOOKBACK_HOURS + 1)
    entities = [load_e, ch_e, dhw_e]
    if ev_included and ev_e:
        entities.append(ev_e)
    hist = client.history(entities, start, now, timeout=45)
    floor = float(cfg.setting("minimum_baseline_w", 200)) / 1000 * 0.5
    now_floor = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    boundaries = [now_floor - timedelta(minutes=30 * i) for i in range(4, -1, -1)]
    actual_sum = expected_sum = 0.0
    valid = 0
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        if ev_included:
            if not ev_e:
                continue
            ev_state = ev_active_during(hist.get(ev_e, []), a, b)
            if ev_state is not False:
                continue
        load = diff_cumulative(hist.get(load_e, []), a, b)
        if load is None:
            continue
        ch = diff_cumulative(hist.get(ch_e, []), a, b) or 0.0
        dhw = diff_cumulative(hist.get(dhw_e, []), a, b) or 0.0
        actual = max(floor, load - ch - dhw)
        expected = baseline_for(store, cfg, a, floor)
        actual_sum += actual
        expected_sum += expected
        valid += 1
    if valid < 2 or expected_sum <= 0:
        return 1.0
    return clamp(actual_sum / expected_sum, RECENT_MULTIPLIER_MIN, RECENT_MULTIPLIER_MAX)


# ---------- Battery simulation ----------

def parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse Home Assistant time states in HH:MM or HH:MM:SS format."""
    try:
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except (TypeError, ValueError):
        return None


def in_daily_window(dt: datetime, start_s: str, end_s: str) -> bool:
    if start_s == end_s:
        return False
    a = parse_hhmm(start_s)
    b = parse_hhmm(end_s)
    if not a or not b:
        return False
    mins = dt.hour * 60 + dt.minute
    sm = a[0] * 60 + a[1]
    em = b[0] * 60 + b[1]
    if sm < em:
        return sm <= mins < em
    return mins >= sm or mins < em


def battery_config(client: HAClient, cfg: Config, reasons: list[str]) -> dict[str, Any]:
    b = cfg.section("battery")
    keys = [
        "soc", "capacity_kwh", "reserve_soc", "inverter_max_rate_w", "charge_rate_w", "discharge_rate_w",
        "eco_mode", "charge_schedule_enabled", "discharge_schedule_enabled",
        "pause_mode", "pause_start", "pause_end",
        "charge_start_1", "charge_end_1", "charge_target_1", "charge_start_2", "charge_end_2", "charge_target_2",
        "discharge_start_1", "discharge_end_1", "discharge_target_1", "discharge_start_2", "discharge_end_2", "discharge_target_2",
    ]
    raw: dict[str, dict[str, Any] | None] = {k: client.state_optional(b.get(k, "")) for k in keys}

    def state_text(key: str) -> str:
        st = raw.get(key)
        return "<unavailable>" if st is None else str(st.get("state", "<missing>"))

    def state_age_minutes(key: str) -> float | None:
        st = raw.get(key)
        if not st:
            return None
        stamp = st.get("last_updated") or st.get("last_changed")
        if not stamp:
            return None
        try:
            return max(0.0, (datetime.now(timezone.utc) - parse_dt(stamp).astimezone(timezone.utc)).total_seconds() / 60.0)
        except Exception:
            return None

    if LOG.isEnabledFor(logging.DEBUG):
        for k in keys:
            age = state_age_minutes(k)
            LOG.debug("battery raw %-28s entity=%s state=%r age_min=%s", k, b.get(k), state_text(k), f"{age:.1f}" if age is not None else "?")

    soc = numeric_state(raw["soc"])
    cap = numeric_state(raw["capacity_kwh"])
    max_rate = numeric_state(raw["inverter_max_rate_w"])
    if soc is None or cap is None or max_rate is None:
        missing = [name for name, v in (("battery SOC", soc), ("battery capacity", cap), ("inverter max battery rate", max_rate)) if v is None]
        raise HAError("Critical battery input unavailable: " + ", ".join(missing))
    reserve = numeric_state(raw["reserve_soc"], FALLBACK_RESERVE_SOC)
    if raw["reserve_soc"] is None:
        reasons.append(f"Battery reserve unavailable: using {FALLBACK_RESERVE_SOC:.0f}% fallback")
    charge_rate = numeric_state(raw["charge_rate_w"])
    discharge_rate = numeric_state(raw["discharge_rate_w"])
    if charge_rate is None:
        charge_rate = max_rate
        reasons.append("Battery charge rate unavailable: using inverter maximum")
    if discharge_rate is None:
        discharge_rate = max_rate
        reasons.append("Battery discharge rate unavailable: using inverter maximum")
    eco_state = raw["eco_mode"]
    eco = bool_state(eco_state, True)
    if eco_state is None:
        reasons.append("Eco mode unavailable: assuming on")
    charge_enabled_state = raw["charge_schedule_enabled"]
    discharge_enabled_state = raw["discharge_schedule_enabled"]
    charge_enabled = bool_state(charge_enabled_state, False)
    discharge_enabled = bool_state(discharge_enabled_state, False)
    if charge_enabled_state is None:
        reasons.append("Charge schedule enable unavailable: scheduled charge ignored")
    if discharge_enabled_state is None:
        reasons.append("Discharge schedule enable unavailable: scheduled discharge ignored")

    pause_state = raw.get("pause_mode")
    pause_mode = str(pause_state.get("state", "Disabled")) if pause_state else "Disabled"
    valid_pause_modes = {"Disabled", "PauseCharge", "PauseDischarge", "PauseBoth"}
    if pause_mode not in valid_pause_modes:
        reasons.append(f"Battery pause mode {pause_mode!r} invalid: treating as Disabled")
        pause_mode = "Disabled"
    if pause_state is None:
        reasons.append("Battery pause mode unavailable: treating as Disabled")

    missing_schedule_fields: set[str] = set()
    def get_state_str(key: str) -> str:
        st = raw.get(key)
        if st is None:
            missing_schedule_fields.add(key)
            return "00:00"
        return str(st.get("state", "00:00"))

    def get_target(key: str, default: float) -> float:
        st = raw.get(key)
        v = numeric_state(st)
        if st is None:
            missing_schedule_fields.add(key)
        return default if v is None else v

    result = {
        "soc": soc, "capacity": cap, "reserve": reserve or FALLBACK_RESERVE_SOC,
        "max_rate_w": max_rate, "charge_rate_w": min(charge_rate, max_rate), "discharge_rate_w": min(discharge_rate, max_rate),
        "eco": eco, "charge_enabled": charge_enabled, "discharge_enabled": discharge_enabled,
        "pause_mode": pause_mode,
        "pause_start": get_state_str("pause_start"),
        "pause_end": get_state_str("pause_end"),
        "charge_slots": [
            (get_state_str("charge_start_1"), get_state_str("charge_end_1"), get_target("charge_target_1", 100.0)),
            (get_state_str("charge_start_2"), get_state_str("charge_end_2"), get_target("charge_target_2", 100.0)),
        ],
        "discharge_slots": [
            (get_state_str("discharge_start_1"), get_state_str("discharge_end_1"), get_target("discharge_target_1", reserve or FALLBACK_RESERVE_SOC)),
            (get_state_str("discharge_start_2"), get_state_str("discharge_end_2"), get_target("discharge_target_2", reserve or FALLBACK_RESERVE_SOC)),
        ],
    }
    if missing_schedule_fields and (charge_enabled or discharge_enabled or pause_mode != "Disabled"):
        reasons.append("Battery schedule/pause fields unavailable: affected windows treated with safe defaults")

    LOG.info(
        "Battery inputs: soc=%.1f%% capacity=%.2fkWh reserve=%.1f%% eco_raw=%r eco=%s charge_enable_raw=%r charge_enabled=%s discharge_enable_raw=%r discharge_enabled=%s pause_mode=%s pause=%s-%s charge_rate=%.0fW discharge_rate=%.0fW charge_slots=%s discharge_slots=%s",
        soc, cap, result["reserve"], state_text("eco_mode"), eco,
        state_text("charge_schedule_enabled"), charge_enabled,
        state_text("discharge_schedule_enabled"), discharge_enabled,
        result["pause_mode"], result["pause_start"], result["pause_end"],
        result["charge_rate_w"], result["discharge_rate_w"], result["charge_slots"], result["discharge_slots"]
    )
    if eco_state is not None and state_text("eco_mode").lower() in ("on", "off") and eco != (state_text("eco_mode").lower() == "on"):
        LOG.warning("Battery control mismatch: raw eco=%r parsed=%s", state_text("eco_mode"), eco)
    if charge_enabled_state is not None and state_text("charge_schedule_enabled").lower() in ("on", "off") and charge_enabled != (state_text("charge_schedule_enabled").lower() == "on"):
        LOG.warning("Battery control mismatch: raw charge schedule=%r parsed=%s", state_text("charge_schedule_enabled"), charge_enabled)
    if discharge_enabled_state is not None and state_text("discharge_schedule_enabled").lower() in ("on", "off") and discharge_enabled != (state_text("discharge_schedule_enabled").lower() == "on"):
        LOG.warning("Battery control mismatch: raw discharge schedule=%r parsed=%s", state_text("discharge_schedule_enabled"), discharge_enabled)
    return result


def active_target(dt: datetime, enabled: bool, slots: list[tuple[str, str, float]]) -> float | None:
    if not enabled:
        return None
    for s, e, target in slots:
        if in_daily_window(dt, s, e):
            return target
    return None



def battery_control_boundaries(slot_start: datetime, slot_end: datetime, batt: dict[str, Any]) -> list[datetime]:
    """Return all configured control transitions strictly inside a forecast slot.

    Schedule and pause entities are daily wall-clock controls.  Splitting a
    30-minute forecast slot at every start/end transition lets each portion be
    simulated with the correct forced/paused/Eco behaviour.
    """
    time_values: list[str] = []
    for start_s, end_s, _target in batt.get("charge_slots", []):
        time_values.extend((start_s, end_s))
    for start_s, end_s, _target in batt.get("discharge_slots", []):
        time_values.extend((start_s, end_s))
    if str(batt.get("pause_mode", "Disabled")) != "Disabled":
        time_values.extend((str(batt.get("pause_start", "00:00")), str(batt.get("pause_end", "00:00"))))

    parsed = [parse_hhmm(v) for v in time_values]
    parsed = [v for v in parsed if v is not None]
    if not parsed:
        return []

    # Include adjacent local dates so cross-midnight windows are represented.
    local_start = slot_start
    local_end = slot_end
    candidates: list[datetime] = []
    for day_offset in (-1, 0, 1):
        day = local_start.date() + timedelta(days=day_offset)
        for hour, minute in parsed:
            point = datetime.combine(day, dtime(hour, minute), tzinfo=local_start.tzinfo)
            if slot_start.astimezone(timezone.utc) < point.astimezone(timezone.utc) < slot_end.astimezone(timezone.utc):
                candidates.append(point)

    # UTC ordering avoids ambiguity around DST folds.
    unique = {x.astimezone(timezone.utc): x for x in candidates}
    return [unique[k] for k in sorted(unique)]


def simulate_battery_fractional(
    slot: dict[str, Any],
    batt: dict[str, Any],
    soc_kwh: float,
    charge_eff: float,
    discharge_eff: float,
) -> tuple[float, float, float, float, str, float | None, float | None]:
    """Simulate one output slot, splitting it at control transitions.

    Load and PV are apportioned by elapsed time within the original forecast
    slot.  This makes a 7-minute forced-discharge tail consume only 7 minutes
    of forced-discharge energy, with the remaining 23 minutes reverting to Eco
    (or idle/pause as configured).
    """
    slot_start = slot["start_dt"]
    slot_end = (slot_start.astimezone(timezone.utc) + timedelta(hours=slot["duration_h"])).astimezone(slot_start.tzinfo)
    boundaries = [slot_start, *battery_control_boundaries(slot_start, slot_end, batt), slot_end]

    total_batt = total_import = total_export = 0.0
    modes: list[tuple[str, float]] = []
    charge_targets: list[float] = []
    discharge_targets: list[float] = []
    total_seconds = max(1e-9, (slot_end.astimezone(timezone.utc) - slot_start.astimezone(timezone.utc)).total_seconds())

    for a, b in zip(boundaries[:-1], boundaries[1:]):
        seconds = (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds()
        if seconds <= 0:
            continue
        frac = seconds / total_seconds
        subslot = {
            "start_dt": a,
            "duration_h": seconds / 3600.0,
            "baseline_kwh": slot.get("baseline_kwh", 0.0) * frac,
            "ch_kwh": slot.get("ch_kwh", 0.0) * frac,
            "dhw_kwh": slot.get("dhw_kwh", 0.0) * frac,
            "load_kwh": slot["load_kwh"] * frac,
            "pv_kwh": slot["pv_kwh"] * frac,
        }

        mode = battery_mode_for_slot(a, batt)
        charge_target = active_target(a, batt["charge_enabled"], batt["charge_slots"])
        discharge_target = active_target(a, batt["discharge_enabled"], batt["discharge_slots"])
        if charge_target is not None:
            charge_targets.append(charge_target)
        if discharge_target is not None:
            discharge_targets.append(discharge_target)

        before = soc_kwh
        soc_kwh, batt_kwh, imp, exp = simulate_battery(subslot, batt, soc_kwh, charge_eff, discharge_eff)

        # Preserve the explicit BMS-idle modes within a scheduled segment.
        if mode == "forced_discharge" and discharge_target is not None and abs(batt_kwh) <= 0.0005:
            floor_pct = max(batt["reserve"], clamp(discharge_target, 0, 100))
            if before <= batt["capacity"] * floor_pct / 100.0 + 1e-6:
                mode = "forced_discharge_idle_reserve" if abs(floor_pct - batt["reserve"]) < 0.01 else "forced_discharge_idle_target"
        elif mode == "forced_charge" and charge_target is not None and abs(batt_kwh) <= 0.0005:
            target_kwh = batt["capacity"] * clamp(charge_target, 0, 100) / 100.0
            if before >= target_kwh - 1e-6:
                mode = "forced_charge_idle_target"

        total_batt += batt_kwh
        total_import += imp
        total_export += exp
        modes.append((mode, seconds))

    # Keep existing mode names for whole-slot behaviour.  Mark mixed slots
    # explicitly so diagnostics/cards can see that the slot is fractional.
    nonzero_modes = [m for m, sec in modes if sec > 0]
    unique_modes = list(dict.fromkeys(nonzero_modes))
    if len(unique_modes) == 1:
        display_mode = unique_modes[0]
    elif any(m.startswith("forced_charge") for m in unique_modes):
        display_mode = "forced_charge_partial"
    elif any(m.startswith("forced_discharge") for m in unique_modes):
        display_mode = "forced_discharge_partial"
    elif any(m.startswith("pause_") for m in unique_modes):
        display_mode = "pause_partial"
    else:
        display_mode = unique_modes[0] if unique_modes else ("eco" if batt["eco"] else "idle")

    return (
        soc_kwh,
        total_batt,
        total_import,
        total_export,
        display_mode,
        charge_targets[-1] if charge_targets else None,
        discharge_targets[-1] if discharge_targets else None,
    )


def simulate_battery(slot: dict[str, Any], batt: dict[str, Any], soc_kwh: float, charge_eff: float, discharge_eff: float) -> tuple[float, float, float, float]:
    """Return new_soc_kwh, signed battery_kwh (AC side; + discharge), import_kwh, export_kwh."""
    load = slot["load_kwh"]
    pv = slot["pv_kwh"]
    hours = slot["duration_h"]
    reserve_kwh = batt["capacity"] * batt["reserve"] / 100.0
    # Scheduled charge/discharge rates are explicit slot controls. Eco mode is
    # constrained by the inverter's actual battery capability instead, because
    # the slot-rate entities may legitimately be set to 0 W while Eco remains active.
    slot_max_kwh_charge = batt["charge_rate_w"] / 1000.0 * hours
    slot_max_kwh_discharge = batt["discharge_rate_w"] / 1000.0 * hours
    eco_max_kwh = batt["max_rate_w"] / 1000.0 * hours
    charge_target = active_target(slot["start_dt"], batt["charge_enabled"], batt["charge_slots"])
    discharge_target = active_target(slot["start_dt"], batt["discharge_enabled"], batt["discharge_slots"])
    charge_paused = pause_blocks_charge(slot["start_dt"], batt)
    discharge_paused = pause_blocks_discharge(slot["start_dt"], batt)

    battery_ac = 0.0
    if charge_target is not None:  # charge priority
        if charge_paused:
            soc_kwh = clamp(soc_kwh, reserve_kwh, batt["capacity"])
            grid_net = load - pv
            return soc_kwh, 0.0, max(0.0, grid_net), max(0.0, -grid_net)
        target_kwh = batt["capacity"] * clamp(charge_target, 0, 100) / 100.0
        room_stored = max(0.0, target_kwh - soc_kwh)
        ac_needed = room_stored / charge_eff if charge_eff > 0 else 0.0
        ac_charge = min(slot_max_kwh_charge, ac_needed)
        soc_kwh += ac_charge * charge_eff
        battery_ac = -ac_charge
    elif discharge_target is not None:
        if discharge_paused:
            soc_kwh = clamp(soc_kwh, reserve_kwh, batt["capacity"])
            grid_net = load - pv
            return soc_kwh, 0.0, max(0.0, grid_net), max(0.0, -grid_net)
        floor_pct = max(batt["reserve"], clamp(discharge_target, 0, 100))
        floor_kwh = batt["capacity"] * floor_pct / 100.0
        stored_avail = max(0.0, soc_kwh - floor_kwh)
        ac_avail = stored_avail * discharge_eff
        ac_discharge = min(slot_max_kwh_discharge, ac_avail)
        soc_kwh -= ac_discharge / discharge_eff if discharge_eff > 0 else 0.0
        battery_ac = ac_discharge
    elif batt["eco"]:
        net_surplus = pv - load
        if net_surplus > 0 and not charge_paused:
            room_stored = max(0.0, batt["capacity"] - soc_kwh)
            ac_needed = room_stored / charge_eff if charge_eff > 0 else 0.0
            ac_charge = min(net_surplus, eco_max_kwh, ac_needed)
            soc_kwh += ac_charge * charge_eff
            battery_ac = -ac_charge
        elif net_surplus < 0 and not discharge_paused:
            deficit = -net_surplus
            stored_avail = max(0.0, soc_kwh - reserve_kwh)
            ac_avail = stored_avail * discharge_eff
            ac_discharge = min(deficit, eco_max_kwh, ac_avail)
            soc_kwh -= ac_discharge / discharge_eff if discharge_eff > 0 else 0.0
            battery_ac = ac_discharge

    soc_kwh = clamp(soc_kwh, reserve_kwh, batt["capacity"])
    grid_net = load - pv - battery_ac  # + import, - export; charging battery_ac negative adds load.
    import_kwh = max(0.0, grid_net)
    export_kwh = max(0.0, -grid_net)
    return soc_kwh, battery_ac, import_kwh, export_kwh


# ---------- Actual-day summary ----------

def actual_today(client: HAClient, cfg: Config, now: datetime, import_rates: list[dict[str, Any]], export_rates: list[dict[str, Any]], completed_dispatches: list[tuple[datetime, datetime]], cheap_rate_p: float | None, imp_e: str, exp_e: str) -> tuple[dict[str, float], dict[str, Any]]:
    load_e = cfg.section("load")["energy_total_kwh"]
    pv_e = cfg.section("solar")["energy_total_kwh"]
    ch_e = cfg.section("ashp")["ch_energy_total_kwh"]
    dhw_e = cfg.section("ashp")["dhw_energy_total_kwh"]
    soc_e = cfg.section("battery")["soc"]
    midnight, _ = day_bounds(now.date(), now.tzinfo)  # type: ignore[arg-type]
    hist = client.history([load_e, pv_e, ch_e, dhw_e, imp_e, exp_e, soc_e], midnight - timedelta(hours=2), now, timeout=60)

    def total(entity: str) -> float:
        v = diff_cumulative(hist.get(entity, []), midnight, now)
        return max(0.0, v or 0.0)

    totals = {
        "load_kwh": total(load_e), "ch_kwh": total(ch_e), "dhw_kwh": total(dhw_e), "pv_kwh": total(pv_e),
        "import_kwh": total(imp_e), "export_kwh": total(exp_e),
        "import_cost_p": 0.0, "export_income_p": 0.0, "cost_p": 0.0,
    }
    boundaries = real_half_hours(midnight, now.replace(second=0, microsecond=0))
    if not boundaries or boundaries[-1] < now:
        boundaries.append(now)
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        imp = diff_cumulative(hist.get(imp_e, []), a, b)
        exp = diff_cumulative(hist.get(exp_e, []), a, b)
        if imp is None and exp is None:
            continue
        ir = closest_rate_at(import_rates, a, b)
        er = closest_rate_at(export_rates, a, b)
        if any(interval_overlaps(a, b, ds, de) for ds, de in completed_dispatches) and cheap_rate_p is not None:
            ir = cheap_rate_p
        if ir is not None and imp is not None:
            totals["import_cost_p"] += imp * ir
        if er is not None and exp is not None:
            totals["export_income_p"] += exp * er
    totals["cost_p"] = totals["import_cost_p"] - totals["export_income_p"]

    soc_vals: list[tuple[float, datetime]] = []
    for item in hist.get(soc_e, []):
        try:
            soc_vals.append((float(item["state"]), parse_dt(item.get("last_changed") or item.get("last_updated")).astimezone(now.tzinfo)))
        except Exception:
            continue
    if soc_vals:
        mn = min(soc_vals, key=lambda x: x[0]); mx = max(soc_vals, key=lambda x: x[0])
        extremes = {"min_soc": {"value": int(round(mn[0])), "time": mn[1].isoformat()}, "max_soc": {"value": int(round(mx[0])), "time": mx[1].isoformat()}}
    else:
        extremes = {}
    return totals, extremes


# ---------- Main forecast ----------

def make_forecast(client: HAClient, store: Store, cfg: Config, now: datetime) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    # Critical baseline source check.
    if client.state_optional(cfg.section("load")["energy_total_kwh"]) is None:
        raise HAError("Critical whole-house cumulative load energy unavailable")

    imp_e, exp_e, import_meter_source, export_meter_source = effective_grid_energy_entities(client, cfg)
    imp_state = client.state_optional(imp_e) if imp_e else None
    exp_state = client.state_optional(exp_e) if exp_e else None
    if not imp_e or numeric_state(imp_state) is None:
        raise HAError(f"Configured cumulative grid import energy entity is unavailable/non-numeric: {imp_e or '<none>'} (source={import_meter_source})")
    if not exp_e or numeric_state(exp_state) is None:
        raise HAError(f"Configured cumulative grid export energy entity is unavailable/non-numeric: {exp_e or '<none>'} (source={export_meter_source})")
    if import_meter_source != "true_meter" or export_meter_source != "true_meter":
        reasons.append(
            "True meter import/export not fully available: using inverter/battery cumulative energy fallback where required"
        )

    batt = battery_config(client, cfg, reasons)
    charge_eff = float(cfg.setting("charge_efficiency", 0.95))
    discharge_eff = float(cfg.setting("discharge_efficiency", 0.95))
    min_full_slot_kwh = float(cfg.setting("minimum_baseline_w", 200)) / 1000.0 * 0.5

    # Forecast slots: partial current half-hour, then full real half-hours to end of tomorrow.
    current_floor = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    next_boundary = current_floor + timedelta(minutes=30)
    horizon_end = datetime.combine(now.date() + timedelta(days=2), dtime.min, tzinfo=tz)
    starts = [now]
    cursor = next_boundary
    while cursor < horizon_end:
        starts.append(cursor)
        cursor = (cursor.astimezone(timezone.utc) + timedelta(minutes=30)).astimezone(tz)
    durations_h = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else horizon_end
        durations_h.append((e.astimezone(timezone.utc) - s.astimezone(timezone.utc)).total_seconds() / 3600)

    # Baseline model + recent correction.
    recent_mult = 1.0
    try:
        recent_mult = recent_baseline_multiplier(client, store, cfg, now)
    except Exception as exc:
        reasons.append(f"Recent-load correction unavailable: using 1.0x ({exc})")
    baseline: list[float] = []
    for s, dh in zip(starts, durations_h):
        full = baseline_for(store, cfg, s, min_full_slot_kwh)
        elapsed_h = max(0.0, (s.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() / 3600)
        fade = max(0.0, 1.0 - elapsed_h / RECENT_FADE_HOURS)
        mult = 1.0 + (recent_mult - 1.0) * fade
        value = full * mult * (dh / 0.5)
        baseline.append(max(float(cfg.setting("minimum_baseline_w", 200)) / 1000.0 * dh, value))

    # ASHP forecast owns both CH and DHW. Fresh detailed slots are authoritative;
    # yesterday's actual profile is retained only as a resilience fallback.
    ashp_state = client.state_optional(cfg.section("ashp")["forecast_48h"])
    fresh_ashp = state_age_hours(ashp_state, now) <= ASHP_STALE_HOURS
    ch_map = parse_ashp_slots(ashp_state, "ch_kwh") if fresh_ashp else {}
    dhw_map = parse_ashp_slots(ashp_state, "dhw_kwh") if fresh_ashp else {}
    if ashp_state is None or not ch_map:
        reasons.append("ASHP CH forecast unavailable/stale: using yesterday CH fallback where available")
    if ashp_state is None or not dhw_map:
        reasons.append("ASHP DHW forecast unavailable/stale: using yesterday DHW fallback where available")
    ch_values: list[float] = []
    dhw_values: list[float] = []
    for s, dh in zip(starts, durations_h):
        key = current_floor.astimezone(timezone.utc) if s == now else s.astimezone(timezone.utc)
        ch = ch_map.get(key)
        dhw = dhw_map.get(key)
        if ch is None:
            ch = store.fallback(s.date() - timedelta(days=1), "ch", slot_key(s), getattr(s, "fold", 0)) or 0.0
        if dhw is None:
            dhw = store.fallback(s.date() - timedelta(days=1), "dhw", slot_key(s), getattr(s, "fold", 0)) or 0.0
        ch_values.append(max(0.0, ch * (dh / 0.5)))
        dhw_values.append(max(0.0, dhw * (dh / 0.5)))

    # Solcast: today owns today, tomorrow owns tomorrow; day3 only gap filler. Detailed values are kW -> kWh.
    solar_sec = cfg.section("solar")
    solcast_states = {
        "today": client.state_optional(solar_sec["solcast_today"]),
        "tomorrow": client.state_optional(solar_sec["solcast_tomorrow"]),
        "day3": client.state_optional(solar_sec.get("solcast_day_3", "")),
    }
    solar_maps = {k: parse_solcast_slots(v) if state_age_hours(v, now) <= SOLCAST_STALE_HOURS else {} for k, v in solcast_states.items()}
    if not solar_maps["today"] or not solar_maps["tomorrow"]:
        reasons.append("Solcast forecast unavailable/stale or incomplete: using interpolation/yesterday PV fallback where needed")
    pv_values: list[float] = []
    for s, dh in zip(starts, durations_h):
        owner = "today" if s.date() == now.date() else "tomorrow"
        target = current_floor.astimezone(timezone.utc) if s == now else s.astimezone(timezone.utc)
        val = interpolate_slot(solar_maps[owner], target)
        if val is None and solar_maps["day3"]:
            val = interpolate_slot(solar_maps["day3"], target)
        if val is None:
            val = store.fallback(s.date() - timedelta(days=1), "pv", slot_key(s), getattr(s, "fold", 0))
        if val is None:
            val = 0.0
        pv_values.append(max(0.0, val * (dh / 0.5)))

    # Tariffs.
    t = cfg.section("tariff")
    import_current_day = client.state_optional(t["import_current_day_rates"])
    import_next_day = client.state_optional(t["import_next_day_rates"])
    export_current_day = client.state_optional(t["export_current_day_rates"])
    export_next_day = client.state_optional(t["export_next_day_rates"])
    import_rates = parse_rates(import_current_day) + parse_rates(import_next_day)
    export_rates = parse_rates(export_current_day) + parse_rates(export_next_day)
    tariff_fallback = False
    if not import_rates:
        cur = normalise_rate_p(numeric_state(client.state_optional(t["import_current_rate"])))
        if cur is not None:
            import_rates = [{"start": datetime.combine(now.date(), dtime.min, tzinfo=tz), "end": horizon_end, "rate_p": cur}]
            tariff_fallback = True
            reasons.append("Import rate events unavailable: current import rate applied to forecast")
        else:
            reasons.append("Import tariff unavailable: import cost outputs incomplete")
    if not export_rates:
        cur = normalise_rate_p(numeric_state(client.state_optional(t["export_current_rate"])))
        if cur is not None:
            export_rates = [{"start": datetime.combine(now.date(), dtime.min, tzinfo=tz), "end": horizon_end, "rate_p": cur}]
            tariff_fallback = True
            reasons.append("Export rate events unavailable: current export rate applied to forecast")
        else:
            reasons.append("Export tariff unavailable: export income outputs incomplete")

    # If next-day tariff entity is missing, carry today's 24h tariff pattern forward for tomorrow.
    if import_rates and not parse_rates(import_next_day):
        today_only = parse_rates(import_current_day)
        copied = [{"start": r["start"] + timedelta(days=1), "end": r["end"] + timedelta(days=1), "rate_p": r["rate_p"]} for r in today_only]
        import_rates.extend(copied)
        tariff_fallback = True
        reasons.append("Tomorrow import tariff unavailable: today pattern carried forward")
    if export_rates and not parse_rates(export_next_day):
        today_only = parse_rates(export_current_day)
        copied = [{"start": r["start"] + timedelta(days=1), "end": r["end"] + timedelta(days=1), "rate_p": r["rate_p"]} for r in today_only]
        export_rates.extend(copied)
        tariff_fallback = True
        reasons.append("Tomorrow export tariff unavailable: today pattern carried forward")

    overnight_blocks = find_overnight_blocks(import_rates, tz)
    overnight_blocks.sort(key=lambda x: x[0])
    selected_offpeak = select_controller_offpeak(import_rates, now, tz)
    cheap_rate_p = selected_offpeak[2] if selected_offpeak else (min((r["rate_p"] for r in import_rates), default=None))
    if selected_offpeak:
        target_start = selected_offpeak[0]
        target_end = selected_offpeak[1]
    else:
        # Preserve the existing degraded fallback if tariff events are not rich
        # enough to identify the controller-facing morning block.
        target_day = now.date() if now.time() < dtime(23, 30) else now.date() + timedelta(days=1)
        target_start = datetime.combine(target_day, dtime(23, 30), tzinfo=tz)
        target_end = target_start + timedelta(hours=6)
        reasons.append("Regular overnight cheap block could not be identified reliably: 23:30-05:30 fallback used")

    dispatch_state = client.state_optional(t["intelligent_dispatching"])
    planned = parse_dispatches(dispatch_state, "planned_dispatches")
    completed = parse_dispatches(dispatch_state, "completed_dispatches")
    if dispatch_state is None:
        reasons.append("Intelligent dispatch entity unavailable: dynamic cheap slots ignored")

    # Simulate both scenarios from the same world inputs:
    #   forecast          = current programmed inverter slots included
    #   forecast_no_slots = all forced charge/discharge slots ignored; Eco remains active
    soc_kwh = batt["capacity"] * batt["soc"] / 100.0
    no_slots_batt = battery_without_forced_slots(batt)
    no_slots_soc_kwh = soc_kwh
    output_slots: list[dict[str, Any]] = []
    no_slots_output: list[dict[str, Any]] = []
    forecast_soc_points: list[dict[str, Any]] = []
    no_slots_soc_points: list[dict[str, Any]] = []
    overnight_start_soc: int | None = None
    overnight_start_soc_no_slots: int | None = None

    for s, dh, base, ch, dhw, pv in zip(starts, durations_h, baseline, ch_values, dhw_values, pv_values):
        e = (s.astimezone(timezone.utc) + timedelta(hours=dh)).astimezone(tz)
        ir = rate_at(import_rates, s, e)
        er = rate_at(export_rates, s, e)
        if any(interval_overlaps(s, e, ds, de) for ds, de in planned) and cheap_rate_p is not None:
            ir = cheap_rate_p

        slot = {
            "start_dt": s, "duration_h": dh, "baseline_kwh": base,
            "ch_kwh": ch, "dhw_kwh": dhw,
            "load_kwh": base + ch + dhw, "pv_kwh": pv,
        }

        # Current-programmed scenario.
        pre_soc_pct = int(round(100 * soc_kwh / batt["capacity"]))
        forecast_soc_points.append({"time": s.isoformat(), "soc": pre_soc_pct})
        if overnight_start_soc is None and s.astimezone(timezone.utc) >= target_start.astimezone(timezone.utc):
            overnight_start_soc = pre_soc_pct
        soc_before_kwh = soc_kwh
        soc_kwh, batt_kwh, imp, exp, mode, charge_target, discharge_target = simulate_battery_fractional(
            slot, batt, soc_kwh, charge_eff, discharge_eff
        )
        soc_pct = int(round(100 * soc_kwh / batt["capacity"]))
        import_cost = imp * ir if ir is not None else 0.0
        export_income = exp * er if er is not None else 0.0
        out = {
            "start": s.isoformat(),
            "baseline_kwh": round(base, 2), "ch_kwh": round(ch, 2), "dhw_kwh": round(dhw, 2),
            "load_kwh": round(base + ch + dhw, 2), "pv_kwh": round(pv, 2),
            "battery_kwh": round(batt_kwh, 2),
            "import_kwh": round(imp, 2), "export_kwh": round(exp, 2), "soc": pre_soc_pct,
            "import_rate_p": round(ir, 2) if ir is not None else None,
            "export_rate_p": round(er, 2) if er is not None else None,
            "cost_p": round(import_cost - export_income, 2),
            "battery_mode": mode,
        }
        output_slots.append(out)
        forecast_soc_points.append({"time": e.isoformat(), "soc": soc_pct})

        # Counterfactual no-forced-slots scenario. All non-battery inputs,
        # tariff treatment, Eco behaviour, efficiencies and limits are identical.
        no_pre_soc_pct = int(round(100 * no_slots_soc_kwh / batt["capacity"]))
        no_slots_soc_points.append({"time": s.isoformat(), "soc": no_pre_soc_pct})
        if overnight_start_soc_no_slots is None and s.astimezone(timezone.utc) >= target_start.astimezone(timezone.utc):
            overnight_start_soc_no_slots = no_pre_soc_pct
        no_slots_soc_kwh, no_batt_kwh, no_imp, no_exp = simulate_battery(
            slot, no_slots_batt, no_slots_soc_kwh, charge_eff, discharge_eff
        )
        no_soc_pct = int(round(100 * no_slots_soc_kwh / batt["capacity"]))
        no_import_cost = no_imp * ir if ir is not None else 0.0
        no_export_income = no_exp * er if er is not None else 0.0
        no_out = {
            "start": s.isoformat(),
            "baseline_kwh": round(base, 2), "ch_kwh": round(ch, 2), "dhw_kwh": round(dhw, 2),
            "load_kwh": round(base + ch + dhw, 2), "pv_kwh": round(pv, 2),
            "battery_kwh": round(no_batt_kwh, 2),
            "import_kwh": round(no_imp, 2), "export_kwh": round(no_exp, 2), "soc": no_pre_soc_pct,
            "import_rate_p": round(ir, 2) if ir is not None else None,
            "export_rate_p": round(er, 2) if er is not None else None,
            "cost_p": round(no_import_cost - no_export_income, 2),
            "battery_mode": battery_mode_for_slot(s, no_slots_batt, ignore_forced_slots=True),
        }
        no_slots_output.append(no_out)
        no_slots_soc_points.append({"time": e.isoformat(), "soc": no_soc_pct})

        if LOG.isEnabledFor(logging.DEBUG):
            if abs(batt_kwh) > 0.0005:
                action = "discharge" if batt_kwh > 0 else "charge"
            elif charge_target is not None:
                action = "idle:charge_target_met_or_no_rate"
            elif discharge_target is not None:
                floor_pct = max(batt["reserve"], clamp(discharge_target, 0, 100))
                if soc_before_kwh <= batt["capacity"] * floor_pct / 100.0 + 1e-6:
                    action = "idle:bms_discharge_target_reached"
                else:
                    action = "idle:discharge_no_rate"
            elif not batt["eco"]:
                action = "idle:eco_off"
            elif pv > (base + ch + dhw) and soc_before_kwh >= batt["capacity"] - 1e-6:
                action = "idle:battery_full"
            elif pv < (base + ch + dhw) and soc_before_kwh <= batt["capacity"] * batt["reserve"] / 100.0 + 1e-6:
                action = "idle:reserve_reached"
            else:
                action = "idle:no_net_flow"
            LOG.debug(
                "slot %s action=%s mode=%s eco=%s pause=%s charge_target=%s discharge_target=%s "
                "base=%.3f ch=%.3f dhw=%.3f load=%.3f pv=%.3f batt=%.3f imp=%.3f exp=%.3f "
                "soc_start=%d soc_end=%d no_slots_batt=%.3f no_slots_imp=%.3f no_slots_exp=%.3f "
                "no_slots_soc_start=%d no_slots_soc_end=%d ir=%s er=%s",
                s.isoformat(), action, mode, batt["eco"], active_pause_mode(s, batt), charge_target, discharge_target,
                base, ch, dhw, base+ch+dhw, pv, batt_kwh, imp, exp, pre_soc_pct, soc_pct,
                no_batt_kwh, no_imp, no_exp, no_pre_soc_pct, no_soc_pct, ir, er
            )

    if overnight_start_soc is None:
        overnight_start_soc = int(round(100 * soc_kwh / batt["capacity"])) if output_slots else int(round(batt["soc"]))
    if overnight_start_soc_no_slots is None:
        overnight_start_soc_no_slots = int(round(100 * no_slots_soc_kwh / batt["capacity"])) if no_slots_output else int(round(batt["soc"]))

    # Today actual + forecast summaries.
    actual, actual_extremes = actual_today(client, cfg, now, import_rates, export_rates, completed, cheap_rate_p, imp_e, exp_e)

    def sum_slot_list(slot_list: list[dict[str, Any]], soc_points_all: list[dict[str, Any]], day: date) -> tuple[dict[str, float], dict[str, Any]]:
        ss = [x for x in slot_list if parse_dt(x["start"]).astimezone(tz).date() == day]
        vals = {
            "load_kwh": sum(x["load_kwh"] for x in ss),
            "ch_kwh": sum(x["ch_kwh"] for x in ss),
            "dhw_kwh": sum(x["dhw_kwh"] for x in ss),
            "pv_kwh": sum(x["pv_kwh"] for x in ss),
            "import_kwh": sum(x["import_kwh"] for x in ss),
            "export_kwh": sum(x["export_kwh"] for x in ss),
            "import_cost_p": sum(x["import_kwh"] * (x["import_rate_p"] or 0) for x in ss),
            "export_income_p": sum(x["export_kwh"] * (x["export_rate_p"] or 0) for x in ss),
            "forecast_battery_charge_kwh": sum(max(0.0, -x["battery_kwh"]) for x in ss),
            "forecast_battery_discharge_kwh": sum(max(0.0, x["battery_kwh"]) for x in ss),
        }
        vals["cost_p"] = vals["import_cost_p"] - vals["export_income_p"]
        vals = {k: round(v, 2) for k, v in vals.items()}
        soc_points = [x for x in soc_points_all if parse_dt(x["time"]).astimezone(tz).date() == day]
        if soc_points:
            mn = min(soc_points, key=lambda x: x["soc"])
            mx = max(soc_points, key=lambda x: x["soc"])
            ext = {
                "min_soc": {"value": mn["soc"], "time": mn["time"]},
                "max_soc": {"value": mx["soc"], "time": mx["time"]},
            }
        else:
            ext = {}
        return vals, ext

    def controller_totals(slot_list: list[dict[str, Any]], day: date) -> dict[str, float]:
        ss = [x for x in slot_list if parse_dt(x["start"]).astimezone(tz).date() == day]
        return {
            "load_kwh": round(sum(x["load_kwh"] for x in ss), 2),
            "pv_kwh": round(sum(x["pv_kwh"] for x in ss), 2),
            "import_kwh": round(sum(x["import_kwh"] for x in ss), 2),
            "export_kwh": round(sum(x["export_kwh"] for x in ss), 2),
            "cost_p": round(sum(x["cost_p"] for x in ss), 2),
        }

    today_fc, today_fc_ext = sum_slot_list(output_slots, forecast_soc_points, now.date())
    tomorrow_fc, tomorrow_ext = sum_slot_list(output_slots, forecast_soc_points, now.date() + timedelta(days=1))
    no_slots_today = controller_totals(no_slots_output, now.date())
    no_slots_tomorrow = controller_totals(no_slots_output, now.date() + timedelta(days=1))
    combined_keys = ["load_kwh", "ch_kwh", "dhw_kwh", "pv_kwh", "import_kwh", "export_kwh", "import_cost_p", "export_income_p", "cost_p"]
    total = {k: round(actual.get(k, 0.0) + today_fc.get(k, 0.0), 2) for k in combined_keys}

    # Full-day SOC extremes: actual history + forecast for today.
    today_ext = dict(actual_extremes)
    if today_fc_ext:
        if "min_soc" not in today_ext or today_fc_ext["min_soc"]["value"] < today_ext["min_soc"]["value"]:
            today_ext["min_soc"] = today_fc_ext["min_soc"]
        if "max_soc" not in today_ext or today_fc_ext["max_soc"]["value"] > today_ext["max_soc"]["value"]:
            today_ext["max_soc"] = today_fc_ext["max_soc"]

    LOG.info(
        "Forecast battery summary: start_soc=%.1f%% overnight_start_soc=%s%% no_slots_overnight_soc=%s%% today_charge=%.2fkWh today_discharge=%.2fkWh tomorrow_charge=%.2fkWh tomorrow_discharge=%.2fkWh end_soc=%.1f%%",
        batt["soc"], overnight_start_soc, overnight_start_soc_no_slots,
        today_fc.get("forecast_battery_charge_kwh", 0.0), today_fc.get("forecast_battery_discharge_kwh", 0.0),
        tomorrow_fc.get("forecast_battery_charge_kwh", 0.0), tomorrow_fc.get("forecast_battery_discharge_kwh", 0.0),
        (100.0 * soc_kwh / batt["capacity"]) if batt["capacity"] else 0.0,
    )

    attrs = {
        "friendly_name": "Home Energy Forecast",
        "unit_of_measurement": "%",
        "icon": "mdi:home-lightning-bolt-outline",
        "forecast_generated_at": now.isoformat(),
        "offpeak": {
            "start": target_start.isoformat(),
            "end": target_end.isoformat(),
            "rate_p": round(float(cheap_rate_p), 4) if cheap_rate_p is not None else None,
        },
        "overnight_start_soc": int(overnight_start_soc),
        "overnight_start_soc_no_slots": int(overnight_start_soc_no_slots),
        "metering": {
            "import_entity": imp_e,
            "export_entity": exp_e,
            "import_source": import_meter_source,
            "export_source": export_meter_source,
            "ev_included_in_battery_load": bool(cfg.section("load").get("ev_included_in_battery_load", False)),
            "ev_charging_entity": str(cfg.section("load").get("ev_charging_entity") or ""),
            "ev_forecasted": False,
        },
        "controller_inputs": {
            "version": 4,
            "timezone": str(cfg.setting("timezone", "Europe/London")),
            "refresh_request_entity": str(cfg.setting("controller_refresh_request_entity", "sensor.home_energy_forecast_refresh_request")),
            "entities": {
                "battery_soc": cfg.section("battery")["soc"],
                "battery_capacity": cfg.section("battery")["capacity_kwh"],
                "battery_reserve": cfg.section("battery")["reserve_soc"],
                "pv_energy_total": cfg.section("solar")["energy_total_kwh"],
                "grid_import_energy_total": imp_e,
                "grid_export_energy_total": exp_e,
                "inverter_max_charge_rate": cfg.section("battery")["inverter_max_rate_w"],
                "inverter_max_discharge_rate": cfg.section("battery")["inverter_max_rate_w"],
                "eco_mode": cfg.section("battery")["eco_mode"],
                "charge_schedule_enable": cfg.section("battery")["charge_schedule_enabled"],
                "discharge_schedule_enable": cfg.section("battery")["discharge_schedule_enabled"],
                "charge_slot_1_start": cfg.section("battery")["charge_start_1"],
                "charge_slot_1_end": cfg.section("battery")["charge_end_1"],
                "charge_slot_1_target": cfg.section("battery")["charge_target_1"],
                "charge_rate": cfg.section("battery")["charge_rate_w"],
                "discharge_slot_1_start": cfg.section("battery")["discharge_start_1"],
                "discharge_slot_1_end": cfg.section("battery")["discharge_end_1"],
                "discharge_slot_1_target": cfg.section("battery")["discharge_target_1"],
                "discharge_rate": cfg.section("battery")["discharge_rate_w"],
                "pause_mode": cfg.section("battery")["pause_mode"],
                "pause_start": cfg.section("battery")["pause_start"],
                "pause_end": cfg.section("battery")["pause_end"],
            },
            "values": {
                "capacity_kwh": round(float(batt["capacity"]), 3),
                "reserve_soc": round(float(batt["reserve"]), 1),
                "max_charge_rate_w": int(round(float(batt["max_rate_w"]))),
                "max_discharge_rate_w": int(round(float(batt["max_rate_w"]))),
                "grid_import_meter_source": import_meter_source,
                "grid_export_meter_source": export_meter_source,
                "ev_included_in_battery_load": bool(cfg.section("load").get("ev_included_in_battery_load", False)),
            },
        },
        "today": {
            "actual": {k: round(v, 2) for k, v in actual.items()},
            "forecast": today_fc,
            "total": total,
            **today_ext,
        },
        "tomorrow": {**tomorrow_fc, **tomorrow_ext},
        "forecast": output_slots,
        "forecast_no_slots": no_slots_output,
        "no_slots_totals": {
            "today": no_slots_today,
            "tomorrow": no_slots_tomorrow,
        },
    }
    return {"state": int(overnight_start_soc), "attributes": attrs}, reasons


# ---------- Midnight forecast verification ----------

def save_tomorrow_midnight_snapshot(store: Store, payload: dict[str, Any], now: datetime) -> None:
    """Continuously stage tomorrow's forecast; the last run before midnight becomes the frozen snapshot."""
    tomorrow = now.date() + timedelta(days=1)
    slots = [
        x for x in payload.get("attributes", {}).get("forecast", [])
        if parse_dt(x.get("start")).astimezone(now.tzinfo).date() == tomorrow
    ]
    if slots:
        store.save_forecast_snapshot(tomorrow, now, slots)
    # Keep enough history for yesterday comparisons plus a little margin.
    store.prune_forecast_snapshots_before(now.date() - timedelta(days=35))


def completed_load_slots(client: HAClient, cfg: Config, day: date, now: datetime) -> list[dict[str, Any]]:
    tz = now.tzinfo
    start, end = day_bounds(day, tz)  # type: ignore[arg-type]
    effective_end = min(end, now) if day == now.date() else end
    # Compare only completed half-hour slots. A partial current slot is deliberately omitted.
    floor_end = effective_end.replace(minute=(effective_end.minute // 30) * 30, second=0, microsecond=0)
    if floor_end <= start:
        return []
    load_cfg = cfg.section("load")
    entity = load_cfg["energy_total_kwh"]
    ev_included = bool(load_cfg.get("ev_included_in_battery_load", False))
    ev_e = str(load_cfg.get("ev_charging_entity") or "").strip()
    entities = [entity] + ([ev_e] if ev_included and ev_e else [])
    all_hist = client.history(entities, start - timedelta(hours=2), floor_end, timeout=60)
    hist = all_hist.get(entity, [])
    ev_hist = all_hist.get(ev_e, []) if ev_e else []
    boundaries = real_half_hours(start, floor_end)
    if not boundaries or boundaries[0] != start:
        boundaries.insert(0, start)
    if boundaries[-1] != floor_end:
        boundaries.append(floor_end)
    out: list[dict[str, Any]] = []
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        if ev_included:
            ev_state = ev_active_during(ev_hist, a, b) if ev_e else None
            if ev_state is not False:
                continue
        val = diff_cumulative(hist, a, b)
        if val is None or val < 0:
            continue
        out.append({"start": a.isoformat(), "end": b.isoformat(), "load_kwh": round(float(val), 3)})
    return out


def comparison_for_day(client: HAClient, store: Store, cfg: Config, day: date, now: datetime) -> dict[str, Any] | None:
    snap = store.forecast_snapshot(day)
    if not snap:
        return None
    captured_at, forecast_slots = snap
    forecast_map: dict[str, dict[str, Any]] = {}
    full_forecast_total = 0.0
    for slot in forecast_slots:
        try:
            st = parse_dt(slot.get("start")).astimezone(now.tzinfo)
            if st.date() != day:
                continue
            key = st.replace(second=0, microsecond=0).isoformat()
            forecast_map[key] = slot
            full_forecast_total += float(slot.get("load_kwh", 0.0) or 0.0)
        except Exception:
            continue

    # Keep the frozen midnight forecast as a full-day curve, independent of
    # how much actual data has completed. This is important for charting: the
    # forecast should always extend to the end of the day while actual grows
    # through the day.
    forecast_points: list[dict[str, Any]] = []
    full_forecast_cum = 0.0
    for key in sorted(forecast_map, key=lambda k: parse_dt(k).astimezone(timezone.utc)):
        fc = forecast_map[key]
        st = parse_dt(fc["start"]).astimezone(now.tzinfo)
        # Snapshot slots are normal half-hours for the target day. Use the end
        # of the slot for a cumulative-energy point.
        en = (st.astimezone(timezone.utc) + timedelta(minutes=30)).astimezone(now.tzinfo)
        f = float(fc.get("load_kwh", 0.0) or 0.0)
        full_forecast_cum += f
        forecast_points.append({
            "time": en.isoformat(),
            "forecast_load_kwh": round(f, 2),
            "forecast_cumulative_kwh": round(full_forecast_cum, 2),
        })

    actual_slots = completed_load_slots(client, cfg, day, now)
    actual_points: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    actual_cum = 0.0
    forecast_cum = 0.0
    for actual in actual_slots:
        a = float(actual["load_kwh"] or 0.0)
        actual_cum += a
        actual_points.append({
            "time": actual["end"],
            "actual_load_kwh": round(a, 2),
            "actual_cumulative_kwh": round(actual_cum, 2),
        })

        # Accuracy metrics only need slots where frozen forecast and actual can
        # be paired. Do not let a missing pairing suppress the actual curve.
        st = parse_dt(actual["start"]).astimezone(now.tzinfo)
        key = st.replace(second=0, microsecond=0).isoformat()
        fc = forecast_map.get(key)
        if fc is None:
            continue
        f = float(fc.get("load_kwh", 0.0) or 0.0)
        forecast_cum += f
        points.append({
            "time": actual["end"],
            "actual_load_kwh": round(a, 2),
            "forecast_load_kwh": round(f, 2),
            "actual_cumulative_kwh": round(actual_cum, 2),
            "forecast_cumulative_kwh": round(forecast_cum, 2),
            "error_kwh": round(f - a, 2),
        })

    actual_total = round(actual_cum, 2)
    forecast_to_now = round(forecast_cum, 2)
    error = round(forecast_to_now - actual_total, 2)
    error_pct = round((error / actual_total) * 100.0, 1) if actual_total > 0 else None
    mae = round(sum(abs(float(x["error_kwh"])) for x in points) / len(points), 3) if points else None
    bias = round(sum(float(x["error_kwh"]) for x in points) / len(points), 3) if points else None
    return {
        "date": day.isoformat(),
        "snapshot_captured_at": captured_at,
        "forecast_full_day_load_kwh": round(full_forecast_total, 2),
        "forecast_completed_load_kwh": forecast_to_now,
        "actual_completed_load_kwh": actual_total,
        "error_kwh": error,
        "error_percent": error_pct,
        "mae_kwh_per_slot": mae,
        "bias_kwh_per_slot": bias,
        "completed_slots": len(points),
        "forecast_points": forecast_points,
        "actual_points": actual_points,
        "points": points,
    }


def publish_battery_efficiency(client: HAClient, cfg: Config) -> None:
    charge = float(cfg.setting("charge_efficiency", 0.95)) * 100.0
    discharge = float(cfg.setting("discharge_efficiency", 0.95)) * 100.0
    values = [
        ("sensor.home_energy_manager_battery_charge_efficiency", "Home Energy Manager Battery Charge Efficiency", charge),
        ("sensor.home_energy_manager_battery_discharge_efficiency", "Home Energy Manager Battery Discharge Efficiency", discharge),
        ("sensor.home_energy_manager_battery_round_trip_efficiency", "Home Energy Manager Battery Round Trip Efficiency", charge * discharge / 100.0),
    ]
    for entity_id, name, value in values:
        client.publish(entity_id, round(value, 1), {"friendly_name": name, "unit_of_measurement": "%", "icon": "mdi:battery-sync", "source": "configured_forecast_efficiency"})


def publish_comparison(client: HAClient, store: Store, cfg: Config, now: datetime) -> None:
    today = comparison_for_day(client, store, cfg, now.date(), now)
    yesterday = comparison_for_day(client, store, cfg, now.date() - timedelta(days=1), now)
    state = today.get("error_kwh") if today else "unknown"
    attrs = {
        "friendly_name": "Home Energy Forecast Comparison",
        "icon": "mdi:chart-timeline-variant",
        "unit_of_measurement": "kWh",
        "today": today,
        "yesterday": yesterday,
    }
    client.publish(COMPARISON_ENTITY, state, attrs)


# ---------- Health / lifecycle ----------
def publish_health(client: HAClient, state: str, reasons: list[str], last_success: str | None, duration_s: float, store: Store, failures: int) -> None:
    attrs = {
        "friendly_name": "Home Energy Forecast Health",
        "icon": "mdi:heart-pulse",
        "reasons": reasons,
        "last_successful_forecast": last_success,
        "calculation_duration_s": round(duration_s, 2),
        "last_model_rebuild": store.meta_get("last_model_rebuild"),
        "baseline_history_days_loaded": store.count_days("baseline_slots"),
        "forecast_slots_generated": int(store.meta_get("last_forecast_slots", "0") or 0),
        "consecutive_failures": failures,
        "version": VERSION,
    }
    client.publish(HEALTH_ENTITY, state, attrs)


def restore_last_forecast(client: HAClient, now: datetime) -> tuple[str | None, bool]:
    if not LAST_FORECAST_PATH.exists():
        return None, False
    try:
        payload = json.loads(LAST_FORECAST_PATH.read_text())
        generated = parse_dt(payload["attributes"]["forecast_generated_at"])
        age_m = (now.astimezone(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds() / 60
        client.publish(OUTPUT_ENTITY, payload["state"], payload["attributes"])
        return generated.isoformat(), age_m <= PERSISTED_FORECAST_STALE_MINUTES
    except Exception as exc:
        LOG.warning("Could not restore persisted forecast: %s", exc)
        return None, False


def model_signature(cfg: Config) -> str:
    payload = {
        "history_days": int(cfg.setting("history_days", 28)),
        "minimum_baseline_w": int(cfg.setting("minimum_baseline_w", 200)),
        "load": cfg.section("load").get("energy_total_kwh"),
        "ev_included_in_battery_load": bool(cfg.section("load").get("ev_included_in_battery_load", False)),
        "ev_charging_entity": cfg.section("load").get("ev_charging_entity"),
        "ch": cfg.section("ashp").get("ch_energy_total_kwh"),
        "dhw": cfg.section("ashp").get("dhw_energy_total_kwh"),
        "pv": cfg.section("solar").get("energy_total_kwh"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def should_rebuild(store: Store, now: datetime) -> bool:
    last = store.meta_get("last_model_rebuild")
    if not last:
        return True
    try:
        last_dt = parse_dt(last).astimezone(now.tzinfo)
    except Exception:
        return True
    scheduled = datetime.combine(now.date(), DAILY_REBUILD_TIME, tzinfo=now.tzinfo)
    return now >= scheduled and last_dt < scheduled



def controller_refresh_request_id(client: HAClient, cfg: Config) -> int:
    """Read the controller->forecaster refresh request sequence from Home Assistant."""
    entity = str(cfg.setting("controller_refresh_request_entity", "sensor.home_energy_forecast_refresh_request")).strip()
    if not entity:
        return 0
    state = client.state_optional(entity)
    if not state:
        return 0
    try:
        return max(0, int(float(state.get("state", 0))))
    except (TypeError, ValueError):
        return 0


def wait_until_scheduled_or_refresh(
    client: HAClient,
    cfg: Config,
    tz: ZoneInfo,
    last_processed_refresh_id: int,
) -> tuple[str, int]:
    """Wait for the next wall-clock forecast boundary, but wake early for a controller refresh."""
    now = datetime.now(tz)
    interval_minutes = max(1, int(cfg.setting("forecast_interval_minutes", 5)))
    minutes_since_midnight = now.hour * 60 + now.minute
    next_slot_minutes = ((minutes_since_midnight // interval_minutes) + 1) * interval_minutes
    next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_slot_minutes)
    poll_seconds = 5.0
    while True:
        now = datetime.now(tz)
        remaining = (next_run - now).total_seconds()
        if remaining <= 0:
            return "scheduled", controller_refresh_request_id(client, cfg)
        request_id = controller_refresh_request_id(client, cfg)
        if request_id > last_processed_refresh_id:
            return "controller_refresh", request_id
        time.sleep(min(poll_seconds, max(0.2, remaining)))


def main() -> None:
    cfg = Config.load()
    level = getattr(logging, str(cfg.setting("log_level", "INFO")).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    tz = ZoneInfo(str(cfg.setting("timezone", "Europe/London")))
    client = HAClient()
    store = Store(DB_PATH)
    now = datetime.now(tz)
    LOG.info("Home Energy Forecaster v%s starting (%s)", VERSION, tz.key)
    last_success, restored_fresh = restore_last_forecast(client, now)
    if last_success:
        LOG.info("Restored persisted forecast from %s (%s)", last_success, "fresh" if restored_fresh else "stale")

    failures = int(store.meta_get("consecutive_failures", "0") or 0)
    last_success = store.meta_get("last_successful_forecast", last_success)
    forecast_sequence = int(store.meta_get("forecast_sequence", "0") or 0)
    last_processed_refresh_id = int(store.meta_get("last_processed_refresh_request", "0") or 0)

    # First startup or a learning-input/config change requires a full model rebuild.
    signature = model_signature(cfg)
    need_full_rebuild = store.count_days("baseline_slots") == 0 or store.meta_get("model_signature") != signature
    if need_full_rebuild:
        try:
            LOG.info("Building full history cache%s", " (model configuration changed)" if store.count_days("baseline_slots") else "")
            rebuild_models(client, store, cfg, now, force=True)
            if store.count_days("baseline_slots") == 0:
                raise HAError("Model rebuild produced no usable baseline history")
            store.meta_set("model_signature", signature)
        except Exception as exc:
            failures += 1
            store.meta_set("consecutive_failures", str(failures))
            publish_health(client, "error", [f"Initial/full model build failed: {exc}"], last_success, 0, store, failures)
            raise

    requested_id = controller_refresh_request_id(client, cfg)
    # If a controller refresh request arrived while startup/history rebuild was
    # in progress, the first forecast is that requested refresh, not merely a
    # startup forecast. This prevents the controller refresh barrier sticking.
    trigger = "controller_refresh" if requested_id > last_processed_refresh_id else "startup"

    while True:
        started = time.monotonic()
        now = datetime.now(tz)
        # Capture the request ID at the START of this calculation. A request
        # arriving during calculation is deliberately left pending for another run.
        run_request_id = requested_id if requested_id is not None else controller_refresh_request_id(client, cfg)
        run_refresh_id = run_request_id if run_request_id > last_processed_refresh_id else last_processed_refresh_id
        health_reasons: list[str] = []
        published = False
        try:
            if should_rebuild(store, now):
                try:
                    rebuild_models(client, store, cfg, now)
                except Exception as exc:
                    health_reasons.append(f"Daily model rebuild failed: previous cache retained ({exc})")
                    LOG.warning(health_reasons[-1])
                    if store.count_days("baseline_slots") == 0:
                        raise HAError("No usable baseline cache after failed model rebuild")

            payload, forecast_reasons = make_forecast(client, store, cfg, now)
            health_reasons.extend(forecast_reasons)

            next_sequence = forecast_sequence + 1
            payload["attributes"]["forecast_sequence"] = next_sequence
            payload["attributes"]["refresh_request_id"] = run_refresh_id
            payload["attributes"]["forecast_trigger"] = trigger

            client.publish(OUTPUT_ENTITY, payload["state"], payload["attributes"])
            published = True
            forecast_sequence = next_sequence
            store.meta_set("forecast_sequence", str(forecast_sequence))

            if run_request_id > last_processed_refresh_id:
                last_processed_refresh_id = run_request_id
                store.meta_set("last_processed_refresh_request", str(last_processed_refresh_id))

            # Keep overwriting tomorrow's staged snapshot. At midnight the date rolls over,
            # so the final pre-midnight version automatically becomes today's frozen forecast.
            save_tomorrow_midnight_snapshot(store, payload, now)
            try:
                publish_comparison(client, store, cfg, now)
                publish_battery_efficiency(client, cfg)
            except Exception as exc:
                health_reasons.append(f"Forecast comparison unavailable: {exc}")
                LOG.warning(health_reasons[-1])

            LAST_FORECAST_PATH.write_text(json.dumps(payload, separators=(",", ":")))
            last_success = now.isoformat()
            failures = 0
            store.meta_set("last_successful_forecast", last_success)
            store.meta_set("consecutive_failures", "0")
            store.meta_set("last_forecast_slots", str(len(payload["attributes"]["forecast"])))
            duration = time.monotonic() - started
            health_state = "degraded" if health_reasons else "ok"
            publish_health(client, health_state, health_reasons, last_success, duration, store, failures)
            LOG.info(
                "Forecast published: sequence=%d trigger=%s refresh_request_id=%d overnight_start_soc=%s%% slots=%d today_cost=%.2fp tomorrow_cost=%.2fp health=%s duration=%.2fs",
                forecast_sequence, trigger, run_refresh_id, payload["state"],
                len(payload["attributes"]["forecast"]),
                payload["attributes"]["today"]["total"]["cost_p"],
                payload["attributes"]["tomorrow"]["cost_p"],
                health_state, duration
            )
        except Exception as exc:
            failures += 1
            store.meta_set("consecutive_failures", str(failures))
            duration = time.monotonic() - started
            LOG.exception("Forecast calculation failed")
            reasons = [str(exc)]
            if last_success:
                try:
                    age_m = (now.astimezone(timezone.utc) - parse_dt(last_success).astimezone(timezone.utc)).total_seconds() / 60
                    reasons.append(f"Last successful forecast is {age_m:.0f} minutes old" + (" (stale)" if age_m > PERSISTED_FORECAST_STALE_MINUTES else ""))
                except Exception:
                    pass
            publish_health(client, "error", reasons, last_success, duration, store, failures)

        # A failed controller-triggered run must not acknowledge the request:
        # the wait loop sees it as still pending and retries promptly.
        if not published and run_request_id > last_processed_refresh_id:
            trigger = "controller_refresh_retry"
            requested_id = run_request_id
            time.sleep(5)
            continue

        trigger, requested_id = wait_until_scheduled_or_refresh(
            client, cfg, tz, last_processed_refresh_id
        )
        LOG.debug("Next forecast trigger=%s request_id=%s", trigger, requested_id)


if __name__ == "__main__":
    main()
