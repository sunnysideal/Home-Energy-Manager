from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASHP_APP = ROOT / "components" / "ashp_forecaster" / "app"

spec = importlib.util.spec_from_file_location("main", ASHP_APP / "main.py")
legacy = importlib.util.module_from_spec(spec)
sys.modules["main"] = legacy
assert spec.loader is not None
spec.loader.exec_module(legacy)

spec = importlib.util.spec_from_file_location("forecast_runner_issue131", ASHP_APP / "forecast_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
assert spec.loader is not None
spec.loader.exec_module(runner)


def _client(db: sqlite3.Connection):
    client = object.__new__(runner.HorizonHAClient)
    client.tz = timezone.utc
    client.ch_validation_db = db
    client.ch_forecast_interval_minutes = 30
    client.weather_observation_db = None
    return client


def _attrs(start: datetime, *, status: str | None = None) -> dict:
    attrs = {
        "kwh_per_degree_day": 1.163,
        "winter_mode_below_c": 13.0,
        "summer_mode_above_c": 14.0,
        "forecast": [{
            "start": start.isoformat(),
            "temperature_c": 8.0,
            "heating_mode": "winter",
            "heating_enabled": True,
            "dhw_active": False,
            "degree_days": 0.15625,
            "ch_kwh": 0.42,
        }],
    }
    if status is not None:
        attrs["aggregate_status"] = status
    return attrs


def test_published_48h_sensor_is_the_capture_boundary_for_healthy_and_degraded(monkeypatch) -> None:
    db = sqlite3.connect(":memory:")
    runner.ensure_ch_validation_schema(db)
    published = []
    monkeypatch.setattr(
        runner.legacy.HAClient,
        "set_sensor",
        lambda self, entity_id, state, attributes: published.append((entity_id, state)),
    )
    client = _client(db)
    target = datetime.now(timezone.utc) + timedelta(hours=2)

    client.set_sensor("sensor.ashp_forecast_next_48h", 1.2, _attrs(target))
    client.set_sensor("sensor.ashp_forecast_next_48h", "unknown", _attrs(target + timedelta(minutes=30), status="degraded"))
    client.set_sensor("sensor.ashp_forecast_next_24h", 0.6, _attrs(target + timedelta(hours=1)))

    rows = db.execute(
        "SELECT forecast_status,forecast_ch_kwh FROM ch_forecast_observations ORDER BY target_start"
    ).fetchall()
    assert rows == [("healthy", 0.42), ("degraded", 0.42)]
    assert len(published) == 3


def test_ch_accuracy_entity_contains_end_to_end_validation_fields(monkeypatch) -> None:
    db = sqlite3.connect(":memory:")
    runner.ensure_ch_validation_schema(db)
    now = datetime.now(timezone.utc)
    target = now - timedelta(hours=1)
    issued = target - timedelta(hours=3)
    db.execute(
        """
        INSERT INTO ch_forecast_observations(
            issued_at,target_start,target_end,horizon_hours,forecast_ch_kwh,
            forecast_temperature_c,forecast_degree_days,coefficient_kwh_per_dd,
            forecast_heating_mode,forecast_heating_enabled,dhw_active,
            winter_mode_below_c,summer_mode_above_c,forecast_status,
            actual_ch_kwh,actual_mean_temperature_c,error_kwh,completed_at,data_quality
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            issued.isoformat(), target.isoformat(), (target + timedelta(minutes=30)).isoformat(),
            3.0, 0.6, 8.0, 0.15625, 1.163, "winter", 1, 0, 13.0, 14.0,
            "healthy", 0.5, 8.2, 0.1, now.isoformat(), "complete",
        ),
    )
    db.commit()

    states = {}
    client = type("Client", (), {"set_sensor": lambda self, entity, state, attrs: states.update({entity: (state, attrs)})})()
    cfg = type("Cfg", (), {"winter_mode_below_c": 13.0})()
    runner._publish_ch_scores(client, type("Store", (), {"db": db})(), cfg, timezone.utc, log_summary=False)

    state, attrs = states[runner.CH_SCORE_ENTITY]
    assert state == 0.1
    assert attrs["phase"] == "issued_forecast_validation"
    assert attrs["error_definition"] == "forecast_minus_actual"
    assert attrs["samples"] == 1
    assert attrs["mean_bias_kwh"] == 0.1
    assert attrs["horizons"]["0_6h"]["samples"] == 1
    assert "temperature_analysis" in attrs
    assert attrs["heating_active"]["samples"] == 1
