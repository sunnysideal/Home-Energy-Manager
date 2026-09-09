import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_simulator.py"

spec = importlib.util.spec_from_file_location("dhw_simulator", MODULE_PATH)
sim = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = sim
spec.loader.exec_module(sim)


def test_passive_tank_cools():
    params = sim.TankParameters(coupling_w_per_k=0.0)
    state = sim.TankState(52.0, 45.0)
    result = sim.step_tank(state, params, sim.StepInputs(ambient_temp_c=20.0), minutes=60)
    assert result.upper_temp_c < state.upper_temp_c
    assert result.lower_temp_c < state.lower_temp_c


def test_zone_coupling_moves_heat_downward():
    params = sim.TankParameters(upper_loss_w_per_k=0.0, lower_loss_w_per_k=0.0, coupling_w_per_k=10.0)
    state = sim.TankState(55.0, 30.0)
    result = sim.step_tank(state, params, sim.StepInputs(), minutes=60)
    assert result.upper_temp_c < 55.0
    assert result.lower_temp_c > 30.0


def test_small_draw_cools_lower_zone_first():
    params = sim.TankParameters(mains_temperature_c=10.0)
    state = sim.TankState(52.0, 42.0)
    result = sim.step_tank(
        state,
        params,
        sim.StepInputs(ambient_temp_c=42.0, draw_kwh=1.0),
        minutes=0.001,
    )
    assert abs(result.upper_temp_c - 52.0) < 0.01
    assert result.lower_temp_c < 42.0


def test_large_draw_eventually_affects_upper_zone():
    params = sim.TankParameters(mains_temperature_c=10.0, upper_loss_w_per_k=0, lower_loss_w_per_k=0, coupling_w_per_k=0)
    state = sim.TankState(52.0, 42.0)
    result = sim.step_tank(state, params, sim.StepInputs(draw_kwh=8.0), minutes=5)
    assert result.lower_temp_c == 10.0
    assert result.upper_temp_c < 52.0


def test_heating_raises_both_effective_zones():
    params = sim.TankParameters(upper_loss_w_per_k=0, lower_loss_w_per_k=0, coupling_w_per_k=0)
    state = sim.TankState(40.0, 30.0)
    result = sim.step_tank(state, params, sim.StepInputs(heating_kwh=2.0), minutes=5)
    assert result.upper_temp_c > 40.0
    assert result.lower_temp_c > 30.0


def test_inverted_tank_is_mixed_without_losing_sensible_heat():
    params = sim.TankParameters(upper_fraction=0.45)
    state = sim.TankState(45.0, 60.0)
    before = (
        state.upper_temp_c * params.upper_volume_l
        + state.lower_temp_c * params.lower_volume_l
    )
    result = sim.stabilise_stratification(state, params)
    after = (
        result.upper_temp_c * params.upper_volume_l
        + result.lower_temp_c * params.lower_volume_l
    )
    assert result.upper_temp_c == result.lower_temp_c
    assert abs(after - before) < 1e-9


def test_step_never_leaves_lower_zone_hotter_than_upper():
    params = sim.TankParameters(
        upper_loss_w_per_k=0.0,
        lower_loss_w_per_k=0.0,
        coupling_w_per_k=0.0,
        heating_upper_fraction=0.10,
    )
    state = sim.TankState(50.0, 49.0)
    result = sim.step_tank(state, params, sim.StepInputs(heating_kwh=2.0), minutes=5)
    assert result.lower_temp_c <= result.upper_temp_c


def test_usable_energy_counts_only_energy_above_minimum_temperature():
    params = sim.TankParameters(minimum_useful_temperature_c=40.0)
    state = sim.TankState(50.0, 35.0)
    expected = params.upper_volume_l * sim.WATER_KWH_PER_LITRE_C * 10.0
    assert abs(sim.usable_energy_kwh(state, params) - expected) < 1e-9


def test_simulator_supports_full_48_hour_horizon_at_five_minutes():
    params = sim.TankParameters()
    inputs = [sim.StepInputs() for _ in range(48 * 12)]
    states = sim.simulate(sim.TankState(50.0, 45.0), params, inputs, minutes=5)
    assert len(states) == 576
    assert states[-1].upper_temp_c < states[0].upper_temp_c
