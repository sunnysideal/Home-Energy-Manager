"""Regression tests for date-safe advance Axle inverter discharge arming."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

CTRL = Path(__file__).parent
TZ = ZoneInfo("Europe/London")


def helpers():
    source = (CTRL / "minimise_export_runtime.py").read_text()
    tree = ast.parse(source)
    names = {"_arm_axle_discharge", "_disabled_discharge"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == 2
    core = SimpleNamespace(iso=lambda d: d.isoformat(), parse_dt=lambda d: datetime.fromisoformat(d) if d else None)
    ns = {"core": core, "timezone": timezone}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<helpers>", "exec"), ns)
    return ns


def test_arms_exact_event_interval_without_extra_house_load():
    fn = helpers()["_arm_axle_discharge"]
    start = datetime(2026, 10, 31, 18, 0, tzinfo=TZ)
    end = start + timedelta(hours=1)
    plan = {"charge": {}, "discharge": {}, "pause": {"mode": "PauseBoth"}}
    fn(plan, start, end, 6000, 4)
    assert plan["discharge"]["start"] == start.isoformat()
    assert plan["discharge"]["end"] == end.isoformat()
    assert plan["discharge"]["rate_w"] == 6000
    assert plan["discharge"]["planned_kwh"] == 6.0
    assert plan["discharge"]["target_soc"] == 4
    assert plan["pause"]["mode"] == "Disabled"


def test_preparation_charge_is_truncated_before_discharge():
    fn = helpers()["_arm_axle_discharge"]
    start = datetime(2026, 10, 31, 18, 0, tzinfo=TZ)
    plan = {"charge": {"start": (start - timedelta(minutes=30)).isoformat(),
                       "end": (start + timedelta(minutes=10)).isoformat(),
                       "planned_kwh": 4.0}, "discharge": {}}
    fn(plan, start, start + timedelta(hours=1), 6000, 4)
    assert datetime.fromisoformat(plan["charge"]["end"]) == start
    assert plan["charge"]["planned_kwh"] == 3.0


def test_event_watcher_phase_changes_and_expiry():
    source = (CTRL / "controller_legacy_core.py").read_text()
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "axle_event_watch_key")
    ns = {"parse_dt": datetime.fromisoformat, "timezone": timezone, "timedelta": timedelta,
          "iso": lambda d: d.isoformat()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<watch>", "exec"), ns)
    key = ns["axle_event_watch_key"]
    start = datetime(2026, 11, 1, 7, 0, tzinfo=TZ)
    end = start + timedelta(hours=1)
    event = {"attributes": {"event_available": True, "event_type": "export",
                            "start": start.isoformat(), "end": end.isoformat()}}
    assert key(event, start - timedelta(minutes=16), 15)[2] == "upcoming"
    assert key(event, start - timedelta(minutes=15), 15)[2] == "armed"
    assert key(event, start, 15)[2] == "active"
    assert key(event, end, 15) is None
    assert key({"attributes": {"event_available": False}}, start, 15) is None
