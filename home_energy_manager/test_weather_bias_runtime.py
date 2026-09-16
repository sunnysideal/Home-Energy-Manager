from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNNER = (ROOT / "components" / "ashp_forecaster" / "app" / "forecast_runner.py").read_text(encoding="utf-8")
OBSERVATIONS = (ROOT / "components" / "ashp_forecaster" / "app" / "weather_observations.py").read_text(encoding="utf-8")


def test_shadow_weather_bias_is_diagnostic_only() -> None:
    assert '"calibration_applied": False' in RUNNER
    assert '"phase": "shadow_bias_learning"' in RUNNER
    assert 'shadow_weather_calibration(store.db, now=now)' in RUNNER
    assert 'global_shadow_correction_c' in RUNNER
    assert 'shadow_corrected_mae_c' in RUNNER
    assert 'shadow_improvement_pct' in RUNNER


def test_weather_bias_logging_includes_summary_and_horizons() -> None:
    assert 'Weather bias shadow: window=%dd samples=%d' in RUNNER
    assert 'global_median=%sC' in RUNNER
    assert 'raw_MAE=%sC shadow_MAE=%sC improvement=%s%%' in RUNNER
    assert 'Weather bias shadow horizons: %s' in RUNNER
    assert "source={result['correction_source']}" in RUNNER


def test_shadow_learner_has_safety_floors_and_clamp() -> None:
    assert 'DEFAULT_CALIBRATION_WINDOW_DAYS = 30' in OBSERVATIONS
    assert 'DEFAULT_MIN_GLOBAL_SAMPLES = 24' in OBSERVATIONS
    assert 'DEFAULT_MIN_HORIZON_SAMPLES = 12' in OBSERVATIONS
    assert 'DEFAULT_MAX_ABS_BIAS_C = 5.0' in OBSERVATIONS
    assert 'correction_source = "global_fallback"' in OBSERVATIONS
    assert 'correction_source = "insufficient_samples"' in OBSERVATIONS
