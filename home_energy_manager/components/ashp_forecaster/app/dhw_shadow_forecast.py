"""48-hour DHW thermal forecast used for validation and production forecasting.

The thermal model is authoritative for DHW heating energy.  A scheduled/on-mode
heating cycle is simulated until the configured target temperature is reached; the
required electrical energy is therefore an output of the simulated tank state rather
than a pre-computed historical cycle-energy cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

from dhw_simulator import StepInputs, TankParameters, TankState, stabilise_stratification, step_tank

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class ShadowModel:
    intercept_kwh: float
    upper_kwh_per_c: float
    lower_kwh_per_c: float
    typical_power_kw: float
    upper_c_per_kwh: float
    lower_c_per_kwh: float
    upper_loss_w_per_k: float
    lower_loss_w_per_k: float
    coupling_w_per_k: float
    demand: dict[tuple[str, int], float]

    def cycle_energy(self, upper_temp_c: float, lower_temp_c: float, target_temp_c: float) -> float:
        """Historical comparator only; production heating is target-seeking.

        The learned cycle regression remains available for diagnostics/validation, but
        it must never cap a simulated production heating cycle.
        """
        upper_deficit = max(target_temp_c - upper_temp_c, 0.0)
        lower_deficit = max(target_temp_c - lower_temp_c, 0.0)
        return max(
            0.0,
            self.intercept_kwh
            + self.upper_kwh_per_c * upper_deficit
            + self.lower_kwh_per_c * lower_deficit,
        )


@dataclass(frozen=True)
class ShadowSlot:
    start: datetime
    upper_temp_c: float
    lower_temp_c: float
    draw_kwh: float
    dhw_kwh: float
    heating: bool


def load_shadow_model(db: sqlite3.Connection) -> ShadowModel | None:
    params = {
        str(name): float(value)
        for name, value in db.execute("SELECT name,value FROM dhw_model_parameters")
    }
    required = [
        "dhw_cycle_intercept_kwh",
        "dhw_cycle_upper_kwh_per_c",
        "dhw_cycle_lower_kwh_per_c",
        "dhw_cycle_power_kw",
        "dhw_cycle_upper_c_per_kwh",
        "dhw_cycle_lower_c_per_kwh",
    ]
    if any(name not in params for name in required):
        return None
    demand = {
        (str(day_type), int(slot_index)): max(0.0, float(expected_kwh))
        for day_type, slot_index, expected_kwh in db.execute(
            "SELECT day_type,slot_index,expected_kwh FROM dhw_demand_profile"
        )
    }
    return ShadowModel(
        intercept_kwh=params["dhw_cycle_intercept_kwh"],
        upper_kwh_per_c=params["dhw_cycle_upper_kwh_per_c"],
        lower_kwh_per_c=params["dhw_cycle_lower_kwh_per_c"],
        typical_power_kw=params["dhw_cycle_power_kw"],
        upper_c_per_kwh=params["dhw_cycle_upper_c_per_kwh"],
        lower_c_per_kwh=params["dhw_cycle_lower_c_per_kwh"],
        upper_loss_w_per_k=params.get("dhw_upper_loss_w_per_k", params.get("upper_loss_w_per_k", 1.2)),
        lower_loss_w_per_k=params.get("dhw_lower_loss_w_per_k", params.get("lower_loss_w_per_k", 0.8)),
        coupling_w_per_k=params.get("dhw_coupling_w_per_k", params.get("coupling_w_per_k", 3.0)),
        demand=demand,
    )


def schedule_enabled(schedule_bits: dict[str, int], local_dt: datetime) -> bool:
    weekday = WEEKDAYS[local_dt.weekday()]
    half = "am" if local_dt.hour < 12 else "pm"
    key = f"{weekday}_{half}"
    value = int(schedule_bits.get(key, 0))
    absolute_half_hour = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
    bit = absolute_half_hour if absolute_half_hour < 24 else absolute_half_hour - 24
    return bool(value & (1 << bit))


def _day_type(local_dt: datetime) -> str:
    return "weekend" if local_dt.weekday() >= 5 else "weekday"


def _expected_draw_step(model: ShadowModel, local_dt: datetime, step_minutes: int) -> float:
    slot = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
    half_hour_expected = model.demand.get((_day_type(local_dt), slot), 0.0)
    return half_hour_expected * step_minutes / 30.0


def _apply_learned_heating(
    state: TankState,
    model: ShadowModel,
    params: TankParameters,
    electrical_kwh: float,
) -> TankState:
    """Apply the learned sensor response for an electrical heating input."""
    if electrical_kwh <= 0.0:
        return state
    heated = TankState(
        upper_temp_c=state.upper_temp_c + electrical_kwh * model.upper_c_per_kwh,
        lower_temp_c=state.lower_temp_c + electrical_kwh * model.lower_c_per_kwh,
    )
    return stabilise_stratification(heated, params)


def _energy_to_target_this_step(
    state: TankState,
    model: ShadowModel,
    params: TankParameters,
    target_temp_c: float,
    max_step_kwh: float,
) -> tuple[float, TankState]:
    """Return the smallest electrical input this step that reaches the target.

    If the target cannot be reached within the step's learned power limit, use the full
    step energy.  A binary solve is used because stabilisation can mix the two zones and
    therefore makes the effective upper-temperature response piecewise linear.
    """
    if state.upper_temp_c >= target_temp_c:
        return 0.0, state
    full_state = _apply_learned_heating(state, model, params, max_step_kwh)
    if full_state.upper_temp_c < target_temp_c:
        return max_step_kwh, full_state

    lo = 0.0
    hi = max_step_kwh
    for _ in range(32):
        mid = (lo + hi) / 2.0
        candidate = _apply_learned_heating(state, model, params, mid)
        if candidate.upper_temp_c >= target_temp_c:
            hi = mid
        else:
            lo = mid
    energy = hi
    reached = _apply_learned_heating(state, model, params, energy)
    return energy, TankState(target_temp_c, min(reached.lower_temp_c, target_temp_c))


def build_shadow_forecast(
    *,
    start: datetime,
    initial_upper_c: float,
    initial_lower_c: float,
    target_temp_c: float,
    hysteresis_c: float,
    mode: str,
    schedule_bits: dict[str, int],
    model: ShadowModel,
    tank_volume_l: float = 250.0,
    ambient_temp_c: float = 20.0,
    minimum_useful_temperature_c: float = 40.0,
    horizon_hours: int = 48,
    step_minutes: int = 5,
) -> list[ShadowSlot]:
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if horizon_hours <= 0 or step_minutes <= 0:
        raise ValueError("horizon and step must be positive")
    if model.typical_power_kw <= 0.0:
        raise ValueError("learned DHW heating power must be positive")
    if model.upper_c_per_kwh <= 0.0:
        raise ValueError("learned DHW upper temperature response must be positive")

    params = TankParameters(
        volume_l=tank_volume_l,
        upper_loss_w_per_k=model.upper_loss_w_per_k,
        lower_loss_w_per_k=model.lower_loss_w_per_k,
        coupling_w_per_k=model.coupling_w_per_k,
        minimum_useful_temperature_c=minimum_useful_temperature_c,
    )
    state = stabilise_stratification(TankState(initial_upper_c, initial_lower_c), params)
    heating_cycle_active = False
    out: list[ShadowSlot] = []
    steps = int(horizon_hours * 60 / step_minutes)
    mode = mode.strip().lower()
    max_step_kwh = model.typical_power_kw * step_minutes / 60.0

    for index in range(steps):
        local_dt = start + timedelta(minutes=index * step_minutes)
        draw_kwh = _expected_draw_step(model, local_dt, step_minutes)
        state = step_tank(
            state,
            params,
            StepInputs(ambient_temp_c=ambient_temp_c, draw_kwh=draw_kwh),
            minutes=step_minutes,
        )

        opportunity = mode == "on" or (mode == "schedule" and schedule_enabled(schedule_bits, local_dt))
        if mode == "off":
            opportunity = False

        if not opportunity:
            # A schedule window is an actual heating opportunity.  Do not invent heating
            # outside it; the next enabled window may start a fresh target-seeking cycle.
            heating_cycle_active = False
        elif not heating_cycle_active and state.upper_temp_c < target_temp_c - hysteresis_c:
            heating_cycle_active = True

        electrical_step = 0.0
        if heating_cycle_active and opportunity:
            electrical_step, state = _energy_to_target_this_step(
                state,
                model,
                params,
                target_temp_c,
                max_step_kwh,
            )
            if state.upper_temp_c >= target_temp_c - 1e-6:
                heating_cycle_active = False

        out.append(
            ShadowSlot(
                start=local_dt,
                upper_temp_c=state.upper_temp_c,
                lower_temp_c=state.lower_temp_c,
                draw_kwh=draw_kwh,
                dhw_kwh=electrical_step,
                heating=electrical_step > 0.0,
            )
        )
    return out


def _half_hour_start(dt: datetime) -> datetime:
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def persist_shadow_validation(
    db: sqlite3.Connection,
    slots: list[ShadowSlot],
    *,
    forecast_ts: datetime | None = None,
    source: str = "thermal_shadow",
    keep_days: int = 21,
    step_minutes: int = 5,
    legacy_dhw_by_start: dict[str, float] | None = None,
) -> int:
    """Store complete clock-aligned 30-minute intervals for like-for-like scoring."""
    if not slots:
        return 0
    forecast_ts = forecast_ts or datetime.now(timezone.utc)
    if forecast_ts.tzinfo is None:
        forecast_ts = forecast_ts.replace(tzinfo=timezone.utc)
    forecast_iso = forecast_ts.astimezone(timezone.utc).isoformat()
    legacy_dhw_by_start = legacy_dhw_by_start or {}
    steps_per_checkpoint = max(1, int(round(30 / step_minutes)))

    groups: dict[datetime, list[ShadowSlot]] = {}
    for slot in slots:
        groups.setdefault(_half_hour_start(slot.start), []).append(slot)

    written = 0
    with db:
        for interval_start in sorted(groups):
            chunk = sorted(groups[interval_start], key=lambda s: s.start)
            expected_starts = [interval_start + timedelta(minutes=i * step_minutes) for i in range(steps_per_checkpoint)]
            if len(chunk) != steps_per_checkpoint or [slot.start for slot in chunk] != expected_starts:
                continue
            endpoint = chunk[-1]
            target_time = interval_start + timedelta(minutes=30)
            target_iso = target_time.astimezone(timezone.utc).isoformat()
            slot_start_iso = interval_start.astimezone(timezone.utc).isoformat()
            predicted_dhw = sum(slot.dhw_kwh for slot in chunk)
            legacy_dhw = legacy_dhw_by_start.get(slot_start_iso)
            db.execute(
                "INSERT INTO dhw_forecast_validation("
                "forecast_ts,target_ts,predicted_upper_c,predicted_lower_c,predicted_dhw_kwh,"
                "legacy_dhw_kwh,actual_dhw_kwh,actual_upper_c,actual_lower_c,model_source"
                ") VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(forecast_ts,target_ts) DO UPDATE SET "
                "predicted_upper_c=excluded.predicted_upper_c,"
                "predicted_lower_c=excluded.predicted_lower_c,"
                "predicted_dhw_kwh=excluded.predicted_dhw_kwh,"
                "legacy_dhw_kwh=excluded.legacy_dhw_kwh,model_source=excluded.model_source",
                (
                    forecast_iso, target_iso, endpoint.upper_temp_c, endpoint.lower_temp_c,
                    predicted_dhw, legacy_dhw, None, None, None, source,
                ),
            )
            written += 1
        cutoff = (forecast_ts.astimezone(timezone.utc) - timedelta(days=max(1, keep_days))).isoformat()
        db.execute("DELETE FROM dhw_forecast_validation WHERE forecast_ts<?", (cutoff,))
    return written
