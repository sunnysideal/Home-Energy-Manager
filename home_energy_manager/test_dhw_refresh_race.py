from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _starts():
    start = datetime(2026, 9, 13, 21, 30, tzinfo=timezone.utc)
    return [start + timedelta(minutes=30 * i) for i in range(96)]


def test_transient_incomplete_thermal_horizon_is_retried_until_thermal_arrives(monkeypatch):
    calls = []
    thermal_values = [0.0] * 96
    thermal_values[13] = 1.25

    def fake_select(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("DHW thermal forecast unavailable (thermal_forecast_incomplete)")
        return SelectionResult(thermal_values, "thermal", "thermal_authoritative")

    monkeypatch.setattr(forecast_runner, "select_dhw_forecast", fake_select)
    monkeypatch.setattr(forecast_runner, "THERMAL_REFRESH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(forecast_runner, "THERMAL_REFRESH_POLL_SECONDS", 0.001)

    cfg = SimpleNamespace(update_minutes=5)
    starts = _starts()
    result = forecast_runner._select_dhw_with_refresh_wait(FakeClient(), FakeStore(), cfg, starts)

    assert len(calls) >= 2
    assert result.source == "thermal"
    assert result.reason == "thermal_authoritative"
    assert result.values[13] == 1.25


def test_persistent_thermal_failure_expires_wait(monkeypatch):
    calls = []

    def fake_select(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("DHW thermal model is not structurally ready")

    monotonic_values = iter([0.0, 0.0, 0.1])
    monkeypatch.setattr(forecast_runner, "select_dhw_forecast", fake_select)
    monkeypatch.setattr(forecast_runner, "THERMAL_REFRESH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(forecast_runner.time, "monotonic", lambda: next(monotonic_values, 0.1))
    monkeypatch.setattr(forecast_runner.time, "sleep", lambda _: None)

    cfg = SimpleNamespace(update_minutes=5)
    with pytest.raises(RuntimeError, match="thermal production forecast unavailable"):
        forecast_runner._select_dhw_with_refresh_wait(FakeClient(), FakeStore(), cfg, _starts())

    assert len(calls) >= 1
