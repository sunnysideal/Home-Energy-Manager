#!/usr/bin/env python3
"""Publish passive DHW thermal-model diagnostics as separate HA entities."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.mqtt import MQTTPublisher
from dhw_model import ensure_dhw_model_schema
from dhw_simulator import TankParameters, TankState, usable_energy_kwh
from dhw_validation import confidence_result, horizon_metrics

LOG = logging.getLogger("ashp_dhw_diagnostics")
HA_API = "http://supervisor/core/api"
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


class Publisher:
    def __init__(self, token: str) -> None:
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.mqtt = MQTTPublisher(
            "ashp_dhw_model",
            "Home Energy Manager – DHW Thermal Model",
            "DHW Thermal Model",
            os.environ.get("HOME_ENERGY_MANAGER_VERSION", "unknown"),
            token,
        )

    def _rest(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
        payload = json.dumps({"state": str(state), "attributes": attributes}).encode()
        req = Request(
            f"{HA_API}/states/{entity_id}",
            data=payload,
            headers=self.headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=15):
                pass
        except (HTTPError, URLError, TimeoutError) as exc:
            LOG.warning("Could not publish %s through HA REST: %s", entity_id, exc)

    def sensor(self, entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
        if self.mqtt.publish_sensor(entity_id, state, attributes):
            return
        self._rest(entity_id, state, attributes)


def _param(db: sqlite3.Connection, name: str) -> float | None:
    row = db.execute("SELECT value FROM dhw_model_parameters WHERE name=?", (name,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _latest_sample(db: sqlite3.Connection) -> tuple[float, float, float | None, float | None] | None:
    row = db.execute(
        "SELECT upper_temp_c,lower_temp_c,target_temp_c,ambient_temp_c "
        "FROM dhw_thermal_samples WHERE valid=1 AND upper_temp_c IS NOT NULL "
        "AND lower_temp_c IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return (
        float(row[0]),
        float(row[1]),
        float(row[2]) if row[2] is not None else None,
        float(row[3]) if row[3] is not None else None,
    )


def _next_heating_energy(db: sqlite3.Connection, upper: float, lower: float, target: float | None) -> float | None:
    if target is None:
        return None
    intercept = _param(db, "dhw_cycle_intercept_kwh")
    upper_coef = _param(db, "dhw_cycle_upper_kwh_per_c")
    lower_coef = _param(db, "dhw_cycle_lower_kwh_per_c")
    if None in (intercept, upper_coef, lower_coef):
        return None
    return max(
        0.0,
        float(intercept)
        + float(upper_coef) * max(target - upper, 0.0)
        + float(lower_coef) * max(target - lower, 0.0),
    )


def _usable_energy(
    db: sqlite3.Connection,
    upper: float,
    lower: float,
    *,
    tank_volume_l: float,
    minimum_useful_temperature_c: float,
) -> float:
    upper_loss = _param(db, "dhw_upper_loss_w_per_k") or 1.2
    lower_loss = _param(db, "dhw_lower_loss_w_per_k") or 0.8
    coupling = _param(db, "dhw_coupling_w_per_k") or 3.0
    params = TankParameters(
        volume_l=tank_volume_l,
        upper_loss_w_per_k=upper_loss,
        lower_loss_w_per_k=lower_loss,
        coupling_w_per_k=coupling,
        minimum_useful_temperature_c=minimum_useful_temperature_c,
    )
    return usable_energy_kwh(TankState(upper, lower), params)


def _status(result) -> str:
    if result.promotion_ready:
        return "ready_for_promotion"
    if result.thermal_ready:
        return "thermal_model_ready"
    if result.cycle_count < 5:
        return "learning_heating_cycles"
    if result.demand_days < 7 or result.draw_count < 10:
        return "learning_hot_water_demand"
    if result.validation_count < 20:
        return "validating_shadow_forecast"
    return "learning"


def publish_diagnostics(db: sqlite3.Connection, publisher: Publisher, cfg: dict[str, Any]) -> None:
    result = confidence_result(db)
    metrics = {metric.name: metric for metric in horizon_metrics(db)}
    near = metrics["0_6h"]
    latest = _latest_sample(db)
    now = datetime.now(timezone.utc).isoformat()
    common = {
        "last_updated": now,
        "thermal_ready": result.thermal_ready,
        "promotion_ready": result.promotion_ready,
    }

    publisher.sensor(
        "sensor.ashp_dhw_model_confidence",
        round(result.confidence * 100.0, 1),
        {
            "friendly_name": "ASHP DHW Model Confidence",
            "unit_of_measurement": "%",
            **common,
        },
    )
    publisher.sensor(
        "sensor.ashp_dhw_model_status",
        _status(result),
        {
            "friendly_name": "ASHP DHW Model Status",
            "passive_intervals": result.passive_samples,
            "normal_heating_cycles": result.cycle_count,
            "demand_days": result.demand_days,
            "draw_events": result.draw_count,
            "validation_points_0_6h": result.validation_count,
            **common,
        },
    )
    publisher.sensor(
        "sensor.ashp_dhw_upper_temperature_mae",
        round(near.upper_mae_c, 2) if near.upper_mae_c is not None else "unknown",
        {
            "friendly_name": "ASHP DHW Upper Temperature MAE",
            "unit_of_measurement": "°C",
            "validation_points": near.count,
            **common,
        },
    )
    publisher.sensor(
        "sensor.ashp_dhw_lower_temperature_mae",
        round(near.lower_mae_c, 2) if near.lower_mae_c is not None else "unknown",
        {
            "friendly_name": "ASHP DHW Lower Temperature MAE",
            "unit_of_measurement": "°C",
            "validation_points": near.count,
            **common,
        },
    )

    if latest is not None:
        upper, lower, target, ambient = latest
        tank_volume_l = float(cfg.get("dhw_tank_volume_l", 250.0))
        minimum_useful = float(cfg.get("dhw_min_usable_temperature_c", 40.0))
        publisher.sensor(
            "sensor.ashp_dhw_available_energy",
            round(
                _usable_energy(
                    db,
                    upper,
                    lower,
                    tank_volume_l=tank_volume_l,
                    minimum_useful_temperature_c=minimum_useful,
                ),
                3,
            ),
            {
                "friendly_name": "ASHP DHW Available Thermal Energy",
                "unit_of_measurement": "kWh",
                "upper_temperature_c": round(upper, 2),
                "lower_temperature_c": round(lower, 2),
                "ambient_temperature_c": round(ambient, 2) if ambient is not None else None,
                "tank_volume_l": tank_volume_l,
                "minimum_useful_temperature_c": minimum_useful,
                **common,
            },
        )
        next_energy = _next_heating_energy(db, upper, lower, target)
        publisher.sensor(
            "sensor.ashp_dhw_predicted_next_heating_energy",
            round(next_energy, 3) if next_energy is not None else "unknown",
            {
                "friendly_name": "ASHP DHW Predicted Next Heating Energy",
                "unit_of_measurement": "kWh",
                "target_temperature_c": round(target, 2) if target is not None else None,
                **common,
            },
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")
    cfg = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    ensure_dhw_model_schema(db)
    db.commit()
    publisher = Publisher(token)
    while True:
        try:
            publish_diagnostics(db, publisher, cfg)
        except Exception:
            LOG.exception("DHW diagnostics publishing failed")
        time.sleep(300)


if __name__ == "__main__":
    main()
