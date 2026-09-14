from datetime import datetime, timedelta, timezone

from minimise_export_headroom import highest_soc_without_avoidable_export


def _segments(values):
    start = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    out = []
    for index, (load, pv) in enumerate(values):
        a = start + timedelta(hours=index)
        out.append((a, a + timedelta(hours=1), load, pv))
    return out


def test_low_solar_naturally_targets_full_charge():
    target, diag = highest_soc_without_avoidable_export(
        _segments([(1.0, 0.2), (1.0, 0.4), (1.0, 0.1)]),
        capacity_kwh=10.0,
        reserve_soc=10.0,
        max_charge_w=5000,
        max_discharge_w=5000,
    )
    assert target == 100
    assert diag["strategy"] == "highest_soc_without_avoidable_pv_export"


def test_solar_surplus_leaves_only_required_headroom():
    target, _diag = highest_soc_without_avoidable_export(
        _segments([(0.0, 2.0)]),
        capacity_kwh=10.0,
        reserve_soc=10.0,
        max_charge_w=5000,
        max_discharge_w=5000,
    )
    assert target == 80


def test_morning_load_allows_higher_overnight_target_before_afternoon_solar():
    target, _diag = highest_soc_without_avoidable_export(
        _segments([(2.0, 0.0), (0.0, 2.0)]),
        capacity_kwh=10.0,
        reserve_soc=10.0,
        max_charge_w=5000,
        max_discharge_w=5000,
    )
    assert target == 100


def test_unavoidable_power_limited_export_does_not_create_false_headroom_need():
    target, _diag = highest_soc_without_avoidable_export(
        _segments([(0.0, 6.0)]),
        capacity_kwh=10.0,
        reserve_soc=10.0,
        max_charge_w=2000,
        max_discharge_w=5000,
    )
    assert target == 80


def test_empty_forecast_fails_safe_to_full():
    target, diag = highest_soc_without_avoidable_export(
        [], 10.0, 10.0, 5000, 5000
    )
    assert target == 100
    assert diag["strategy"] == "no_forecast_segments_fail_safe_full"
