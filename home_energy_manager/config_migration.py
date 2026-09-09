from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

CANONICAL_VERSION = 1
CANONICAL_DOMAINS = (
    "battery",
    "grid",
    "inverter",
    "tariff",
    "solar",
    "heat_pump",
    "dhw",
    "ev",
    "energy_strategy",
    "advanced",
)


@dataclass
class MigrationDiagnostics:
    canonical_version: int = CANONICAL_VERSION
    source_layout: str = "legacy"
    moves: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_version": self.canonical_version,
            "source_layout": self.source_layout,
            "moves": self.moves,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
        }


def _nonempty(value: Any) -> bool:
    return value not in (None, "")


def _get(root: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = root
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(root: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = root
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _pick(
    raw: dict[str, Any],
    canonical: dict[str, Any],
    canonical_path: str,
    legacy_paths: Iterable[str],
    diagnostics: MigrationDiagnostics,
    default: Any = None,
) -> Any:
    explicit = _get(raw, canonical_path)
    candidates: list[tuple[str, Any]] = []
    if _nonempty(explicit):
        candidates.append((canonical_path, explicit))
    for path in legacy_paths:
        value = _get(raw, path)
        if _nonempty(value):
            candidates.append((path, value))

    if not candidates:
        value = default
    else:
        chosen_path, value = candidates[0]
        for other_path, other_value in candidates[1:]:
            if other_value == value:
                diagnostics.duplicates.append(
                    f"{canonical_path}: {chosen_path} and {other_path} contain the same value"
                )
            else:
                diagnostics.conflicts.append(
                    f"{canonical_path}: using {chosen_path}={value!r}; ignoring {other_path}={other_value!r}"
                )
        if chosen_path != canonical_path:
            diagnostics.moves.append(f"{chosen_path} -> {canonical_path}")

    _set(canonical, canonical_path, value)
    return value


def _normalise_legacy_home_forecaster(raw: dict[str, Any]) -> None:
    """Accept the standalone-style Home Forecaster layout accidentally shipped in 0.1.31."""
    hf = raw.get("home_forecaster")
    if not isinstance(hf, dict):
        return

    settings = hf.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        hf["settings"] = settings
    for key, default in (
        ("timezone", "Europe/London"),
        ("history_days", 28),
        ("day_of_week_weighting", True),
        ("same_weekday_weight", 1.5),
        ("forecast_interval_minutes", 5),
        ("controller_refresh_request_entity", "sensor.home_energy_forecast_refresh_request"),
        ("charge_efficiency", 0.95),
        ("discharge_efficiency", 0.95),
        ("minimum_baseline_w", 200),
        ("log_level", "INFO"),
    ):
        if key not in settings:
            settings[key] = hf.get(key, default)

    battery = hf.setdefault("battery", {})
    if isinstance(battery, dict):
        for key in ("charge_target_1", "charge_target_2", "discharge_target_1", "discharge_target_2"):
            old_key = key.replace("_target_", "_target_soc_")
            if key not in battery:
                battery[key] = battery.get(old_key, "")

    load = hf.setdefault("load", {})
    if isinstance(load, dict):
        load.setdefault("energy_total_kwh", "")

    pv = hf.get("pv") if isinstance(hf.get("pv"), dict) else {}
    solar = hf.setdefault("solar", {})
    if isinstance(solar, dict):
        solar.setdefault("energy_total_kwh", pv.get("energy_total_kwh", ""))
        solar.setdefault("solcast_today", pv.get("solcast_today", ""))
        solar.setdefault("solcast_tomorrow", pv.get("solcast_tomorrow", ""))
        solar.setdefault("solcast_day_3", pv.get("solcast_day3", ""))

    grid = hf.get("grid") if isinstance(hf.get("grid"), dict) else {}
    meter = hf.setdefault("meter", {})
    if isinstance(meter, dict):
        meter.setdefault("import_energy_total_kwh", grid.get("import_energy_total_kwh", ""))
        meter.setdefault("export_energy_total_kwh", grid.get("export_energy_total_kwh", ""))

    tariff = hf.setdefault("tariff", {})
    if isinstance(tariff, dict):
        tariff.setdefault("import_energy_total_kwh", grid.get("inverter_import_energy_total_kwh", ""))
        tariff.setdefault("export_energy_total_kwh", grid.get("inverter_export_energy_total_kwh", ""))
        tariff.setdefault("import_current_rate", tariff.get("import_rate", ""))
        tariff.setdefault("export_current_rate", tariff.get("export_rate", ""))
        tariff.setdefault("import_current_day_rates", "")
        tariff.setdefault("import_next_day_rates", "")
        tariff.setdefault("export_current_day_rates", "")
        tariff.setdefault("export_next_day_rates", "")

    ashp = hf.setdefault("ashp", {})
    if isinstance(ashp, dict):
        ashp.setdefault("forecast_48h", ashp.get("forecast_entity", "sensor.ashp_forecast_next_48h"))
        ashp.setdefault("ch_energy_total_kwh", "sensor.ashp_electrical_energy_ch")
        ashp.setdefault("dhw_energy_total_kwh", "sensor.ashp_electrical_energy_dhw")

    ev = hf.setdefault("ev", {})
    if isinstance(ev, dict):
        ev.setdefault("smart_charging_active_entity", load.get("ev_charging_entity", "") if isinstance(load, dict) else "")
        ev.setdefault("smart_charging_dispatch_entity", tariff.get("intelligent_dispatching", "") if isinstance(tariff, dict) else "")
        ev.setdefault("energy_total_kwh", "")


def _legacy_controller_aliases(raw: dict[str, Any]) -> None:
    ctl = raw.get("controller")
    if not isinstance(ctl, dict):
        return
    if "ev_smart_charging_enabled" not in ctl and "intelligent_go_enabled" in ctl:
        ctl["ev_smart_charging_enabled"] = ctl.get("intelligent_go_enabled")
    if "ev_smart_charging_dispatch_entity" not in ctl:
        ctl["ev_smart_charging_dispatch_entity"] = ctl.get("intelligent_dispatch_entity", "")
    if "ev_smart_charging_active_entity" not in ctl:
        ctl["ev_smart_charging_active_entity"] = ctl.get("intelligent_car_charging_entity", "")


def build_canonical(raw: dict[str, Any], diagnostics: MigrationDiagnostics) -> dict[str, Any]:
    canonical: dict[str, Any] = {name: {} for name in CANONICAL_DOMAINS}
    diagnostics.source_layout = "canonical" if any(isinstance(raw.get(k), dict) for k in CANONICAL_DOMAINS) else "legacy"

    mappings: tuple[tuple[str, tuple[str, ...], Any], ...] = (
        ("battery.soc", ("home_forecaster.battery.soc",), ""),
        ("battery.capacity_kwh", ("home_forecaster.battery.capacity_kwh",), ""),
        ("battery.reserve_soc", ("home_forecaster.battery.reserve_soc",), ""),
        ("battery.power_w", ("home_forecaster.battery.battery_power_w",), ""),
        ("battery.charge_energy_total_kwh", ("home_forecaster.battery.battery_charge_energy_total_kwh",), ""),
        ("battery.discharge_energy_total_kwh", ("home_forecaster.battery.battery_discharge_energy_total_kwh",), ""),

        ("grid.import_energy_total_kwh", ("home_forecaster.meter.import_energy_total_kwh",), ""),
        ("grid.export_energy_total_kwh", ("home_forecaster.meter.export_energy_total_kwh",), ""),
        ("inverter.house_load_energy_total_kwh", ("home_forecaster.load.energy_total_kwh",), ""),
        ("inverter.import_energy_total_kwh", ("home_forecaster.tariff.import_energy_total_kwh",), ""),
        ("inverter.export_energy_total_kwh", ("home_forecaster.tariff.export_energy_total_kwh",), ""),
        ("inverter.max_rate_w", ("home_forecaster.battery.inverter_max_rate_w",), ""),
        ("inverter.charge_rate_w", ("home_forecaster.battery.charge_rate_w",), ""),
        ("inverter.discharge_rate_w", ("home_forecaster.battery.discharge_rate_w",), ""),
        ("inverter.eco_mode", ("home_forecaster.battery.eco_mode",), ""),
        ("inverter.charge_schedule_enabled", ("home_forecaster.battery.charge_schedule_enabled",), ""),
        ("inverter.discharge_schedule_enabled", ("home_forecaster.battery.discharge_schedule_enabled",), ""),
        ("inverter.pause_mode", ("home_forecaster.battery.pause_mode",), ""),
        ("inverter.pause_start", ("home_forecaster.battery.pause_start",), ""),
        ("inverter.pause_end", ("home_forecaster.battery.pause_end",), ""),
        ("inverter.charge_start_1", ("home_forecaster.battery.charge_start_1",), ""),
        ("inverter.charge_end_1", ("home_forecaster.battery.charge_end_1",), ""),
        ("inverter.charge_target_1", ("home_forecaster.battery.charge_target_1",), ""),
        ("inverter.charge_start_2", ("home_forecaster.battery.charge_start_2",), ""),
        ("inverter.charge_end_2", ("home_forecaster.battery.charge_end_2",), ""),
        ("inverter.charge_target_2", ("home_forecaster.battery.charge_target_2",), ""),
        ("inverter.discharge_start_1", ("home_forecaster.battery.discharge_start_1",), ""),
        ("inverter.discharge_end_1", ("home_forecaster.battery.discharge_end_1",), ""),
        ("inverter.discharge_target_1", ("home_forecaster.battery.discharge_target_1",), ""),
        ("inverter.discharge_start_2", ("home_forecaster.battery.discharge_start_2",), ""),
        ("inverter.discharge_end_2", ("home_forecaster.battery.discharge_end_2",), ""),
        ("inverter.discharge_target_2", ("home_forecaster.battery.discharge_target_2",), ""),

        ("tariff.import_current_rate", ("home_forecaster.tariff.import_current_rate",), ""),
        ("tariff.export_current_rate", ("home_forecaster.tariff.export_current_rate",), ""),
        ("tariff.import_current_day_rates", ("home_forecaster.tariff.import_current_day_rates",), ""),
        ("tariff.import_next_day_rates", ("home_forecaster.tariff.import_next_day_rates",), ""),
        ("tariff.export_current_day_rates", ("home_forecaster.tariff.export_current_day_rates",), ""),
        ("tariff.export_next_day_rates", ("home_forecaster.tariff.export_next_day_rates",), ""),

        ("solar.energy_total_kwh", ("home_forecaster.solar.energy_total_kwh",), ""),
        ("solar.solcast_today", ("home_forecaster.solar.solcast_today",), "sensor.solcast_pv_forecast_forecast_today"),
        ("solar.solcast_tomorrow", ("home_forecaster.solar.solcast_tomorrow",), "sensor.solcast_pv_forecast_forecast_tomorrow"),
        ("solar.solcast_day_3", ("home_forecaster.solar.solcast_day_3",), "sensor.solcast_pv_forecast_forecast_day_3"),

        ("heat_pump.ch_energy_total_kwh", ("ashp_forecaster.ch_energy_entity", "home_forecaster.ashp.ch_energy_total_kwh"), "sensor.ashp_electrical_energy_ch"),
        ("heat_pump.outdoor_temperature", ("ashp_forecaster.outdoor_temperature_entity",), "sensor.ecomax360i_outdoor_temperature"),
        ("heat_pump.weather", ("ashp_forecaster.weather_entity",), "weather.forecast_home"),
        ("heat_pump.summer_mode_off", ("ashp_forecaster.summer_mode_off_entity",), "number.ecomax360i_summer_mode_off"),
        ("heat_pump.summer_mode_on", ("ashp_forecaster.summer_mode_on_entity",), "number.ecomax360i_summer_mode_on"),

        ("dhw.energy_total_kwh", ("ashp_forecaster.dhw_energy_entity", "home_forecaster.ashp.dhw_energy_total_kwh"), "sensor.ashp_electrical_energy_dhw"),
        ("dhw.mode", ("ashp_forecaster.dhw_mode_entity",), "select.dhw_mode"),
        ("dhw.tank_temperature", ("ashp_forecaster.dhw_tank_temperature_entity",), "sensor.dhw_temperature"),
        ("dhw.tank_upper_temperature", ("ashp_forecaster.dhw_tank_upper_temperature_entity",), ""),
        ("dhw.tank_lower_temperature", ("ashp_forecaster.dhw_tank_lower_temperature_entity",), ""),
        ("dhw.ambient_temperature", ("ashp_forecaster.dhw_ambient_temperature_entity",), ""),
        ("dhw.target_temperature", ("ashp_forecaster.dhw_target_temperature_entity",), "number.dhw_target_temperature"),
        ("dhw.hysteresis", ("ashp_forecaster.dhw_hysteresis_entity",), "number.dhw_hysteresis"),
        ("dhw.schedule_prefix", ("ashp_forecaster.dhw_schedule_prefix",), "number.dhw_dhw_schedule_"),

        ("ev.energy_total_kwh", ("home_forecaster.ev.energy_total_kwh",), ""),
        ("ev.smart_charging_dispatch", ("home_forecaster.ev.smart_charging_dispatch_entity", "controller.ev_smart_charging_dispatch_entity"), ""),
        ("ev.smart_charging_active", ("home_forecaster.ev.smart_charging_active_entity", "controller.ev_smart_charging_active_entity"), ""),
        ("ev.smart_charging_enabled", ("controller.ev_smart_charging_enabled",), True),
    )
    for canonical_path, legacy_paths, default in mappings:
        _pick(raw, canonical, canonical_path, legacy_paths, diagnostics, default)

    controller = raw.get("controller") if isinstance(raw.get("controller"), dict) else {}
    strategy = canonical["energy_strategy"]
    canonical_strategy = raw.get("energy_strategy") if isinstance(raw.get("energy_strategy"), dict) else {}
    plumbing = {
        "status_entity_id",
        "home_energy_forecast_entity",
        "ev_smart_charging_enabled",
        "ev_smart_charging_dispatch_entity",
        "ev_smart_charging_active_entity",
    }
    for key, value in controller.items():
        if key not in plumbing:
            strategy[key] = value
    for key, value in canonical_strategy.items():
        if key in strategy and _nonempty(strategy[key]) and _nonempty(value) and strategy[key] != value:
            diagnostics.conflicts.append(
                f"energy_strategy.{key}: using canonical value {value!r}; legacy controller.{key}={strategy[key]!r}"
            )
        strategy[key] = value

    hf_settings = _get(raw, "home_forecaster.settings", {})
    if isinstance(hf_settings, dict):
        canonical["advanced"]["home_forecaster"] = {
            k: v for k, v in hf_settings.items()
            if k not in {"timezone", "controller_refresh_request_entity"}
        }
    ashp = raw.get("ashp_forecaster") if isinstance(raw.get("ashp_forecaster"), dict) else {}
    canonical["advanced"]["ashp_forecaster"] = {
        k: v for k, v in ashp.items()
        if k in {
            "base_temperature_c", "winter_mode_below_c", "summer_mode_above_c",
            "initial_kwh_per_degree_day", "training_days", "minimum_daily_degree_days",
            "minimum_daily_ch_kwh", "forecast_hours", "forecast_interval_minutes",
            "update_minutes", "dhw_tank_volume_l", "dhw_min_usable_temperature_c",
            "dhw_thermal_sample_minutes", "dhw_history_days", "dhw_activity_threshold_kwh",
        }
    }
    battery = _get(raw, "home_forecaster.battery", {})
    if isinstance(battery, dict):
        canonical["advanced"]["battery_learning"] = {
            k: v for k, v in battery.items()
            if k.startswith("battery_efficiency_") or k.startswith("battery_idle_")
        }
    canonical["advanced"]["mqtt"] = deepcopy(raw.get("mqtt", {})) if isinstance(raw.get("mqtt"), dict) else {}
    canonical["advanced"]["internal"] = {
        "forecast_refresh_request_entity": str(
            _get(raw, "home_forecaster.settings.controller_refresh_request_entity", "sensor.home_energy_forecast_refresh_request")
            or "sensor.home_energy_forecast_refresh_request"
        ),
        "home_energy_forecast_entity": str(
            _get(raw, "controller.home_energy_forecast_entity", "sensor.home_energy_forecast")
            or "sensor.home_energy_forecast"
        ),
        "controller_status_entity": str(
            _get(raw, "controller.status_entity_id", "sensor.home_energy_controller")
            or "sensor.home_energy_controller"
        ),
        "ashp_forecast_entity": str(
            _get(raw, "home_forecaster.ashp.forecast_48h", "sensor.ashp_forecast_next_48h")
            or "sensor.ashp_forecast_next_48h"
        ),
    }

    user_advanced = raw.get("advanced") if isinstance(raw.get("advanced"), dict) else {}
    for key, value in user_advanced.items():
        if isinstance(value, dict) and isinstance(canonical["advanced"].get(key), dict):
            canonical["advanced"][key].update(value)
        else:
            canonical["advanced"][key] = deepcopy(value)

    return canonical


def apply_canonical(runtime: dict[str, Any], canonical: dict[str, Any]) -> None:
    """Render canonical user-domain config into the unchanged component contracts."""
    hf = runtime.setdefault("home_forecaster", {})
    af = runtime.setdefault("ashp_forecaster", {})
    ctl = runtime.setdefault("controller", {})

    battery = canonical["battery"]
    inv = canonical["inverter"]
    grid = canonical["grid"]
    tariff = canonical["tariff"]
    solar = canonical["solar"]
    hp = canonical["heat_pump"]
    dhw = canonical["dhw"]
    ev = canonical["ev"]

    hb = hf.setdefault("battery", {})
    for src, dst in (
        ("soc", "soc"), ("capacity_kwh", "capacity_kwh"), ("reserve_soc", "reserve_soc"),
        ("power_w", "battery_power_w"), ("charge_energy_total_kwh", "battery_charge_energy_total_kwh"),
        ("discharge_energy_total_kwh", "battery_discharge_energy_total_kwh"),
    ):
        hb[dst] = battery.get(src, "")
    for key in (
        "max_rate_w", "charge_rate_w", "discharge_rate_w", "eco_mode", "charge_schedule_enabled",
        "discharge_schedule_enabled", "pause_mode", "pause_start", "pause_end", "charge_start_1",
        "charge_end_1", "charge_target_1", "charge_start_2", "charge_end_2", "charge_target_2",
        "discharge_start_1", "discharge_end_1", "discharge_target_1", "discharge_start_2",
        "discharge_end_2", "discharge_target_2",
    ):
        dst = "inverter_max_rate_w" if key == "max_rate_w" else key
        hb[dst] = inv.get(key, "")

    load = hf.setdefault("load", {})
    load["energy_total_kwh"] = inv.get("house_load_energy_total_kwh", "")

    hm = hf.setdefault("meter", {})
    hm["import_energy_total_kwh"] = grid.get("import_energy_total_kwh", "")
    hm["export_energy_total_kwh"] = grid.get("export_energy_total_kwh", "")

    ht = hf.setdefault("tariff", {})
    ht["import_energy_total_kwh"] = inv.get("import_energy_total_kwh", "")
    ht["export_energy_total_kwh"] = inv.get("export_energy_total_kwh", "")
    for key in (
        "import_current_rate", "export_current_rate", "import_current_day_rates", "import_next_day_rates",
        "export_current_day_rates", "export_next_day_rates",
    ):
        ht[key] = tariff.get(key, "")

    hs = hf.setdefault("solar", {})
    for key in ("energy_total_kwh", "solcast_today", "solcast_tomorrow", "solcast_day_3"):
        hs[key] = solar.get(key, "")

    advanced = canonical["advanced"]
    internal = advanced.get("internal", {}) if isinstance(advanced.get("internal"), dict) else {}

    ha = hf.setdefault("ashp", {})
    ha["forecast_48h"] = str(internal.get("ashp_forecast_entity") or "sensor.ashp_forecast_next_48h")
    ha["ch_energy_total_kwh"] = hp.get("ch_energy_total_kwh", "")
    ha["dhw_energy_total_kwh"] = dhw.get("energy_total_kwh", "")

    he = hf.setdefault("ev", {})
    he["energy_total_kwh"] = ev.get("energy_total_kwh", "")
    he["smart_charging_dispatch_entity"] = ev.get("smart_charging_dispatch", "")
    he["smart_charging_active_entity"] = ev.get("smart_charging_active", "")

    af["ch_energy_entity"] = hp.get("ch_energy_total_kwh", "")
    af["outdoor_temperature_entity"] = hp.get("outdoor_temperature", "")
    af["weather_entity"] = hp.get("weather", "")
    af["summer_mode_off_entity"] = hp.get("summer_mode_off", "")
    af["summer_mode_on_entity"] = hp.get("summer_mode_on", "")
    af["dhw_energy_entity"] = dhw.get("energy_total_kwh", "")
    af["dhw_mode_entity"] = dhw.get("mode", "")
    af["dhw_tank_temperature_entity"] = dhw.get("tank_temperature", "")
    af["dhw_tank_upper_temperature_entity"] = dhw.get("tank_upper_temperature", "")
    af["dhw_tank_lower_temperature_entity"] = dhw.get("tank_lower_temperature", "")
    af["dhw_ambient_temperature_entity"] = dhw.get("ambient_temperature", "")
    af["dhw_target_temperature_entity"] = dhw.get("target_temperature", "")
    af["dhw_hysteresis_entity"] = dhw.get("hysteresis", "")
    af["dhw_schedule_prefix"] = dhw.get("schedule_prefix", "")

    hfs = hf.setdefault("settings", {})
    for key, value in advanced.get("home_forecaster", {}).items():
        if key != "timezone":
            hfs[key] = value
    hfs["controller_refresh_request_entity"] = str(
        internal.get("forecast_refresh_request_entity") or "sensor.home_energy_forecast_refresh_request"
    )
    for key, value in advanced.get("ashp_forecaster", {}).items():
        af[key] = value
    for key, value in advanced.get("battery_learning", {}).items():
        hb[key] = value

    for key, value in canonical["energy_strategy"].items():
        ctl[key] = value
    ctl["status_entity_id"] = str(internal.get("controller_status_entity") or "sensor.home_energy_controller")
    ctl["home_energy_forecast_entity"] = str(
        internal.get("home_energy_forecast_entity") or "sensor.home_energy_forecast"
    )
    ctl["ev_smart_charging_enabled"] = bool(ev.get("smart_charging_enabled", True))
    ctl["ev_smart_charging_dispatch_entity"] = ev.get("smart_charging_dispatch", "")
    ctl["ev_smart_charging_active_entity"] = ev.get("smart_charging_active", "")


def migrate_runtime_options(raw_options: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], MigrationDiagnostics]:
    """Return component-compatible runtime options plus canonical diagnostics.

    The caller's object is never mutated. No data is written back to /data/options.json.
    """
    runtime = deepcopy(raw_options)
    _normalise_legacy_home_forecaster(runtime)
    _legacy_controller_aliases(runtime)
    diagnostics = MigrationDiagnostics()
    canonical = build_canonical(runtime, diagnostics)
    apply_canonical(runtime, canonical)
    return runtime, canonical, diagnostics
