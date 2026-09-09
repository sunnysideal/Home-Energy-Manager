import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "components" / "ashp_forecaster" / "app" / "dhw_draw_detector.py"

spec = importlib.util.spec_from_file_location("dhw_draw_detector", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def sample(minutes, upper, lower, heating=False, valid=True):
    return mod.ThermalSample(
        datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        upper,
        lower,
        heating,
        valid,
    )


def test_small_passive_temperature_change_is_not_a_draw():
    assert mod.detect_draw(sample(0, 51.0, 44.0), sample(5, 50.95, 43.92)) is None


def test_lower_zone_drop_is_detected_as_likely_draw():
    event = mod.detect_draw(sample(0, 51.0, 44.0), sample(5, 50.9, 41.5))
    assert event is not None
    assert event.estimated_thermal_kwh > 0.3
    assert 0.0 < event.confidence <= 0.95


def test_large_draw_can_affect_upper_zone_too():
    event = mod.detect_draw(sample(0, 51.0, 44.0), sample(5, 48.0, 35.0))
    assert event is not None
    assert event.estimated_thermal_kwh > 1.0


def test_heating_period_is_never_classified_as_draw():
    assert mod.detect_draw(sample(0, 51.0, 44.0), sample(5, 50.0, 39.0, heating=True)) is None


def test_long_gap_is_rejected():
    assert mod.detect_draw(sample(0, 51.0, 44.0), sample(30, 50.0, 39.0)) is None
