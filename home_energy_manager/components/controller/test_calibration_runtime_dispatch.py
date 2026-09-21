"""Regression tests for the calibration policy executed by the packaged overlay planner."""
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Europe/London')


class DummyLog:
    def info(self, *_args, **_kwargs):
        pass


class DummyController:
    def __init__(self):
        self.c = {'reserve_dwell_minutes': 15, 'charge_safety_margin_minutes': 10}
        self._calibration_max_charge_w = 6000
        self.state = 'awaiting_deep_low'

    def calibration_state(self):
        return self.state

    def now(self):
        return datetime(2026, 9, 21, 14, 0, tzinfo=TZ)

    def soc_at(self, forecast, when, key):
        assert key == 'forecast_no_slots'
        return forecast['predicted_low_soc']


def _import_path(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / Path(path).name)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(monkeypatch):
    """Mirror the Docker import chain, including the forwarding rollover wrapper.

    The overlay planner executes the function defined in minimise_export_core.
    Assigning to the rollover wrapper creates a shadow attribute instead of
    updating the function resolved by that planner.
    """
    effective = types.ModuleType('minimise_export_core')

    def disabled_discharge(_controller, plan, at):
        plan['discharge'] = {
            'start': at.isoformat(), 'end': at.isoformat(),
            'rate_w': 6000, 'target_soc': 4, 'planned_kwh': 0,
        }

    effective._disabled_discharge = disabled_discharge
    effective._minimise_calibration_discharge = lambda *_args: None

    rollover = types.ModuleType('legacy_minimise_export_runtime')
    rollover.runtime = effective

    def forwarded(name):
        return getattr(effective, name)

    rollover.__getattr__ = forwarded
    overlays = types.SimpleNamespace(legacy_runtime=rollover)
    active = types.ModuleType('active_runtime')
    core = types.SimpleNamespace(
        Controller=type('CoreController', (), {
            '__init__': lambda self: None,
            'sample': lambda self: None,
            'calibration_state': lambda self: 'disabled',
            'calibration_attrs': lambda self: {},
        }),
        LOG=DummyLog(),
        iso=lambda value: value.isoformat(),
        parse_dt=lambda value: datetime.fromisoformat(value) if value else None,
        as_float=lambda value: float(value) if value is not None else None,
        clamp=lambda value, low, high: max(low, min(high, value)),
    )
    active.core = core
    active.runtime = overlays
    monkeypatch.setitem(sys.modules, 'active_runtime', active)
    mqtt = types.ModuleType('common.mqtt_button')
    mqtt.publish_command_button = lambda *_args: True
    monkeypatch.setitem(sys.modules, 'common.mqtt_button', mqtt)

    low = _import_path(monkeypatch, 'manual_low_calibration_runtime',
                       'manual_low_calibration_runtime.py')
    assert effective._minimise_calibration_discharge is low._natural_first_calibration_discharge
    assert '_minimise_calibration_discharge' not in rollover.__dict__

    _import_path(monkeypatch, 'manual_high_calibration_runtime',
                 'manual_high_calibration_runtime.py')
    recharge = _import_path(monkeypatch, 'calibration_charge_runtime',
                            'calibration_charge_runtime.py')
    assert effective._minimise_calibration_discharge is recharge._strategy_with_forecast_recharge
    assert '_minimise_calibration_discharge' not in rollover.__dict__
    return effective, recharge


def test_manual_low_calibration_preprograms_same_window_recharge(runtime):
    policy, recharge = runtime
    controller = DummyController()
    start = datetime(2026, 9, 21, 23, tzinfo=TZ)
    end = datetime(2026, 9, 22, 6, tzinfo=TZ)
    window = {'start': start, 'end': end}
    plan = {'initial_soc': 86, 'charge': {'start': start.isoformat(), 'end': start.isoformat()}}
    forecast = {'predicted_low_soc': 4}

    # Invoke the registered function, not the implementation directly: this
    # reproduces the dispatch performed by the real minimise-export planner.
    diagnostics = policy._minimise_calibration_discharge(
        controller, plan, window, 13.79, 4, 6000, forecast,
    )

    assert policy._minimise_calibration_discharge is recharge._strategy_with_forecast_recharge
    assert diagnostics['strategy'] == 'natural_discharge'
    assert diagnostics['forecast_recharge_target_soc'] == 100
    assert diagnostics['forced_export_required'] is False
    assert plan['pause']['mode'] == 'Disabled'
    assert plan['discharge']['kind'] == 'calibration_natural_depletion'
    assert plan['charge']['kind'] == 'calibration_forecast_recharge'
    assert plan['charge']['target_soc'] == 100
    charge_start = datetime.fromisoformat(plan['charge']['start'])
    charge_end = datetime.fromisoformat(plan['charge']['end'])
    low_at = datetime.fromisoformat(diagnostics['latest_safe_reserve_at'])
    assert low_at < charge_start < charge_end <= end
    assert (charge_start - low_at).total_seconds() >= 14 * 60


def test_no_calibration_request_does_not_force_recharge(runtime):
    policy, _ = runtime
    controller = DummyController()
    controller.state = 'normal'
    start = datetime(2026, 9, 21, 23, tzinfo=TZ)
    plan = {'charge': {'start': start.isoformat(), 'end': start.isoformat()}}
    result = policy._minimise_calibration_discharge(
        controller, plan,
        {'start': start, 'end': datetime(2026, 9, 22, 6, tzinfo=TZ)},
        13.79, 4, 6000, {'predicted_low_soc': 4},
    )
    assert result is None
    assert plan['charge']['start'] == plan['charge']['end']


def test_forecast_shortfall_still_preserves_recharge(runtime):
    policy, _ = runtime
    controller = DummyController()
    start = datetime(2026, 9, 21, 23, tzinfo=TZ)
    end = datetime(2026, 9, 22, 6, tzinfo=TZ)
    plan = {'initial_soc': 86}
    diagnostics = policy._minimise_calibration_discharge(
        controller, plan, {'start': start, 'end': end},
        13.79, 4, 6000, {'predicted_low_soc': 18},
    )
    assert diagnostics['forced_export_required'] is True
    assert plan['discharge']['kind'] == 'calibration_to_reserve'
    assert plan['charge']['kind'] == 'calibration_forecast_recharge'
    assert datetime.fromisoformat(plan['charge']['end']) <= end
