"""Regression checks for stable Axle discharge suppression."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from components.controller import minimise_export_runtime as runtime


def test_axle_disabled_discharge_is_stable_across_replans():
    tz = ZoneInfo("Europe/London")
    controller = SimpleNamespace()
    first = {"discharge": {"rate_w": 6000, "target_soc": 4}}
    second = {"discharge": {"rate_w": 6000, "target_soc": 4}}
    now = datetime(2026, 9, 20, 18, 30, tzinfo=tz)
    runtime._disabled_discharge(controller, first, now)
    runtime._disabled_discharge(controller, second, now + timedelta(minutes=10))
    assert first["discharge"] == second["discharge"]
    assert first["discharge"]["start"] == first["discharge"]["end"]
    assert first["discharge"]["kind"] == "axle_protection"
    assert first["discharge"]["target_soc"] == 4
