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


def s(minutes, upper, lower, heating, energy, outdoor=8.0, valid=True):
    return mod.CycleSample(
        datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        upper,
        lower,
        heating,
        energy,
        outdoor,
        valid,
    )


def test_complete_cycle_is_identified():
    cycle = mod.build_cycle([
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3),
        s(10, 46.5, 38.0, True, 100.8),
        s(15, 49.0, 42.0, True, 101.4),
        s(20, 51.0, 45.0, False, 101.6),
    ])
    assert cycle is not None
    assert cycle.start_ts == s(5, 0, 0, True, 0).timestamp
    assert cycle.end_ts == s(20, 0, 0, False, 0).timestamp
    assert abs(cycle.electrical_kwh - 1.6) < 1e-9
    assert cycle.start_upper_c == 45.0
    assert cycle.end_lower_c == 45.0
    assert cycle.duration_minutes == 15.0


def test_partial_cycle_without_off_transition_is_rejected():
    assert mod.build_cycle([
        s(0, 45.0, 35.0, False, 100.0),
        s(5, 45.2, 35.5, True, 100.3),
        s(10, 46.5, 38.0, True, 100.8),
    ]) is None


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
