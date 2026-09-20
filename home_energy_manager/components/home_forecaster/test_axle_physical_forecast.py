"""Axle Export physical forecast regression tests."""
import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

module_path = Path(__file__).resolve().parent / "app" / "main.py"
spec = importlib.util.spec_from_file_location("home_forecaster_axle_test_module", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class AxleForecastTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/London")
        self.start = datetime(2026, 9, 21, 18, 0, tzinfo=self.tz)
        self.end = datetime(2026, 9, 21, 18, 30, tzinfo=self.tz)
        self.batt = {
            "capacity": 10.0, "reserve": 20.0, "max_rate_w": 6000,
            "charge_rate_w": 6000, "discharge_rate_w": 6000,
            "eco": True, "charge_enabled": False, "discharge_enabled": False,
            "charge_slots": [], "discharge_slots": [], "pause_mode": "Disabled",
            "pause_start": "00:00", "pause_end": "00:00",
        }

    def simulate(self, start=None, duration=0.5, soc=8.0, batt=None, event=True, ev_windows=None):
        slot = {"start_dt": start or self.start, "duration_h": duration,
                "load_kwh": 0.25 * duration / 0.5, "pv_kwh": 0.0}
        return module.simulate_battery_fractional(
            slot, batt or self.batt, soc, 0.95, 0.95,
            axle_event=(self.start, self.end) if event else None,
            ev_windows=ev_windows,
        )

    def test_future_event_discharge_and_reserve(self):
        soc, battery, imp, exp, mode, _, _ = self.simulate()
        self.assertAlmostEqual(battery, 3.0)
        self.assertAlmostEqual(soc, 8.0 - 3.0 / 0.95)
        self.assertAlmostEqual(exp, 2.75)
        self.assertEqual(mode, "forced_discharge")
        soc2, battery2, _, _, _, _, _ = self.simulate(soc=2.5)
        self.assertAlmostEqual(soc2, 2.0)
        self.assertAlmostEqual(battery2, 0.5 * 0.95)

    def test_partial_event_and_subsequent_soc(self):
        start = datetime(2026, 9, 21, 17, 45, tzinfo=self.tz)
        soc, battery, _, _, mode, _, _ = self.simulate(start=start, duration=0.5)
        self.assertGreater(battery, 1.4)
        self.assertLess(battery, 1.6)
        self.assertEqual(mode, "forced_discharge_partial")
        later, later_battery, _, _, _, _, _ = self.simulate(start=self.end, soc=soc)
        self.assertLess(later, soc)
        self.assertLess(later_battery, 1.0)

    def test_existing_discharge_is_not_doubled(self):
        batt = dict(self.batt, discharge_enabled=True,
                    discharge_slots=[("18:00", "18:30", 20.0)])
        actual = self.simulate(batt=batt)
        expected = self.simulate(batt=batt, event=False)
        self.assertEqual(actual, expected)

    def test_ev_window_blocks_axle_overlay(self):
        event = self.simulate(ev_windows=[(self.start, self.end)])
        baseline = self.simulate(event=False)
        self.assertEqual(event, baseline)

    def test_no_slots_counterfactual_is_unchanged(self):
        no_slots = module.battery_without_forced_slots(self.batt)
        actual = self.simulate(batt=no_slots, event=False)
        self.assertLess(actual[1], 1.0)
        self.assertGreater(self.simulate()[1], actual[1])


if __name__ == "__main__":
    unittest.main()
