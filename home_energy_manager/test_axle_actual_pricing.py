import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
APP = ROOT / "components" / "home_forecaster" / "app"
sys.path.insert(0, str(APP))

# axle_pricing_runner imports its sibling as the generic module name ``main``.
# Other tests may already have loaded another component's main.py under that name,
# so isolate this import and then restore the previous module to avoid collection-order
# dependence in the full GitHub CI suite.
_previous_main = sys.modules.pop("main", None)
try:
    spec = importlib.util.spec_from_file_location("axle_pricing_runner_test", APP / "axle_pricing_runner.py")
    axle = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(axle)
finally:
    if _previous_main is not None:
        sys.modules["main"] = _previous_main
    else:
        sys.modules.pop("main", None)


def test_realised_axle_credit_replaces_normal_export_income():
    payload = {
        "attributes": {
            "today": {
                "actual": {"import_cost_p": 25.0, "export_income_p": 12.0, "cost_p": 13.0},
                "total": {"import_cost_p": 40.0, "export_income_p": 22.0, "cost_p": 18.0},
            }
        }
    }
    credit = {
        "export_kwh": 2.0,
        "normal_export_income_p": 24.0,
        "axle_export_income_p": 200.0,
        "replacement_delta_p": 176.0,
    }
    axle._apply_actual_credit(payload, credit)
    actual = payload["attributes"]["today"]["actual"]
    total = payload["attributes"]["today"]["total"]
    assert actual["export_income_p"] == 188.0
    assert actual["cost_p"] == -163.0
    assert total["export_income_p"] == 198.0
    assert total["cost_p"] == -158.0


def test_actual_event_records_persist_when_current_axle_event_changes(tmp_path):
    store = axle.base.Store(tmp_path / "forecast.db")
    day = datetime(2026, 9, 11, 18, 30, tzinfo=ZoneInfo("Europe/London")).date()
    records = {
        "event-a": {
            "export_kwh": 3.5,
            "normal_export_income_p": 0.0,
            "axle_export_income_p": 350.0,
            "replacement_delta_p": 350.0,
        }
    }
    axle._save_actual_records(store, day, records)
    loaded = axle._load_actual_records(store, day)
    assert loaded == records


def test_partial_active_event_is_measured_only_to_now():
    tz = ZoneInfo("Europe/London")
    start = datetime(2026, 9, 11, 18, 0, tzinfo=tz)
    end = datetime(2026, 9, 11, 19, 0, tzinfo=tz)
    now = datetime(2026, 9, 11, 18, 25, tzinfo=tz)
    assert min(end, now) == now
    assert (now - start).total_seconds() == 25 * 60
