"""Executable regression coverage for the controller actually shipped in Docker.

Only test code is added for #115. A temporary directory is populated using
the production Dockerfile's real ordered controller COPY instructions; each
scenario runs in a fresh interpreter against that assembled /app/... entrypoint.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
CTRL = ROOT / "components" / "controller"
RESULT = "@@CONTROLLER_TEST_RESULT@@"
OFFPEAK_END = datetime.fromisoformat("2026-09-22T06:00:00+01:00")


@pytest.fixture(scope="session")
def packaged_controller(tmp_path_factory):
    image = tmp_path_factory.mktemp("controller-image")
    runtime = image / "runtime" / "controller"
    runtime.mkdir(parents=True)
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    mappings = re.findall(
        r"^COPY components/controller/(\S+) /app/runtime/controller/(\S+)$",
        docker, re.MULTILINE,
    )
    assert mappings, "No controller runtime files located in the production Dockerfile"
    for source, destination in mappings:
        shutil.copyfile(CTRL / source, runtime / destination)
    shutil.copytree(ROOT / "common", image / "common")
    # Keep this assertion on the final copied bytes, rather than merely checking
    # that the Dockerfile text contains the expected COPY line.
    assert (runtime / "app.py").read_bytes() == (CTRL / "calibration_charge_runtime.py").read_bytes()
    assert (runtime / "legacy_minimise_export_runtime.py").read_bytes() == (
        CTRL / "offpeak_rollover_runtime.py").read_bytes()
    assert (runtime / "minimise_export_core.py").read_bytes() == (
        CTRL / "minimise_export_runtime.py").read_bytes()
    return image


@pytest.fixture
def simulate(packaged_controller, tmp_path):
    def run(case="calibration", **kwargs):
        input_data = {"case": case, **kwargs}
        env = os.environ.copy()
        env["PYTHONPATH"] = str(packaged_controller)
        env["HOME_ENERGY_MQTT_CONFIG"] = '{"enabled":false}'
        env["SUPERVISOR_TOKEN"] = "test-supervisor-token"
        cmd = [sys.executable, str(ROOT / "test_support" / "controller_package_runner.py"),
               str(packaged_controller), str(tmp_path)]
        completed = subprocess.run(
            cmd, input=json.dumps(input_data), text=True, capture_output=True,
            cwd=packaged_controller, env=env, timeout=45, check=False,
        )
        assert completed.returncode == 0, (
            f"Packaged controller scenario {input_data} failed:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        matching = [line[len(RESULT):] for line in completed.stdout.splitlines()
                    if line.startswith(RESULT)]
        assert len(matching) == 1, completed.stdout
        return json.loads(matching[0])
    return run


def fields(result):
    # These are the actual desired values sent through Controller.apply, after
    # the production pause/transfer conflict and active-slot guards.
    by_entity = {entry["entity"]: entry["value"] for entry in result["writes"]}
    return {name: by_entity.get(entity) for name, entity in result["entities"].items()}


def assert_no_unauthorised_target_write(result):
    # The user's charge target setting must remain untouched even when the
    # planner expresses a logical 100% calibration objective.
    assert result["entities"]["charge_slot_1_target"] not in {
        write["entity"] for write in result["writes"]}
    assert fields(result)["discharge_slot_1_target"] == 4


def assert_recharge_sequence(result, *, natural):
    plan = result["plan"]
    assert plan is not None
    calibration = plan["calibration"]
    assert calibration["state"] == "awaiting_deep_low"
    assert calibration["feasible"] is True
    assert plan["pause"]["mode"] == "Disabled"
    assert plan["charge"]["kind"] == "calibration_forecast_recharge"
    assert plan["charge"]["target_soc"] == 100
    low = datetime.fromisoformat(calibration["latest_safe_reserve_at"])
    start = datetime.fromisoformat(plan["charge"]["start"])
    end = datetime.fromisoformat(plan["charge"]["end"])
    assert start >= low + timedelta(minutes=14)  # minute-truncated start
    assert start < end <= OFFPEAK_END
    # Calibration discharge (if any) finishes before recharge starts.
    assert datetime.fromisoformat(plan["discharge"]["end"]) <= start
    assert datetime.fromisoformat(plan["discharge"]["start"]) <= low
    assert plan["discharge"]["target_soc"] == 4
    assert fields(result)["charge_slot_1_start"] == start.strftime("%H:%M:%S")
    assert fields(result)["charge_slot_1_end"] == end.replace(second=0, microsecond=0).strftime("%H:%M:%S")
    assert fields(result)["pause_mode"] == "Disabled"
    assert_no_unauthorised_target_write(result)
    if natural:
        assert calibration["strategy"] == "natural_discharge"
        assert calibration["forced_export_required"] is False
        assert plan["discharge"]["kind"] == "calibration_natural_depletion"
        assert plan["discharge"]["planned_kwh"] == 0
    else:
        # The 0.1.115 plan does not expose initial_soc; diagnostics therefore
        # label the residual as forced_discharge despite the no-slots curve
        # already accounting for prior natural house consumption.
        assert calibration["strategy"] == "forced_discharge"
        assert calibration["forced_export_required"] is True
        assert plan["discharge"]["kind"] == "calibration_to_reserve"
        assert 0 < plan["discharge"]["planned_kwh"] < 13.79 * .96


@pytest.mark.parametrize("natural_soc,natural", [(4, True), (18, False)])
def test_manual_low_calibration_preprograms_actual_inverter_schedule(simulate, natural_soc, natural):
    result = simulate(request=True, natural_soc=natural_soc)
    assert result["calibration_state"] == "awaiting_deep_low"
    assert result["request_pending"] is True
    assert result["auto_calibration_enabled"] is False
    assert_recharge_sequence(result, natural=natural)


def test_package_dispatch_ignores_shadow_patch_on_forwarding_module(simulate):
    # Regression for #92/#114: a replacement on the wrong forwarding module
    # must not change the *real* plan's charge kind or final applied slot.
    result = simulate(request=True, incorrect_patch=True)
    assert_recharge_sequence(result, natural=True)


def test_effective_planner_dispatch_is_required_for_recharge(simulate):
    # Deliberately break the effective registration; the public schedule
    # assertion above MUST now fail. A mere source-string test cannot detect it.
    result = simulate(request=True, incorrect_patch_only=True)
    # If the implementation is installed only on the forwarding module,
    # the actual planner still produces its original no-recharge schedule.
    assert result["calibration_state"] == "awaiting_deep_low"
    assert result["plan"]["charge"].get("kind") != "calibration_forecast_recharge"
    # Normal minimise-export no longer pauses Eco during the cheap window.
    assert result["plan"]["charge"].get("kind") != "calibration_forecast_recharge"


def test_low_request_survives_fresh_controller_instance_and_db(simulate):
    result = simulate(request=True, restart=True)
    assert result["request_pending"] is True
    assert result["calibration_state"] == "awaiting_deep_low"
    assert_recharge_sequence(result, natural=True)


def test_low_observation_transitions_to_full_recharge_without_reenabling_automatic(simulate):
    result = simulate("lifecycle", request=True)
    assert_recharge_sequence(result, natural=True)
    observed = result["observed_low"]
    assert observed["state"] == "deep_recharge"
    assert observed["pending"] is False
    assert observed["low_reached_at"]
    recharge = result["recharge_plan"]
    assert recharge is not None
    assert recharge["calibration"]["state"] == "deep_recharge"
    assert recharge["charge"]["target_soc"] == 100
    assert datetime.fromisoformat(recharge["charge"]["end"]) <= OFFPEAK_END
    assert all(write["entity"] != result["entities"]["charge_slot_1_target"]
               for write in result["recharge_writes"])
    full = result["observed_full"]
    assert full["low_reached_at"] is None
    assert full["last_deep_calibration_at"]
    assert full["auto_calibration_enabled"] is False
    assert full["state"] == "disabled"


def test_no_request_and_spontaneous_low_do_not_force_calibration_recharge(simulate):
    result = simulate(soc=4, natural_soc=4, observe=True)
    assert result["calibration_state"] == "disabled"
    assert result["request_pending"] is False
    assert result["low_reached_at"] is None
    assert result["plan"]["charge"].get("kind") != "calibration_forecast_recharge"
    assert_no_unauthorised_target_write(result)


def test_infeasible_calibration_does_not_schedule_forced_export_or_late_recharge(simulate):
    result = simulate(request=True, clock_minutes=14 * 60, natural_soc=18)
    assert result["plan"]["calibration"]["feasible"] is False
    assert result["plan"]["calibration"]["action"] == "postponed"
    assert result["plan"]["discharge"]["kind"] != "calibration_to_reserve"
    assert result["plan"]["charge"].get("kind") != "calibration_forecast_recharge"
    assert result["request_pending"] is True


def test_future_axle_suppresses_forced_calibration_discharge_but_keeps_recharge(simulate):
    result = simulate(request=True, natural_soc=18, axle="future")
    assert result["plan"]["axle"]["action"] in ("ready", "waiting_to_prepare", "protecting",
                                               "prepare_in_regular_offpeak", "peak_prepare_last_resort")
    assert result["plan"]["discharge"]["kind"] != "calibration_to_reserve"
    assert result["plan"]["discharge"]["planned_kwh"] == 0
    assert result["plan"]["charge"]["kind"] == "calibration_forecast_recharge"
    assert_no_unauthorised_target_write(result)


def test_active_axle_overlays_low_calibration_and_confirmed_ev_blocks_export(simulate):
    active = simulate(request=True, natural_soc=18, axle="active", clock_minutes=4 * 60 + 45)
    assert active["plan"]["axle"]["action"] == "axle_export"
    assert active["plan"]["discharge"]["kind"] == "axle_export"
    assert active["plan"]["discharge"]["target_soc"] == 4
    assert_no_unauthorised_target_write(active)
    ev = simulate(request=True, natural_soc=18, axle="active", ev_active=True,
                  clock_minutes=4 * 60 + 45)
    assert ev["plan"]["intelligent_go"]["confirmed"] is True
    assert ev["plan"]["discharge"]["kind"] != "axle_export"
    assert ev["plan"]["discharge"]["planned_kwh"] == 0
    assert ev["plan"]["intelligent_go"]["export_suspended"] is True


def test_axle_lifecycle_replans_same_controller_without_losing_calibration(simulate):
    result = simulate("axle_sequence", request=True, natural_soc=18, axle="future")
    assert result["plan"]["discharge"]["kind"] != "calibration_to_reserve"
    assert result["plan"]["charge"]["kind"] == "calibration_forecast_recharge"
    active = result["sequence_active"]
    assert active["plan"]["axle"]["action"] == "axle_export"
    assert active["plan"]["discharge"]["kind"] == "axle_export"
    after = result["sequence_after"]
    assert after["plan"]["discharge"]["kind"] == "calibration_to_reserve"
    assert after["plan"]["charge"]["kind"] == "calibration_forecast_recharge"
    assert result["request_pending_after"] is True
    charge_entity = result["entities"]["charge_slot_1_target"]
    assert all(write["entity"] != charge_entity for stage in (
        result["writes"], active["writes"], after["writes"]) for write in stage)


def test_active_axle_discharge_preserves_existing_inverter_slot_start(simulate):
    result = simulate(request=True, natural_soc=18, axle="active",
                      clock_minutes=4 * 60 + 45, active_existing_discharge=True)
    assert datetime.fromisoformat(result["plan"]["discharge"]["start"]).strftime(
        "%H:%M:%S") == "18:30:00"
    assert fields(result)["discharge_slot_1_start"] == "18:35:00"
    assert fields(result)["discharge_slot_1_end"] == "19:30:00"
    assert_no_unauthorised_target_write(result)


def test_expired_or_absent_axle_restores_underlying_calibration_planner(simulate):
    for axle in ("expired", "none"):
        result = simulate(request=True, natural_soc=18, axle=axle,
                          clock_minutes=6 * 60)
        assert result["plan"]["discharge"]["kind"] == "calibration_to_reserve"
        assert result["plan"]["charge"]["kind"] == "calibration_forecast_recharge"
        assert result["plan"]["discharge"]["target_soc"] == 4


def test_normal_minimise_export_eco_remains_available_before_charge(simulate):
    result = simulate(natural_soc=18)
    assert result["calibration_state"] == "disabled"
    assert result["plan"]["discharge"]["planned_kwh"] == 0
    assert result["plan"]["pause"]["mode"] == "Disabled"
    assert fields(result)["eco_mode"] == "on"
    assert fields(result)["pause_mode"] == "Disabled"
    charge = result["plan"]["charge"]
    assert datetime.fromisoformat(charge["start"]) <= datetime.fromisoformat(charge["end"])
    assert_no_unauthorised_target_write(result)


def test_confirmed_ev_smart_charging_uses_no_forced_export(simulate):
    result = simulate(ev_active=True, request=True, natural_soc=18, clock_minutes=10)
    assert result["plan"]["intelligent_go"]["confirmed"] is True
    assert result["plan"]["intelligent_go"]["export_suspended"] is True
    discharge = result["plan"]["discharge"]
    start = datetime.fromisoformat(discharge["start"])
    end = datetime.fromisoformat(discharge["end"])
    ev_start = datetime.fromisoformat(result["plan"]["intelligent_go"]["slot_start"])
    ev_end = datetime.fromisoformat(result["plan"]["intelligent_go"]["slot_end"])
    # Existing planner can retain a *future* calibration export slot. It must
    # not force export during the currently confirmed EV settlement half-hour.
    assert end <= start or end <= ev_start or start >= ev_end
    assert_no_unauthorised_target_write(result)


def test_power_down_uses_existing_controller_overlay_without_target_write(simulate):
    result = simulate(power_down=True, natural_soc=18)
    assert result["plan"].get("power_down")
    assert result["plan"]["discharge"]["target_soc"] == 4
    assert_no_unauthorised_target_write(result)


@pytest.mark.parametrize("invalid_model", [True, False])
def test_invalid_or_missing_authoritative_model_fails_closed(simulate, invalid_model):
    if not invalid_model:
        valid = simulate()
        assert valid["model_source"] == "forecaster"
        return
    result = simulate(request=True, invalid_model=True)
    assert result["plan"] is None
    assert result["model_source"] == "unavailable"
    assert result["writes"] == []


def test_incomplete_bridge_forecast_fails_safe_to_full_solar_headroom(simulate):
    result = simulate(incomplete_forecast=True)
    assert result["plan"]["minimise_export"]["solar_headroom_target_soc"] == 100
    assert result["plan"]["minimise_export"]["solar_headroom_forecast_complete"] is False



def test_manual_high_reuses_top_due_without_automatic_calibration(simulate):
    result = simulate(request_high=True)
    assert result["auto_calibration_enabled"] is False
    assert result["calibration_state"] == "top_due"
    assert result["high_pending"] is True
    assert result["plan"]["charge"]["target_soc"] == 100
    assert result["calibration_attrs"]["high_calibration_request_state"] == "planned"
    assert_no_unauthorised_target_write(result)


def test_repeated_low_and_high_button_presses_keep_original_request_timestamps(simulate):
    result = simulate(request=True, request_high=True, repeat_request=True)
    assert result["request_pending"] is True
    assert result["high_pending"] is True
    assert result["low_requested_at"]
    assert result["high_requested_at"]
    assert result["calibration_state"] == "awaiting_deep_low"
    assert result["calibration_attrs"]["high_calibration_request_state"] == "blocked"
    assert_recharge_sequence(result, natural=True)


def test_low_and_high_requests_survive_restart_and_keep_low_priority(simulate):
    result = simulate(request=True, request_high=True, restart=True)
    assert result["request_pending"] is True
    assert result["high_pending"] is True
    assert result["calibration_state"] == "awaiting_deep_low"
    assert result["calibration_attrs"]["high_calibration_request_state"] == "blocked"
    assert_recharge_sequence(result, natural=True)


def test_manual_high_requires_observed_100_percent_not_99(simulate):
    result = simulate("calibration_request_control", request_high=True, check_high_completion=True)
    assert result["at_99"]["high_pending"] is True
    assert result["at_99"]["high_completed"] is None
    assert result["at_100"]["high_pending"] is False
    assert result["at_100"]["high_completed"]
    assert result["at_100"]["last_full"]
    assert result["at_100"]["auto_enabled"] is False


def test_clear_removes_transient_low_and_high_but_preserves_history(simulate):
    result = simulate("calibration_request_control", request=True, request_high=True, clear=True)
    cleared = result["after_clear"]
    assert cleared["low_pending"] is False
    assert cleared["high_pending"] is False
    assert cleared["low_result"] == "cleared"
    assert cleared["high_result"] == "cleared"
    assert cleared["clear_result"] == "cleared"
    assert cleared["last_below_40"] == "2026-09-20T09:00:00+01:00"
    assert cleared["last_deep"] == "2026-09-01T05:00:00+01:00"
    assert cleared["low_reached"] is None


def test_simultaneous_low_high_and_clear_resolves_to_clear(simulate):
    result = simulate("calibration_request_control", clear_same_pass=True)
    cleared = result["after_clear"]
    assert cleared["low_pending"] is False
    assert cleared["high_pending"] is False
    assert cleared["low_result"] == "cleared"
    assert cleared["high_result"] == "cleared"
    assert cleared["clear_result"] == "cleared"
    assert set(cleared["cleared_keys"]) >= {
        "manual_low_calibration_pending", "manual_high_calibration_pending",
    }


def test_forecast_only_loads_no_active_controller_and_performs_no_writes(simulate):
    result = simulate("forecast_only")
    assert result["active_controller_loaded"] is False
    assert result["writes"] == []
    assert result["published"][0]["attributes"]["control_enabled"] is False
