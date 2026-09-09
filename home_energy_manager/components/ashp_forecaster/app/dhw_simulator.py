"""Two-zone DHW cylinder simulator used by the ASHP forecaster.

The model is intentionally simple: two effective thermal zones, standing losses,
inter-zone coupling, plug-flow-style cold-water displacement and explicit thermal
heating input.  It is designed for 5-minute stepping and can be reset from live
sensor values on every forecast run.
"""
from __future__ import annotations

from dataclasses import dataclass

WATER_KWH_PER_LITRE_C = 4.186 / 3600.0


@dataclass(frozen=True)
class TankParameters:
    volume_l: float = 250.0
    upper_fraction: float = 0.45
    upper_loss_w_per_k: float = 1.2
    lower_loss_w_per_k: float = 0.8
    coupling_w_per_k: float = 3.0
    heating_upper_fraction: float = 0.35
    mains_temperature_c: float = 10.0
    minimum_useful_temperature_c: float = 40.0

    def __post_init__(self) -> None:
        if not 0.1 <= self.upper_fraction <= 0.9:
            raise ValueError("upper_fraction must be between 0.1 and 0.9")
        if not 0.0 <= self.heating_upper_fraction <= 1.0:
            raise ValueError("heating_upper_fraction must be between 0 and 1")
        if self.volume_l <= 0:
            raise ValueError("volume_l must be positive")

    @property
    def upper_volume_l(self) -> float:
        return self.volume_l * self.upper_fraction

    @property
    def lower_volume_l(self) -> float:
        return self.volume_l - self.upper_volume_l


@dataclass(frozen=True)
class TankState:
    upper_temp_c: float
    lower_temp_c: float


@dataclass(frozen=True)
class StepInputs:
    ambient_temp_c: float = 20.0
    draw_kwh: float = 0.0
    heating_kwh: float = 0.0


def _capacity_kwh_per_c(volume_l: float) -> float:
    return volume_l * WATER_KWH_PER_LITRE_C


def stabilise_stratification(state: TankState, params: TankParameters) -> TankState:
    """Remove an unstable lower-hotter-than-upper inversion without losing energy.

    A vertical DHW cylinder cannot sustain a meaningful thermal inversion: buoyancy
    rapidly mixes hotter lower water upward.  The two-zone model therefore collapses an
    inverted state to the volume-weighted mixed temperature.  Weighting by zone volume
    preserves the represented sensible heat (same water specific heat in both zones).
    """
    if state.lower_temp_c <= state.upper_temp_c:
        return state
    total_v = params.upper_volume_l + params.lower_volume_l
    mixed = (
        state.upper_temp_c * params.upper_volume_l
        + state.lower_temp_c * params.lower_volume_l
    ) / total_v
    return TankState(mixed, mixed)


def usable_energy_kwh(state: TankState, params: TankParameters) -> float:
    """Approximate stored thermal energy above the configured useful temperature."""
    upper = _capacity_kwh_per_c(params.upper_volume_l) * max(
        state.upper_temp_c - params.minimum_useful_temperature_c, 0.0
    )
    lower = _capacity_kwh_per_c(params.lower_volume_l) * max(
        state.lower_temp_c - params.minimum_useful_temperature_c, 0.0
    )
    return upper + lower


def _apply_draw(state: TankState, draw_kwh: float, params: TankParameters) -> TankState:
    """Apply a plug-flow-style draw: mains enters low and displaces water upward."""
    if draw_kwh <= 0.0:
        return state
    outlet_delta = max(state.upper_temp_c - params.mains_temperature_c, 1.0)
    draw_l = min(draw_kwh / (WATER_KWH_PER_LITRE_C * outlet_delta), params.volume_l)
    old_upper = state.upper_temp_c
    old_lower = state.lower_temp_c
    lower_v = params.lower_volume_l
    upper_v = params.upper_volume_l

    if draw_l <= lower_v:
        lower = ((lower_v - draw_l) * old_lower + draw_l * params.mains_temperature_c) / lower_v
        return TankState(old_upper, lower)

    remaining = min(draw_l - lower_v, upper_v)
    upper = ((upper_v - remaining) * old_upper + remaining * old_lower) / upper_v
    return TankState(upper, params.mains_temperature_c)


def step_tank(
    state: TankState,
    params: TankParameters,
    inputs: StepInputs,
    *,
    minutes: float = 5.0,
) -> TankState:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    hours = minutes / 60.0

    state = stabilise_stratification(_apply_draw(state, max(inputs.draw_kwh, 0.0), params), params)
    upper = state.upper_temp_c
    lower = state.lower_temp_c
    upper_cap = _capacity_kwh_per_c(params.upper_volume_l)
    lower_cap = _capacity_kwh_per_c(params.lower_volume_l)

    upper_loss_kwh = max(upper - inputs.ambient_temp_c, 0.0) * params.upper_loss_w_per_k * hours / 1000.0
    lower_loss_kwh = max(lower - inputs.ambient_temp_c, 0.0) * params.lower_loss_w_per_k * hours / 1000.0
    upper -= upper_loss_kwh / upper_cap
    lower -= lower_loss_kwh / lower_cap

    # In normal stratification, slow conductive/circulatory coupling moves heat downward.
    coupling_kwh = max(upper - lower, 0.0) * params.coupling_w_per_k * hours / 1000.0
    upper -= coupling_kwh / upper_cap
    lower += coupling_kwh / lower_cap

    heat = max(inputs.heating_kwh, 0.0)
    upper += (heat * params.heating_upper_fraction) / upper_cap
    lower += (heat * (1.0 - params.heating_upper_fraction)) / lower_cap

    return stabilise_stratification(TankState(upper, lower), params)


def simulate(
    initial: TankState,
    params: TankParameters,
    inputs: list[StepInputs],
    *,
    minutes: float = 5.0,
) -> list[TankState]:
    """Return one tank state per input step, suitable for a 48-hour horizon."""
    state = stabilise_stratification(initial, params)
    out: list[TankState] = []
    for item in inputs:
        state = step_tank(state, params, item, minutes=minutes)
        out.append(state)
    return out
