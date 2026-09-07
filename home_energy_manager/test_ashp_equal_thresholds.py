from pathlib import Path


def test_equal_thresholds_are_accepted_everywhere():
    text = (Path(__file__).parent / "components" / "ashp_forecaster" / "app" / "main.py").read_text()
    # Current HA threshold validation and historical training validation reject only inversion.
    assert text.count("winter_below > summer_above") >= 2
    assert "winter_below >= summer_above" not in text


def test_zero_width_hysteresis_keeps_state_at_exact_threshold():
    source = (Path(__file__).parent / "components" / "ashp_forecaster" / "app" / "main.py").read_text()
    # Strict crossings mean equality retains the previous state.
    assert "temp_c < winter_below_c" in source
    assert "temp_c > summer_above_c" in source
