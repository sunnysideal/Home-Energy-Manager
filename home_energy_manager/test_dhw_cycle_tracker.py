import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_cycle_tracker.py"

spec = importlib.util.spec_from_file_location("dhw_cycle_tracker", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def s(minutes, upper, lower, heating, energy, outdoor=8.0, target=50.0, immersion=False, valid=True):
    return mod.CycleSample(
        timestamp=datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        upper_temp_c=upper,
        lower_temp_c=lower,
        dhw_heating=heating,
        dhw_energy_total_kwh=energy,
        outdoor_temp_c=outdoor,
        target_temp_c=target,
        immersion_heating=immersion,
        valid=valid,
    )


def normal_cycle_samples():
    return [
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3),
        s(10, 46.5, 38.0, True, 100.8),
        s(15, 49.0, 42.0, True, 101.4),
        s(20, 51.0, 45.0, False, 101.6),
    ]


def test_complete_cycle_is_identified():
    cycle = mod.build_cycle(normal_cycle_samples())
    assert cycle is not None
    assert cycle.start_ts == s(5, 0, 0, True, 0).timestamp
    assert cycle.end_ts == s(20, 0, 0, False, 0).timestamp
    assert abs(cycle.electrical_kwh - 1.6) < 1e-9
    assert cycle.start_upper_c == 45.0
    assert cycle.end_lower_c == 45.0
    assert cycle.duration_minutes == 15.0
    assert cycle.cycle_type == "normal_dhw"
    assert cycle.valid is True


def test_high_target_cycle_is_classified_separately():
    samples = normal_cycle_samples()
    samples = [
        mod.CycleSample(**{**sample.__dict__, "target_temp_c": 60.0})
        for sample in samples
    ]
    cycle = mod.build_cycle(samples)
    assert cycle is not None
    assert cycle.cycle_type == "high_temp_dhw"
    assert cycle.valid is True


def test_immersion_overlap_is_excluded_from_normal_learning():
    samples = normal_cycle_samples()
    samples[2] = mod.CycleSample(**{**samples[2].__dict__, "immersion_heating": True})
    cycle = mod.build_cycle(samples)
    assert cycle is not None
    assert cycle.cycle_type == "immersion"
    assert cycle.valid is False


def test_target_change_during_cycle_is_invalid():
    samples = normal_cycle_samples()
    samples[2] = mod.CycleSample(**{**samples[2].__dict__, "target_temp_c": 55.0})
    cycle = mod.build_cycle(samples)
    assert cycle is not None
    assert cycle.cycle_type == "normal_dhw"
    assert cycle.valid is False


def test_partial_cycle_without_off_transition_is_rejected():
    assert mod.build_cycle(normal_cycle_samples()[:3]) is None


def test_large_sampling_gap_is_rejected():
    assert mod.build_cycle([
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3),
        s(30, 49.0, 42.0, False, 101.4),
    ]) is None


def test_cumulative_energy_reset_is_rejected():
    assert mod.build_cycle([
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3),
        s(10, 49.0, 42.0, False, 2.0),
    ]) is None


def test_invalid_sample_rejects_cycle():
    assert mod.build_cycle([
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3, valid=False),
        s(10, 49.0, 42.0, False, 101.4),
    ]) is None
