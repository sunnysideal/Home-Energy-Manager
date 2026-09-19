from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import main as forecaster


BASE = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)


class Client:
    def __init__(self, rates):
        self.rates = rates

    def state_optional(self, entity):
        if entity == "today":
            return {"attributes": {"rates": self.rates}}
        return None


class Config:
    def section(self, name):
        assert name == "tariff"
        return {"import_current_day_rates": "today",
                "import_next_day_rates": "tomorrow",
                "import_current_rate": "current"}


def rate(start, end, value):
    return {"start": start.isoformat(), "end": end.isoformat(), "value": value}


def test_supplier_current_price(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUAL_TARIFF_WINDOWS_PATH", str(tmp_path / "none"))
    value, attrs = forecaster.effective_import_price(
        Client([rate(BASE, BASE + timedelta(hours=1), .07)]), Config(), BASE)
    assert value == 7
    assert attrs["source"] == "supplier"
    assert attrs["window_end"] == (BASE + timedelta(hours=1)).isoformat()


def test_manual_zero_and_expiry(monkeypatch, tmp_path):
    path = tmp_path / "windows.json"
    monkeypatch.setenv("MANUAL_TARIFF_WINDOWS_PATH", str(path))
    path.write_text(json.dumps([{"start": (BASE + timedelta(minutes=10)).isoformat(),
                                 "end": (BASE + timedelta(minutes=20)).isoformat(),
                                 "rate_p": 0}]))
    client = Client([rate(BASE, BASE + timedelta(hours=1), .07)])
    assert forecaster.effective_import_price(client, Config(), BASE)[0] == 7
    value, attrs = forecaster.effective_import_price(client, Config(), BASE + timedelta(minutes=15))
    assert value == 0
    assert attrs["manual_override_active"] is True
    assert attrs["next_change"] == (BASE + timedelta(minutes=20)).isoformat()
    value, attrs = forecaster.effective_import_price(client, Config(), BASE + timedelta(minutes=20))
    assert value == 7
    assert attrs["manual_override_active"] is False


def test_overlapping_overrides_use_forecaster_precedence(monkeypatch, tmp_path):
    path = tmp_path / "windows.json"
    monkeypatch.setenv("MANUAL_TARIFF_WINDOWS_PATH", str(path))
    path.write_text(json.dumps([
        {"start": BASE.isoformat(), "end": (BASE + timedelta(minutes=40)).isoformat(), "rate_p": 2},
        {"start": (BASE + timedelta(minutes=10)).isoformat(),
         "end": (BASE + timedelta(minutes=20)).isoformat(), "rate_p": 0}]))
    value, _ = forecaster.effective_import_price(
        Client([rate(BASE, BASE + timedelta(hours=1), .07)]), Config(), BASE + timedelta(minutes=15))
    assert value == 0


def test_missing_supplier_window_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUAL_TARIFF_WINDOWS_PATH", str(tmp_path / "none"))
    value, attrs = forecaster.effective_import_price(
        Client([rate(BASE, BASE + timedelta(minutes=10), .07)]), Config(), BASE + timedelta(minutes=20))
    assert value == "unavailable"
    assert attrs["source"] == "unavailable"
