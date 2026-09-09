import json
from copy import deepcopy
from unittest.mock import patch

import launcher


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_home_assistant_timezone_overrides_legacy_setting(monkeypatch):
    raw = {"home_forecaster": {"settings": {"timezone": "UTC"}}}
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    with patch.object(launcher, "urlopen", return_value=_Response({"time_zone": "Europe/London"})):
        resolved = launcher._resolve_home_assistant_timezone(raw)
    assert resolved == "Europe/London"
    assert raw["home_forecaster"]["settings"]["timezone"] == "Europe/London"


def test_home_assistant_timezone_failure_keeps_legacy_fallback(monkeypatch):
    raw = {"home_forecaster": {"settings": {"timezone": "Europe/Paris"}}}
    before = deepcopy(raw)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    resolved = launcher._resolve_home_assistant_timezone(raw)
    assert resolved == "Europe/Paris"
    assert raw == before
