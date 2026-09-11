#!/usr/bin/env python3
"""Run one DHW learning pass at ASHP component startup.

The collector owns live sampling and hourly retraining. This helper waits for the collector's
schema/bootstrap work to become available, then runs the same persisted learners once so an
add-on restart does not have to wait for the collector's next scheduled learning pass.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from dhw_cycle_learner import learn_cycle_energy, persist_cycle_energy_fit
from dhw_demand_learner import learn_demand_profile, persist_demand_profile
from dhw_efficiency_learner import (
    evaluate_shadow_cycle_model,
    learn_temperature_efficiency,
    persist_temperature_efficiency_fit,
)
from dhw_passive_learner import learn_passive_parameters, persist_passive_fit

LOG = logging.getLogger("ashp_dhw_collector")
DB_PATH = Path(os.environ.get("ASHP_FORECASTER_DB_PATH", "/data/ashp_forecast.db"))
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


def _tables_ready(db: sqlite3.Connection) -> bool:
    names = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {"dhw_thermal_samples", "dhw_heating_cycles", "dhw_draw_events", "dhw_model_parameters"} <= names


def _log_efficiency(db: sqlite3.Connection, cycle_fit) -> None:
    fit = learn_temperature_efficiency(db)
    if fit is None:
        LOG.info(
            "DHW temperature efficiency shadow model not ready: "
            "need more clean heating intervals across temperature bands"
        )
        return

    validation_cycles = 0
    baseline_mae = None
    curve_mae = None
    if cycle_fit is not None:
        validation_cycles, baseline_mae_value, curve_mae_value = evaluate_shadow_cycle_model(db, cycle_fit, fit)
        if validation_cycles:
            baseline_mae = baseline_mae_value
            curve_mae = curve_mae_value

    persist_temperature_efficiency_fit(
        db,
        fit,
        baseline_mae_kwh=baseline_mae,
        curve_mae_kwh=curve_mae,
        validation_cycles=validation_cycles,
    )
    curve = ",".join(
        f"{item.center_c:.1f}C:{item.multiplier:.3f}x(n={item.sample_count})"
        for item in fit.bins
    )
    comparison = (
        f" validation_cycles={validation_cycles} baseline_mae={baseline_mae:.3f}kWh "
        f"curve_mae={curve_mae:.3f}kWh"
        if baseline_mae is not None and curve_mae is not None
        else " validation_cycles=0 baseline_mae=n/a curve_mae=n/a"
    )
    LOG.info(
        "DHW temperature efficiency shadow learned: observations=%d range=%.1f-%.1fC "
        "curve=[%s] fit_rmse=%.3fx%s production=unchanged",
        fit.sample_count,
        fit.min_temp_c,
        fit.max_temp_c,
        curve,
        fit.rmse_multiplier,
        comparison,
    )


def run_learning_pass(db: sqlite3.Connection, cfg: dict) -> None:
    sample_minutes = max(1, int(cfg.get("dhw_thermal_sample_minutes", 5)))
    passive_fit = learn_passive_parameters(db, volume_l=float(cfg.get("dhw_tank_volume_l", 250)))
    cycle_fit = learn_cycle_energy(db)
    demand_slots = learn_demand_profile(
        db,
        timezone_name=str(os.environ.get("TZ") or "UTC"),
        history_days=int(cfg.get("dhw_history_days", 28)),
        sample_minutes=sample_minutes,
    )

    if passive_fit is not None:
        persist_passive_fit(db, passive_fit)
        LOG.info(
            "DHW startup passive model learned: upper_loss=%.3fW/K lower_loss=%.3fW/K "
            "coupling=%.3fW/K intervals=%d rmse=%.3fC",
            passive_fit.upper_loss_w_per_k,
            passive_fit.lower_loss_w_per_k,
            passive_fit.coupling_w_per_k,
            passive_fit.sample_count,
            passive_fit.rmse_c,
        )
    else:
        LOG.info("DHW startup passive model not ready: insufficient/unsuitable quiet samples")

    if cycle_fit is not None:
        # Persist only the cycle model here. persist_cycle_energy_fit also runs the efficiency
        # shadow learner for backward compatibility; the explicit call below guarantees a
        # diagnostic even when the cycle model is unavailable.
        persist_cycle_energy_fit(db, cycle_fit)
        LOG.info(
            "DHW startup cycle energy model learned: cycles=%d rmse=%.3fkWh",
            cycle_fit.sample_count,
            cycle_fit.rmse_kwh,
        )
    else:
        LOG.info("DHW startup cycle energy model not ready: need more valid normal cycles")

    _log_efficiency(db, cycle_fit)

    if demand_slots:
        persist_demand_profile(db, demand_slots)
        LOG.info("DHW startup demand profile learned: slots=%d", len(demand_slots))
    else:
        LOG.info("DHW startup demand profile not ready: need more fully observed days")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(OPTIONS_PATH.read_text())
    if not str(cfg.get("dhw_tank_lower_temperature_entity") or ""):
        LOG.info("DHW startup learning inactive: lower tank temperature entity is not configured")
        return

    # The collector creates the schema and performs any historical bootstrap. Wait briefly
    # for that startup work rather than racing it. Existing installations normally satisfy
    # this on the first check.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            db = sqlite3.connect(DB_PATH, timeout=30)
            if _tables_ready(db):
                sample_count = int(db.execute("SELECT COUNT(*) FROM dhw_thermal_samples").fetchone()[0])
                if sample_count > 0:
                    LOG.info("DHW startup learning pass: samples=%d", sample_count)
                    run_learning_pass(db, cfg)
                    db.close()
                    return
            db.close()
        except sqlite3.Error:
            pass
        time.sleep(0.5)
    LOG.warning("DHW startup learning pass skipped: thermal history was not ready within 60s")


if __name__ == "__main__":
    main()
