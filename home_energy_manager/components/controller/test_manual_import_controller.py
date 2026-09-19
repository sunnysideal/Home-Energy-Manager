"""Focused tests for cheaper explicit manual import overrides."""
from datetime import datetime, timedelta, timezone
from controller_manual_import import qualifying_windows, covered_segments


def at(hour):
    return datetime(2026, 9, 20, hour, tzinfo=timezone.utc)


def test_strictly_cheaper_and_expiry():
    state = {'attributes': {'manual_import_overrides': [
        {'start': at(10).isoformat(), 'end': at(12).isoformat(), 'rate_p': 6.99},
        {'start': at(12).isoformat(), 'end': at(14).isoformat(), 'rate_p': 0},
        {'start': at(8).isoformat(), 'end': at(9).isoformat(), 'rate_p': 0},
    ]}}
    result = qualifying_windows(state, 6.99, at(11))
    assert len(result) == 1
    assert result[0]['start'] == at(12)
    assert result[0]['rate_p'] == 0


def test_overlaps_choose_cheapest_and_merge():
    state = {'attributes': {'manual_import_overrides': [
        {'start': at(10).isoformat(), 'end': at(14).isoformat(), 'rate_p': 3},
        {'start': at(11).isoformat(), 'end': at(12).isoformat(), 'rate_p': 0},
    ]}}
    result = qualifying_windows(state, 7, at(9))
    assert [(x['start'].hour, x['end'].hour, x['rate_p']) for x in result] == [
        (10, 11, 3), (11, 12, 0), (12, 14, 3)]


def test_invalid_windows_ignored():
    state = {'attributes': {'manual_import_overrides': [
        {'start': 'bad', 'end': at(12).isoformat(), 'rate_p': 0},
        {'start': at(12).isoformat(), 'end': at(10).isoformat(), 'rate_p': 0},
        {'start': at(10).isoformat(), 'end': at(12).isoformat(), 'rate_p': float('nan')},
    ]}}
    assert qualifying_windows(state, 7, at(9)) == []


def test_incomplete_bridge_fails_closed():
    class Fake:
        def forecast_net_segments(self, _state, _start, _end, _name):
            return [(at(10), at(11), 1, 0)]
    assert covered_segments(Fake(), {}, at(10), at(12)) is None


import asyncio
from controller_manual_import import apply_cheaper_manual_override


def test_future_override_defers_only_safe_charge():
    class Fake:
        c = {'safety_buffer_soc': 20, 'battery_soc_entity': 'soc'}
        def now(self): return at(9)
        def operation_mode(self): return 'maximise_export'
        async def num(self, key, *_args):
            return {'battery_capacity_entity': 10,
                    'battery_reserve_entity': 4,
                    'inverter_max_charge_rate_entity': 4000}[key], None
        def forecast_net_segments(self, _state, start, end, _attr):
            return [(start, end, 1., 0.)]
        def soc_at(self, *_args): return 40.
    state = {'attributes': {'manual_import_overrides': [
        {'start': at(15).isoformat(), 'end': at(17).isoformat(), 'rate_p': 0}]}}
    window = {'start': at(10), 'end': at(12), 'rate_p': 7}
    plan = {'charge': {'start': at(10).isoformat(), 'end': at(12).isoformat(),
                       'rate_w': 4000, 'target_soc': 100, 'planned_kwh': 6},
            'discharge': {'planned_kwh': 0}, 'calibration': {'state': 'normal'}}
    result = asyncio.run(apply_cheaper_manual_override(Fake(), state, plan, window, 40))
    assert result['manual_import_override']['action'] == 'defer_regular_charge'
    assert result['charge']['target_soc'] >= 30
    assert result['charge']['target_soc'] < 100
    assert result['charge']['planned_kwh'] < 6


def test_incomplete_future_bridge_keeps_full_regular_charge():
    class Fake:
        c = {'safety_buffer_soc': 20}
        def now(self): return at(9)
        def operation_mode(self): return 'maximise_export'
        async def num(self, key, *_args):
            return {'battery_capacity_entity': 10,
                    'battery_reserve_entity': 4,
                    'inverter_max_charge_rate_entity': 4000}[key], None
        def forecast_net_segments(self, *_args): return []
    state = {'attributes': {'manual_import_overrides': [
        {'start': at(15).isoformat(), 'end': at(17).isoformat(), 'rate_p': 0}]}}
    plan = {'charge': {'start': at(10).isoformat(), 'end': at(12).isoformat(),
                       'rate_w': 4000, 'target_soc': 100, 'planned_kwh': 6},
            'discharge': {}, 'calibration': {'state': 'normal'}}
    result = asyncio.run(apply_cheaper_manual_override(
        Fake(), state, plan, {'start': at(10), 'end': at(12), 'rate_p': 7}, 40))
    assert result['charge']['target_soc'] == 100
    assert result['manual_import_override']['reason'] == 'incomplete_intervening_forecast'
