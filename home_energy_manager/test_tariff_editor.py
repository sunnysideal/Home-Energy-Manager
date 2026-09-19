"""Regression tests for the built-in free electricity editor."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).with_name("tariff_editor.py")
spec = importlib.util.spec_from_file_location("tariff_editor", MODULE)
editor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(editor)


class TariffEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "windows.json"
        self.patcher = patch.object(editor, "STORE", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=1)

    def window(self, start=None, end=None):
        a = start or self.start
        b = end or a + timedelta(hours=1)
        return {"start": a.isoformat(), "end": b.isoformat()}

    def test_save_and_restart_read(self):
        expected = editor.save_windows([self.window()], "Europe/London")
        self.assertEqual(editor.read_windows(), expected)
        self.assertEqual(expected[0]["rate_p"], 0)

    def test_overlapping_and_adjacent_windows_merge(self):
        windows = [self.window(), self.window(self.start + timedelta(minutes=30), self.start + timedelta(hours=2)),
                   self.window(self.start + timedelta(hours=2), self.start + timedelta(hours=3))]
        saved = editor.save_windows(windows, "Europe/London")
        self.assertEqual(len(saved), 1)
        self.assertEqual(datetime.fromisoformat(saved[0]["end"]), self.start + timedelta(hours=3))

    def test_overnight_and_utc_offset(self):
        start = datetime(2026, 10, 24, 23, 30, tzinfo=timezone(timedelta(hours=1)))
        saved = editor.save_windows([self.window(start, start + timedelta(hours=2))], "Europe/London")
        self.assertEqual(datetime.fromisoformat(saved[0]["start"]), start.astimezone(timezone.utc))

    def test_invalid_period_does_not_replace_saved(self):
        original = editor.save_windows([self.window()], "Europe/London")
        with self.assertRaises(ValueError):
            editor.save_windows([self.window(self.start, self.start)], "Europe/London")
        self.assertEqual(editor.read_windows(), original)

    def test_delete_all_periods(self):
        editor.save_windows([self.window()], "Europe/London")
        self.assertEqual(editor.save_windows([], "Europe/London"), [])
        self.assertEqual(editor.read_windows(), [])

    def test_reject_missing_timezone(self):
        with self.assertRaises(ValueError):
            editor.save_windows([{"start": "2026-09-20T09:00:00", "end": "2026-09-20T10:00:00"}], "Europe/London")

    def test_reject_excessive_windows(self):
        with self.assertRaises(ValueError):
            editor.save_windows([self.window()] * 65, "Europe/London")

    def test_edit_middle_slot_preserves_neighbours(self):
        editor.save_windows([self.window(self.start, self.start + timedelta(hours=2))], "Europe/London")
        middle = self.start + timedelta(minutes=30)
        saved = editor.set_slot_price(middle.isoformat(), (middle + timedelta(minutes=30)).isoformat(), 12.5, "Europe/London")
        self.assertEqual([w["rate_p"] for w in saved], [0, 12.5, 0])
        self.assertEqual(len(saved), 3)
        restored = editor.set_slot_price(middle.isoformat(), (middle + timedelta(minutes=30)).isoformat(), None, "Europe/London")
        self.assertEqual(len(restored), 2)
        self.assertEqual([w["rate_p"] for w in restored], [0, 0])

    def test_mqtt_command_save_and_restore(self):
        start = self.start.isoformat()
        with patch.dict(editor.os.environ, {"HA_TIMEZONE": "Europe/London"}):
            saved = editor.apply_tariff_command(json.dumps({"id": "save-1", "start": start, "rate_p": 12.5}))
            self.assertEqual(saved["status"], "saved")
            self.assertEqual(saved["windows"][0]["rate_p"], 12.5)
            restored = editor.apply_tariff_command(json.dumps({"id": "restore-1", "start": start, "rate_p": None}))
            self.assertEqual(restored["windows"], [])

    def test_mqtt_command_rejects_missing_price_and_naive_time(self):
        with self.assertRaises(ValueError):
            editor.apply_tariff_command(json.dumps({"id": "missing", "start": self.start.isoformat()}))
        with self.assertRaises(ValueError):
            editor.apply_tariff_command(json.dumps({"id": "naive", "start": "2026-09-20T09:00:00", "rate_p": 0}))

    def test_reject_negative_or_non_finite_rate(self):
        for price in (-1, float("nan"), float("inf"), True):
            with self.subTest(price=price), self.assertRaises(ValueError):
                editor.save_windows([{**self.window(), "rate_p": price}], "Europe/London")

    def test_nonzero_adjacent_prices_remain_separate(self):
        first = {**self.window(), "rate_p": 0}
        second = {**self.window(self.start + timedelta(hours=1)), "rate_p": 12.5}
        saved = editor.save_windows([first, second], "Europe/London")
        self.assertEqual([w["rate_p"] for w in saved], [0, 12.5])

    def test_ingress_ui_contains_working_controls(self):
        for label in ('type="datetime-local"', 'Add period', 'Remove', 'api/windows'):
            self.assertIn(label, editor.PAGE)


if __name__ == "__main__":
    unittest.main()
