"""Regression checks for stable Axle discharge suppression."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


SOURCE = Path(__file__).with_name("minimise_export_runtime.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTION = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "_disabled_discharge")


def test_axle_disabled_discharge_is_stable_across_replans():
    # Execute the actual helper without importing the full add-on runtime.
    namespace = {"core": SimpleNamespace(iso=lambda dt: dt.isoformat())}
    exec(compile(ast.Module(body=[FUNCTION], type_ignores=[]), "<axle-helper>", "exec"), namespace)
    disable = namespace["_disabled_discharge"]
    tz = ZoneInfo("Europe/London")
    first = {"discharge": {"rate_w": 6000, "target_soc": 4}}
    second = {"discharge": {"rate_w": 6000, "target_soc": 4}}
    now = datetime(2026, 9, 20, 18, 30, tzinfo=tz)
    disable(None, first, now)
    disable(None, second, now + timedelta(minutes=10))
    assert first["discharge"] == second["discharge"]
    assert first["discharge"]["start"] == first["discharge"]["end"]
    assert first["discharge"]["kind"] == "axle_protection"
    assert first["discharge"]["target_soc"] == 4
