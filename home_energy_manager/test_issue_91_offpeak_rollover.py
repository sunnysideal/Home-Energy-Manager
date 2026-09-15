from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/London")


def dt(day, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=TZ)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_forecaster_rollover_derives_following_edf_window_from_active_tariff(monkeypatch):
    active = (dt("2026-09-14", "23:00"), dt("2026-09-15", "06:00"), 6.99)
    fake_base = SimpleNamespace(
        select_controller_offpeak=lambda rates, now, tz: None,
        find_overnight_blocks=lambda rates, tz: [active],
        LOG=SimpleNamespace(info=lambda *args, **kwargs: None),
        HAError=RuntimeError,
        main=lambda: None,
    )
    fake_runtime = ModuleType("battery_model_forecast_runtime")
    fake_runtime.base = fake_base
    monkeypatch.setitem(sys.modules, "battery_model_forecast_runtime", fake_runtime)
    module = load_module(
        ROOT / "components/home_forecaster/app/offpeak_rollover_runtime.py",
        "issue91_forecaster_runtime",
    )

    for clock in ("23:00", "23:05", "23:29", "23:30"):
        selected = module.select_controller_offpeak([], dt("2026-09-14", clock), TZ)
        assert selected[0] == dt("2026-09-15", "23:00")
        assert selected[1] == dt("2026-09-16", "06:00")
        assert selected[2] == 6.99

    selected = module.select_controller_offpeak([], dt("2026-09-15", "00:15"), TZ)
    assert selected[:2] == (dt("2026-09-15", "23:00"), dt("2026-09-16", "06:00"))


def test_forecaster_never_invents_octopus_hours_when_tariff_unknown(monkeypatch):
    fake_base = SimpleNamespace(
        select_controller_offpeak=lambda rates, now, tz: None,
        find_overnight_blocks=lambda rates, tz: [],
        LOG=SimpleNamespace(info=lambda *args, **kwargs: None),
        HAError=RuntimeError,
        main=lambda: None,
    )
    fake_runtime = ModuleType("battery_model_forecast_runtime")
    fake_runtime.base = fake_base
    monkeypatch.setitem(sys.modules, "battery_model_forecast_runtime", fake_runtime)
    module = load_module(
        ROOT / "components/home_forecaster/app/offpeak_rollover_runtime.py",
        "issue91_forecaster_missing_tariff",
    )
    try:
        module.select_controller_offpeak([], dt("2026-09-14", "23:05"), TZ)
    except RuntimeError as exc:
        assert "fixed tariff hours are not assumed" in str(exc)
    else:
        raise AssertionError("missing tariff data must fail explicitly")


def test_controller_does_not_treat_forecaster_current_window_as_following_window(monkeypatch):
    active = {"start": dt("2026-09-14", "23:00"), "end": dt("2026-09-15", "06:00"), "rate_p": 6.99}
    fake_core = SimpleNamespace()
    fake_runtime = ModuleType("minimise_export_core")
    fake_runtime.core = fake_core
    fake_runtime._shift_local_day = lambda controller, value, days: value + timedelta(days=days)
    fake_runtime._apply_axle_overlay = object()
    fake_runtime._current_regular_offpeak = lambda controller, window: active
    monkeypatch.setitem(sys.modules, "minimise_export_core", fake_runtime)
    module = load_module(
        ROOT / "components/controller/offpeak_rollover_runtime.py",
        "issue91_controller_runtime",
    )

    controller = SimpleNamespace(now=lambda: dt("2026-09-14", "23:05"))
    assert module._current_regular_offpeak(controller, active) is None

    following = {"start": dt("2026-09-15", "23:00"), "end": dt("2026-09-16", "06:00"), "rate_p": 6.99}
    assert module._current_regular_offpeak(controller, following) == active
    assert active["end"] < following["start"]


def test_runtime_image_packages_issue_91_guards():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "offpeak_rollover_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py" in docker
    assert "minimise_export_runtime.py /app/runtime/controller/minimise_export_core.py" in docker
    assert "offpeak_rollover_runtime.py /app/runtime/controller/legacy_minimise_export_runtime.py" in docker
