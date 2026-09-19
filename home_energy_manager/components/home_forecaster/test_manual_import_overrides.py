"""Regression tests for issue #101: price overlays never redefine regular off-peak."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import importlib.util
import sys
from pathlib import Path

module_path = Path(__file__).resolve().parent / "app" / "main.py"
spec = importlib.util.spec_from_file_location("home_forecaster_manual_override_test_module", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
apply_manual_import_overrides = module.apply_manual_import_overrides
select_controller_offpeak = module.select_controller_offpeak
rate_at = module.rate_at


class ManualImportOverrideTests(unittest.TestCase):
    def test_daytime_free_period_preserves_regular_window(self):
        tz = ZoneInfo("Europe/London")
        def dt(value):
            return datetime.fromisoformat(value)
        supplier = [
            {"start": dt("2026-09-19T23:00:00+01:00"), "end": dt("2026-09-20T06:00:00+01:00"), "rate_p": 6.99},
            {"start": dt("2026-09-20T06:00:00+01:00"), "end": dt("2026-09-20T23:00:00+01:00"), "rate_p": 31.12},
        ]
        windows = [
            {"start": "2026-09-19T23:00:00+01:00", "end": "2026-09-20T06:00:00+01:00", "rate_p": 0},
            {"start": "2026-09-20T09:00:00+01:00", "end": "2026-09-20T14:00:00+01:00", "rate_p": 0},
            {"start": "2026-09-20T15:00:00+01:00", "end": "2026-09-20T16:00:00+01:00", "rate_p": 0},
        ]
        before = select_controller_offpeak(supplier, dt("2026-09-19T20:00:00+01:00"), tz)
        effective = apply_manual_import_overrides(supplier, windows)
        after = select_controller_offpeak(supplier, dt("2026-09-19T20:00:00+01:00"), tz)
        self.assertEqual(before, after)
        self.assertEqual(before[2], 6.99)
        for start, end in (("2026-09-20T09:00:00+01:00", "2026-09-20T09:30:00+01:00"),
                           ("2026-09-20T15:00:00+01:00", "2026-09-20T15:30:00+01:00")):
            self.assertEqual(rate_at(effective, dt(start), dt(end)), 0)
        self.assertEqual(rate_at(effective, dt("2026-09-20T14:00:00+01:00"), dt("2026-09-20T14:30:00+01:00")), 31.12)
        self.assertEqual(rate_at(supplier, dt("2026-09-20T09:00:00+01:00"), dt("2026-09-20T09:30:00+01:00")), 31.12)

    def test_rejects_invalid_interval(self):
        with self.assertRaises(ValueError):
            apply_manual_import_overrides([], [{"start": "2026-09-20T15:00:00+01:00", "end": "2026-09-20T14:00:00+01:00"}])


if __name__ == "__main__":
    unittest.main()
