from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "ashp_forecaster" / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import forecast_runner
from dhw_production_selector import SelectionResult


class FakeStore:
    db = object()


class FakeClient:
    def get_state(self, entity_id):
        return {}


def test_transient_incomplete_thermal_horizon_is_retried_before_legacy_fallback(monkeypatch):
    calls = []
    thermal_values = [0.0] * 96
    thermal_values[13] = 1.25

    def fake_select(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SelectionResult([0.0] * 96, "legacy", "thermal_forecast_incomplete")
        return SelectionResult(thermal_values, "thermal", "thermal_trial_active")

    monkeypatch.setattr(forecast_runner, "select_dhw_forecast", fake_select)
    monkeypatch.setattr(forecast_runner, "THERMAL_REFRESH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(forecast_runner, "THERMAL_REFRESH_POLL_SECONDS", 0.001)

    cfg = SimpleNamespace(update_minutes=5)
    starts = list(range(96))
    result = forecast_runner._select_dhw_with_refresh_wait(
        FakeClient(), FakeStore(), cfg, starts, [0.0] * 96
    )

    assert len(calls) >= 2
    assert result.source == "thermal"
    assert result.values[13] == 1.25


def test_non_transient_selector_decision_is_not_delayed(monkeypatch):
    calls = []

    def fake_select(*args, **kwargs):
        calls.append(1)
        return SelectionResult([0.2] * 96, "legacy", "trial_not_ready")

    monkeypatch.setattr(forecast_runner, "select_dhw_forecast", fake_select)
    cfg = SimpleNamespace(update_minutes=5)
    result = forecast_runner._select_dhw_with_refresh_wait(
        FakeClient(), FakeStore(), cfg, list(range(96)), [0.2] * 96
    )

    assert len(calls) == 1
    assert result.source == "legacy"
    assert result.reason == "trial_not_ready"
