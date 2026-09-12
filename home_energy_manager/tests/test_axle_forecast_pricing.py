from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "components" / "home_forecaster" / "app" / "axle_pricing_runner.py"
LAUNCHER = ROOT / "launcher.py"
DOCKERFILE = ROOT / "Dockerfile"


def test_axle_pricing_is_forecast_only_not_physical_control():
    text = RUNNER.read_text()
    assert 'AXLE_EXPORT_REWARD_P_PER_KWH = 100.0' in text
    assert 'event_type") or "").lower() != "export"' in text
    assert '"physical_plan_modified": False' in text
    assert 'client.publish' not in text


def test_axle_pricing_reprices_both_programmed_and_no_slots_forecasts():
    text = RUNNER.read_text()
    assert '_reprice_slots(forecast, event_start, event_end, reward)' in text
    assert '_reprice_slots(no_slots, event_start, event_end, reward)' in text
    assert 'base_export_rate_p' in text
    assert 'axle_overlap_minutes' in text


def test_partial_overlap_is_weighted_not_whole_slot_reward():
    text = RUNNER.read_text()
    assert 'normal * (1.0 - overlap_fraction) + reward_rate * overlap_fraction' in text


def test_home_forecaster_runs_through_axle_pricing_and_health_wrappers():
    dockerfile = DOCKERFILE.read_text()
    assert '/app/runtime/home_forecaster/axle_pricing_runner.py' in LAUNCHER.read_text()
    assert 'COPY components/home_forecaster/app/axle_pricing_runner.py /app/runtime/home_forecaster/axle_pricing_core.py' in dockerfile
    assert 'COPY components/home_forecaster/app/health_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py' in dockerfile
