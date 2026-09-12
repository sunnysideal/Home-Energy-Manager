"""Heating-active degree-day learning for the ASHP CH model.

The production CH coefficient is learned only from 30-minute slots in which the
reconstructed EcoMAX hysteresis state is winter.  Warm/summer slots therefore do
not dilute shoulder-season observations and their CH energy cannot contaminate
the learned coefficient.

The historical configuration names ``minimum_daily_degree_days`` and
``minimum_daily_ch_kwh`` are retained for compatibility.  In this model they mean
minimum *heating-active* evidence accumulated across a completed day.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import main as legacy

LOG = logging.getLogger("ashp_forecast")
MODEL_VERSION = 5
DEFAULT_MIN_ACTIVE_DD = 0.25
DEFAULT_MIN_ACTIVE_CH_KWH = 0.20
LEGACY_MIN_DD = 2.0
LEGACY_MIN_CH_KWH = 2.0


def effective_training_thresholds(cfg: legacy.Config) -> tuple[float, float]:
    """Return active-slot evidence thresholds, migrating the old default pair.

    Existing installations commonly persist the former 2 DD / 2 kWh defaults in
    Home Assistant options.  Treat that exact pair as the legacy default and move
    it to the new active-slot defaults.  Explicit custom values are preserved.
    """
    minimum_dd = max(0.0, float(cfg.minimum_daily_degree_days))
    minimum_ch = max(0.0, float(cfg.minimum_daily_ch_kwh))
    if math.isclose(minimum_dd, LEGACY_MIN_DD) and math.isclose(minimum_ch, LEGACY_MIN_CH_KWH):
        return DEFAULT_MIN_ACTIVE_DD, DEFAULT_MIN_ACTIVE_CH_KWH
    return minimum_dd, minimum_ch


def _ensure_training_signature(store: legacy.Store, cfg: legacy.Config) -> tuple[float, float]:
    """Invalidate old cached observations when active-DD semantics change."""
    minimum_dd, minimum_ch = effective_training_thresholds(cfg)
    signature = json.dumps(
        {
            "model_version": MODEL_VERSION,
            "base_temperature_c": round(float(cfg.base_temperature_c), 4),
            "summer_mode_off_entity": cfg.summer_mode_off_entity,
            "summer_mode_on_entity": cfg.summer_mode_on_entity,
            "interval_minutes": int(cfg.forecast_interval_minutes),
            "minimum_active_degree_days": round(minimum_dd, 4),
            "minimum_active_ch_kwh": round(minimum_ch, 4),
        },
        sort_keys=True,
    )
    row = store.db.execute("SELECT value FROM metadata WHERE key='active_dd_training_signature'").fetchone()
    if not row or row[0] != signature:
        LOG.info(
            "ASHP CH training definition changed; rebuilding heating-active observations "
            "(min active DD %.2f, min active CH %.2f kWh)",
            minimum_dd,
            minimum_ch,
        )
        with store.db:
            store.db.execute("DELETE FROM slot_training")
            store.db.execute("DELETE FROM daily_training")
            store.db.execute(
                "INSERT INTO metadata(key, value) VALUES('active_dd_training_signature', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (signature,),
            )
    return minimum_dd, minimum_ch


def build_training(
    client: legacy.HAClient,
    store: legacy.Store,
    cfg: legacy.Config,
    tz: ZoneInfo,
) -> tuple[float, int, int]:
    """Train CH kWh/DD from heating-active historical 30-minute observations.

    The historical EcoMAX summer/winter thresholds are replayed slot-by-slot.
    Degree days and CH energy enter the model only while that reconstructed state
    is winter.  The final coefficient remains the robust weighted aggregate
    ``sum(active CH kWh) / sum(active DD)`` already implemented by ``Store.model``.
    """
    minimum_dd, minimum_ch = _ensure_training_signature(store, cfg)
    now = datetime.now(tz)
    first_day = (now - timedelta(days=cfg.training_days)).date()
    interval = timedelta(minutes=30)

    threshold_start = datetime.combine(
        first_day - timedelta(days=1), datetime.min.time(), tzinfo=tz
    )
    try:
        off_history = legacy.numeric_states(
            client.get_history(cfg.summer_mode_off_entity, threshold_start, now)
        )
        on_history = legacy.numeric_states(
            client.get_history(cfg.summer_mode_on_entity, threshold_start, now)
        )
    except RuntimeError as exc:
        LOG.warning(
            "Could not read historical summer-mode thresholds; falling back to current values for training: %s",
            exc,
        )
        off_history = []
        on_history = []

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
            energy = legacy.numeric_states(client.get_history(cfg.ch_energy_entity, day_start, day_end))
            temps = legacy.numeric_states(
                client.get_history(cfg.outdoor_temperature_entity, day_start, day_end)
            )
        except RuntimeError as exc:
            LOG.warning("Could not backfill training day %s: %s", day, exc)
            continue

        slots: list[dict[str, float | str]] = []
        cursor = day_start
        while cursor < day_end:
            slot_end = min(cursor + interval, day_end)
            ch_slot = legacy.energy_delta(energy, cursor, slot_end)
            temp_slot = legacy.time_weighted_mean(temps, cursor, slot_end)
            if ch_slot is None or temp_slot is None:
                slots = []
                break

            winter_below = legacy.value_at_or_before(off_history, cursor)
            summer_above = legacy.value_at_or_before(on_history, cursor)
            if winter_below is None:
                winter_below = fallback_off
            if summer_above is None:
                summer_above = fallback_on
            if winter_below > summer_above:
                LOG.warning(
                    "Ignoring training day %s because historical thresholds are inverted at %s: %.2f/%.2f C",
                    day,
                    cursor.isoformat(),
                    winter_below,
                    summer_above,
                )
                slots = []
                break

            if training_mode is None:
                default_mode = "winter" if ch_slot > 0.02 else "summer"
                training_mode = legacy.initial_mode_from_temp_with_thresholds(
                    temp_slot, winter_below, summer_above, default_mode
                )
            training_mode = legacy.update_heating_mode_with_thresholds(
                training_mode, temp_slot, winter_below, summer_above
            )
            hours = (slot_end - cursor).total_seconds() / 3600.0
            dd_slot = (
                legacy.degree_days_for_period(temp_slot, cfg.base_temperature_c, hours)
                if training_mode == "winter"
                else 0.0
            )
            slots.append(
                {
                    "start_ts": cursor.isoformat(),
                    "degree_days": dd_slot,
                    "ch_kwh": ch_slot,
                    "mean_temperature_c": temp_slot,
                    "winter_mode_below_c": winter_below,
                    "summer_mode_above_c": summer_above,
                }
            )
            cursor = slot_end

        if len(slots) != 48:
            LOG.debug("Insufficient 30-minute history for training day %s", day)
            continue

        active_slots = [slot for slot in slots if float(slot["degree_days"]) > 0.0]
        active_dd = sum(float(slot["degree_days"]) for slot in active_slots)
        active_ch = sum(float(slot["ch_kwh"]) for slot in active_slots)
        mean_temp = legacy.time_weighted_mean(temps, day_start, day_end)
        valid = active_dd >= minimum_dd and active_ch >= minimum_ch

        # Store active CH in the daily diagnostic so its displayed ratio matches
        # exactly the observations that can contribute to Store.model().
        store.replace_day(day.isoformat(), slots, active_dd, active_ch, mean_temp, valid)
        used_slots = len(active_slots) if valid else 0
        unique_thresholds = {
            (
                round(float(slot["winter_mode_below_c"]), 2),
                round(float(slot["summer_mode_above_c"]), 2),
            )
            for slot in slots
        }
        threshold_note = ", ".join(
            f"{off:.1f}/{on:.1f}C" for off, on in sorted(unique_thresholds)
        )
        reason = "used" if valid else (
            f"ignored: need {minimum_dd:.2f} active DD and {minimum_ch:.2f} kWh active CH"
        )
        LOG.info(
            "Training day %s: %.2f active DD, %.2f kWh active CH, %.2f kWh/DD, "
            "%d heating slots, thresholds %s (%s)",
            day,
            active_dd,
            active_ch,
            (active_ch / active_dd if active_dd > 0 else 0.0),
            used_slots,
            threshold_note,
            reason,
        )

    cutoff = (now.date() - timedelta(days=cfg.training_days)).isoformat()
    coefficient, days, _, _, slots = store.model(cfg.initial_kwh_per_degree_day, cutoff)
    return coefficient, days, slots
