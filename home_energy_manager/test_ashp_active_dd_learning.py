from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ASHP_APP = ROOT / "components" / "ashp_forecaster" / "app"
if str(ASHP_APP) not in sys.path:
    sys.path.insert(0, str(ASHP_APP))

import main as legacy
from active_dd_training import (
    DEFAULT_MIN_ACTIVE_CH_KWH,
    DEFAULT_MIN_ACTIVE_DD,
    build_training,
    effective_training_thresholds,
)


def _state(ts: datetime, value: float) -> dict[str, str]:
    return {"state": str(value), "last_changed": ts.isoformat()}


class FakeClient:
    def __init__(self, day_start: datetime):
        self.day_start = day_start
        self.day_end = day_start + timedelta(days=1)
        # Cold for 8 hours, warm for 16 hours: high diurnal range and less than
        # the old 2-DD whole-day gate, but a genuine heating period exists.
        self.temps = [
            _state(day_start - timedelta(minutes=1), 10.0),
            _state(day_start + timedelta(hours=8), 18.0),
            _state(self.day_end, 18.0),
        ]
        # 1.10 kWh CH occurs during the cold/winter interval. A further 0.90 kWh
        # after summer mode begins must not contaminate the learned coefficient.
        self.energy = [_state(day_start - timedelta(minutes=1), 100.0)]
        total = 100.0
        for index in range(1, 49):
            ts = day_start + timedelta(minutes=30 * index)
            if index <= 16:
                total += 1.10 / 16.0
            elif index <= 32:
                total += 0.90 / 16.0
            self.energy.append(_state(ts, total))
        self.off = [_state(day_start - timedelta(days=2), 11.0)]
        self.on = [_state(day_start - timedelta(days=2), 12.0)]

    def get_history(self, entity_id: str, start: datetime, end: datetime):
        if entity_id == "sensor.ch":
            return self.energy
        if entity_id == "sensor.temp":
            return self.temps
        if entity_id == "number.off":
            return self.off
        if entity_id == "number.on":
            return self.on
        raise AssertionError(entity_id)


def _cfg() -> legacy.Config:
    return legacy.Config(
        ch_energy_entity="sensor.ch",
        outdoor_temperature_entity="sensor.temp",
        weather_entity="weather.test",
        summer_mode_off_entity="number.off",
        summer_mode_on_entity="number.on",
        training_days=2,
        minimum_daily_degree_days=2.0,
        minimum_daily_ch_kwh=2.0,
    )


def test_legacy_default_pair_migrates_to_active_evidence_thresholds():
    assert effective_training_thresholds(_cfg()) == (
        DEFAULT_MIN_ACTIVE_DD,
        DEFAULT_MIN_ACTIVE_CH_KWH,
    )


def test_custom_thresholds_are_preserved():
    cfg = _cfg()
    cfg.minimum_daily_degree_days = 0.4
    cfg.minimum_daily_ch_kwh = 0.3
    assert effective_training_thresholds(cfg) == (0.4, 0.3)


def test_shoulder_day_learns_from_winter_slots_only(monkeypatch, tmp_path):
    tz = ZoneInfo("Europe/London")
    real_datetime = datetime
    fixed_now = real_datetime(2026, 9, 12, 12, 0, tzinfo=tz)

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz_arg=None):
            return fixed_now if tz_arg else fixed_now.replace(tzinfo=None)

    # The learner asks for two days; only 11 Sep is complete and relevant.
    monkeypatch.setattr(sys.modules["active_dd_training"], "datetime", FixedDateTime)
    day_start = real_datetime(2026, 9, 11, 0, 0, tzinfo=tz)
    client = FakeClient(day_start)
    store = legacy.Store(tmp_path / "ashp.db")

    coefficient, days, slots = build_training(client, store, _cfg(), tz)

    # 8h at 10C => (15.5-10)*8/24 = 1.8333 active DD. This is below the old
    # 2-DD daily gate, but now qualifies. Only the 1.10 kWh winter-period energy
    # contributes; the 0.90 kWh summer-period CH is excluded.
    assert days == 1
    assert slots == 16
    assert abs(coefficient - (1.10 / ((15.5 - 10.0) * 8.0 / 24.0))) < 1e-6

    row = store.db.execute(
        "SELECT degree_days, ch_kwh, valid FROM daily_training WHERE day='2026-09-11'"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - ((15.5 - 10.0) * 8.0 / 24.0)) < 1e-6
    assert abs(row[1] - 1.10) < 1e-6
    assert row[2] == 1


def test_store_model_is_weighted_by_active_dd(tmp_path):
    store = legacy.Store(tmp_path / "weighted.db")
    now = datetime.now(ZoneInfo("UTC"))
    slots = [
        {
            "start_ts": now.isoformat(),
            "degree_days": 0.25,
            "ch_kwh": 0.25,
            "mean_temperature_c": 10.0,
            "winter_mode_below_c": 11.0,
            "summer_mode_above_c": 12.0,
        },
        {
            "start_ts": (now + timedelta(minutes=30)).isoformat(),
            "degree_days": 1.0,
            "ch_kwh": 2.0,
            "mean_temperature_c": 8.0,
            "winter_mode_below_c": 11.0,
            "summer_mode_above_c": 12.0,
        },
    ]
    store.replace_day("2026-09-11", slots, 1.25, 2.25, 9.0, True)
    coefficient, days, dd, ch, count = store.model(1.55, "2026-09-01")
    assert coefficient == 2.25 / 1.25
    assert (days, dd, ch, count) == (1, 1.25, 2.25, 2)
