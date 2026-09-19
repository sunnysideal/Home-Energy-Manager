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
