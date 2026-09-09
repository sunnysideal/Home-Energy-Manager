#!/usr/bin/env python3
"""Periodic passive DHW validation/confidence runner."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from dhw_model import ensure_dhw_model_schema
from dhw_validation import (
    apply_actual_energy,
    apply_actuals,
    confidence_result,
    horizon_metrics,
    persist_confidence,
)

LOG = logging.getLogger("ashp_dhw_validation")
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))


def run_once(db: sqlite3.Connection) -> None:
    matched = apply_actuals(db)
    energy_matched = apply_actual_energy(db)
    result = confidence_result(db)
    persist_confidence(db, result)
    metrics = horizon_metrics(db)
    metric_text = ", ".join(
        f"{m.name}:n={m.count},upper={m.upper_mae_c:.2f}C,lower={m.lower_mae_c:.2f}C"
        if m.count and m.upper_mae_c is not None and m.lower_mae_c is not None
        else f"{m.name}:n=0"
        for m in metrics
    )
    energy_text = (
        f"energy:n={result.energy_validation_count},days={result.energy_validation_days},"
        f"thermal_mae={result.thermal_energy_mae_kwh:.3f}kWh,"
        f"legacy_mae={result.legacy_energy_mae_kwh:.3f}kWh"
        if result.thermal_energy_mae_kwh is not None and result.legacy_energy_mae_kwh is not None
        else f"energy:n={result.energy_validation_count},days={result.energy_validation_days},not_ready"
    )
    LOG.info(
        "DHW validation: temp_matched=%d energy_matched=%d confidence=%.0f%% "
        "trial_ready=%s thermal_ready=%s promotion_ready=%s performance_bad=%s "
        "passive=%d cycles=%d demand_days=%d draws=%d near_validation=%d %s [%s]",
        matched,
        energy_matched,
        result.confidence * 100.0,
        result.trial_ready,
        result.thermal_ready,
        result.promotion_ready,
        result.performance_bad,
        result.passive_samples,
        result.cycle_count,
        result.demand_days,
        result.draw_count,
        result.validation_count,
        energy_text,
        metric_text,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = sqlite3.connect(DB_PATH, timeout=30)
    ensure_dhw_model_schema(db)
    while True:
        try:
            run_once(db)
        except Exception:
            LOG.exception("DHW validation update failed")
        time.sleep(300)


if __name__ == "__main__":
    main()
