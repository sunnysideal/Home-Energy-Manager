"""Shadow-only 48-hour DHW thermal forecast.

The output from this module is for validation/diagnostics only. It must not replace the
legacy ASHP forecast until confidence and compatibility promotion criteria are met.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3

from dhw_simulator import StepInputs, TankParameters, TankState, step_tank

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
        upper_loss_w_per_k=params.get("dhw_upper_loss_w_per_k", 1.2),
        lower_loss_w_per_k=params.get("dhw_lower_loss_w_per_k", 0.8),
        coupling_w_per_k=params.get("dhw_coupling_w_per_k", 3.0),
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
    """Simulate DHW tank state and electrical demand for the full requested horizon."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if horizon_hours <= 0 or step_minutes <= 0:
        raise ValueError("horizon and step must be positive")

    params = TankParameters(
        volume_l=tank_volume_l,
        upper_loss_w_per_k=model.upper_loss_w_per_k,
        lower_loss_w_per_k=model.lower_loss_w_per_k,
        coupling_w_per_k=model.coupling_w_per_k,
        minimum_useful_temperature_c=minimum_useful_temperature_c,
    )
    state = TankState(initial_upper_c, initial_lower_c)
    remaining_cycle_kwh = 0.0
    out: list[ShadowSlot] = []
    steps = int(horizon_hours * 60 / step_minutes)
    mode = mode.strip().lower()

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
        if remaining_cycle_kwh <= 1e-9 and opportunity and state.upper_temp_c < target_temp_c - hysteresis_c:
            remaining_cycle_kwh = model.cycle_energy(state.upper_temp_c, state.lower_temp_c, target_temp_c)

        electrical_step = 0.0
        if remaining_cycle_kwh > 0.0 and opportunity:
            electrical_step = min(remaining_cycle_kwh, model.typical_power_kw * step_minutes / 60.0)
            remaining_cycle_kwh -= electrical_step
            state = TankState(
                upper_temp_c=min(target_temp_c + 5.0, state.upper_temp_c + electrical_step * model.upper_c_per_kwh),
                lower_temp_c=min(target_temp_c + 5.0, state.lower_temp_c + electrical_step * model.lower_c_per_kwh),
            )
            if state.upper_temp_c >= target_temp_c:
                remaining_cycle_kwh = 0.0

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
