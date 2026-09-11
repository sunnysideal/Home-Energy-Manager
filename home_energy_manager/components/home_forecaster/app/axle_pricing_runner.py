#!/usr/bin/env python3
"""Home Forecaster entry point with Axle export-reward pricing.

The base forecaster continues to own all physical load/PV/battery simulation. This
wrapper changes only the monetary value of grid export that overlaps a
package-normalised Axle Export event. Forecast export is repriced at the Axle
reward rate, while realised export is measured from the authoritative cumulative
grid export meter and retained in the Home Forecaster store for the rest of the
day. It never changes battery behaviour.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

import main as base

AXLE_ENTITY = "sensor.home_energy_manager_axle"
AXLE_EXPORT_REWARD_P_PER_KWH = 100.0
AXLE_ACTUAL_META_PREFIX = "axle_actual_rewards:"


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _overlap_fraction(start: datetime, end: datetime, event_start: datetime, event_end: datetime) -> float:
    a = max(start.astimezone(timezone.utc), event_start.astimezone(timezone.utc))
    b = min(end.astimezone(timezone.utc), event_end.astimezone(timezone.utc))
    total = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    if total <= 0 or b <= a:
        return 0.0
    return max(0.0, min(1.0, (b - a).total_seconds() / total))


def _effective_rate(normal_rate: Any, overlap_fraction: float, reward_rate: float) -> float | None:
    try:
        normal = float(normal_rate) if normal_rate is not None else 0.0
    except (TypeError, ValueError):
        normal = 0.0
    if overlap_fraction <= 0:
        return float(normal_rate) if normal_rate is not None else None
    return normal * (1.0 - overlap_fraction) + reward_rate * overlap_fraction


def _reprice_slots(slots: list[dict[str, Any]], event_start: datetime, event_end: datetime, reward_rate: float) -> None:
    for index, slot in enumerate(slots):
        start = _dt(slot.get("start"))
        if not start:
            continue
        next_start = _dt(slots[index + 1].get("start")) if index + 1 < len(slots) else None
        end = next_start or (start + timedelta(minutes=30))
        fraction = _overlap_fraction(start, end, event_start, event_end)
        if fraction <= 0:
            continue
        original_rate = slot.get("export_rate_p")
        effective_rate = _effective_rate(original_rate, fraction, reward_rate)
        slot["base_export_rate_p"] = original_rate
        slot["export_rate_p"] = round(effective_rate, 2) if effective_rate is not None else None
        slot["axle_export_reward"] = True
        slot["axle_overlap_minutes"] = round((end - start).total_seconds() / 60.0 * fraction, 1)
        imp = float(slot.get("import_kwh") or 0.0)
        exp = float(slot.get("export_kwh") or 0.0)
        ir = float(slot.get("import_rate_p") or 0.0)
        er = float(effective_rate or 0.0)
        slot["cost_p"] = round(imp * ir - exp * er, 2)


def _day_costs(slots: list[dict[str, Any]], day, tz) -> tuple[float, float, float]:
    selected = [s for s in slots if (_dt(s.get("start")) and _dt(s.get("start")).astimezone(tz).date() == day)]
    import_cost = sum(float(s.get("import_kwh") or 0.0) * float(s.get("import_rate_p") or 0.0) for s in selected)
    export_income = sum(float(s.get("export_kwh") or 0.0) * float(s.get("export_rate_p") or 0.0) for s in selected)
    return round(import_cost, 2), round(export_income, 2), round(import_cost - export_income, 2)


def _event_key(event_start: datetime, event_end: datetime) -> str:
    return f"{event_start.astimezone(timezone.utc).isoformat()}|{event_end.astimezone(timezone.utc).isoformat()}"


def _meta_key(day) -> str:
    return f"{AXLE_ACTUAL_META_PREFIX}{day.isoformat()}"


def _load_actual_records(store, day) -> dict[str, dict[str, Any]]:
    raw = store.meta_get(_meta_key(day), "{}")
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _save_actual_records(store, day, records: dict[str, dict[str, Any]]) -> None:
    store.meta_set(_meta_key(day), json.dumps(records, separators=(",", ":"), sort_keys=True))


def _export_rates_for_actual(client, cfg, day, tz) -> list[dict[str, Any]]:
    tariff = cfg.section("tariff")
    rates: list[dict[str, Any]] = []
    for key in ("export_current_day_rates", "export_next_day_rates"):
        entity = str(tariff.get(key) or "").strip()
        if entity:
            rates.extend(base.parse_rates(client.state_optional(entity)))
    if rates:
        return rates

    current_entity = str(tariff.get("export_current_rate") or "").strip()
    current = base.normalise_rate_p(base.numeric_state(client.state_optional(current_entity))) if current_entity else None
    if current is None:
        return []
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    return [{"start": start, "end": start + timedelta(days=1), "rate_p": current}]


def _normal_export_income(
    states: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    export_rates: list[dict[str, Any]],
) -> float:
    if end <= start or not export_rates:
        return 0.0
    boundaries = {start.astimezone(timezone.utc), end.astimezone(timezone.utc)}
    for rate in export_rates:
        rs = rate["start"].astimezone(timezone.utc)
        re = rate["end"].astimezone(timezone.utc)
        if start.astimezone(timezone.utc) < rs < end.astimezone(timezone.utc):
            boundaries.add(rs)
        if start.astimezone(timezone.utc) < re < end.astimezone(timezone.utc):
            boundaries.add(re)
    points = sorted(boundaries)
    income = 0.0
    for a_utc, b_utc in zip(points[:-1], points[1:]):
        a = a_utc.astimezone(start.tzinfo)
        b = b_utc.astimezone(start.tzinfo)
        energy = base.diff_cumulative(states, a, b)
        if energy is None:
            continue
        rate = base.closest_rate_at(export_rates, a, b)
        if rate is not None:
            income += energy * rate
    return income


def _measure_actual_event(
    client,
    cfg,
    event_start: datetime,
    event_end: datetime,
    now: datetime,
    reward_rate: float,
) -> dict[str, Any] | None:
    tz = now.tzinfo or timezone.utc
    day = now.astimezone(tz).date()
    day_start = datetime.combine(day, dtime.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    measured_start = max(event_start.astimezone(tz), day_start)
    measured_end = min(event_end.astimezone(tz), now.astimezone(tz), day_end)
    if measured_end <= measured_start:
        return None

    _, export_entity, _, export_source = base.effective_grid_energy_entities(client, cfg)
    if not export_entity:
        return None
    history_start = measured_start - timedelta(hours=2)
    hist = client.history([export_entity], history_start, measured_end, timeout=45)
    states = hist.get(export_entity, [])
    export_kwh = base.diff_cumulative(states, measured_start, measured_end)
    if export_kwh is None:
        return None

    export_rates = _export_rates_for_actual(client, cfg, day, tz)
    normal_income = _normal_export_income(states, measured_start, measured_end, export_rates)
    axle_income = export_kwh * reward_rate
    replacement_delta = axle_income - normal_income
    return {
        "event_start": event_start.isoformat(),
        "event_end": event_end.isoformat(),
        "measured_start": measured_start.isoformat(),
        "measured_end": measured_end.isoformat(),
        "export_entity": export_entity,
        "export_source": export_source,
        "export_kwh": round(export_kwh, 5),
        "normal_export_income_p": round(normal_income, 4),
        "axle_export_income_p": round(axle_income, 4),
        "replacement_delta_p": round(replacement_delta, 4),
        "reward_rate_p_per_kwh": reward_rate,
    }


def _update_actual_records(store, client, cfg, axle_attrs: dict[str, Any], now: datetime, reward_rate: float) -> dict[str, Any]:
    tz = now.tzinfo or timezone.utc
    day = now.astimezone(tz).date()
    records = _load_actual_records(store, day)
    event_start = _dt(axle_attrs.get("start"))
    event_end = _dt(axle_attrs.get("end"))
    event_type = str(axle_attrs.get("event_type") or "").lower()
    if event_type == "export" and event_start and event_end and event_end > event_start and now.astimezone(timezone.utc) > event_start.astimezone(timezone.utc):
        measured = _measure_actual_event(client, cfg, event_start, event_end, now, reward_rate)
        if measured is not None:
            records[_event_key(event_start, event_end)] = measured
            _save_actual_records(store, day, records)

    export_kwh = sum(float(item.get("export_kwh") or 0.0) for item in records.values())
    normal_income = sum(float(item.get("normal_export_income_p") or 0.0) for item in records.values())
    axle_income = sum(float(item.get("axle_export_income_p") or 0.0) for item in records.values())
    replacement_delta = sum(float(item.get("replacement_delta_p") or 0.0) for item in records.values())
    return {
        "event_count": len(records),
        "export_kwh": round(export_kwh, 5),
        "normal_export_income_p": round(normal_income, 2),
        "axle_export_income_p": round(axle_income, 2),
        "replacement_delta_p": round(replacement_delta, 2),
    }


def _apply_actual_credit(payload: dict[str, Any], actual_credit: dict[str, Any]) -> None:
    delta = float(actual_credit.get("replacement_delta_p") or 0.0)
    if abs(delta) < 0.000001:
        return
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if not isinstance(attrs, dict):
        return
    today = attrs.get("today") if isinstance(attrs.get("today"), dict) else None
    if not isinstance(today, dict):
        return
    actual = today.get("actual") if isinstance(today.get("actual"), dict) else None
    total = today.get("total") if isinstance(today.get("total"), dict) else None
    if not isinstance(actual, dict) or not isinstance(total, dict):
        return

    actual["export_income_p"] = round(float(actual.get("export_income_p") or 0.0) + delta, 2)
    actual["cost_p"] = round(float(actual.get("import_cost_p") or 0.0) - actual["export_income_p"], 2)
    total["export_income_p"] = round(float(total.get("export_income_p") or 0.0) + delta, 2)
    total["cost_p"] = round(float(total.get("import_cost_p") or 0.0) - total["export_income_p"], 2)


def apply_axle_pricing(payload: dict[str, Any], axle_state: dict[str, Any] | None, now: datetime) -> bool:
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    axle_attrs = axle_state.get("attributes") if isinstance(axle_state, dict) else None
    if not isinstance(attrs, dict) or not isinstance(axle_attrs, dict):
        return False
    if str(axle_attrs.get("event_type") or "").lower() != "export":
        return False
    event_start = _dt(axle_attrs.get("start")); event_end = _dt(axle_attrs.get("end"))
    if not event_start or not event_end or event_end <= event_start:
        return False
    if not bool(axle_attrs.get("event_available")):
        return False

    reward = AXLE_EXPORT_REWARD_P_PER_KWH
    forecast = attrs.get("forecast") if isinstance(attrs.get("forecast"), list) else []
    no_slots = attrs.get("forecast_no_slots") if isinstance(attrs.get("forecast_no_slots"), list) else []
    _reprice_slots(forecast, event_start, event_end, reward)
    _reprice_slots(no_slots, event_start, event_end, reward)

    tz = now.tzinfo or timezone.utc
    today_import, today_export, today_cost = _day_costs(forecast, now.date(), tz)
    tomorrow_import, tomorrow_export, tomorrow_cost = _day_costs(forecast, now.date() + timedelta(days=1), tz)
    today = attrs.get("today") if isinstance(attrs.get("today"), dict) else {}
    today_fc = today.get("forecast") if isinstance(today.get("forecast"), dict) else {}
    today_fc.update({"import_cost_p": today_import, "export_income_p": today_export, "cost_p": today_cost})
    actual = today.get("actual") if isinstance(today.get("actual"), dict) else {}
    total = today.get("total") if isinstance(today.get("total"), dict) else {}
    total["import_cost_p"] = round(float(actual.get("import_cost_p") or 0.0) + today_import, 2)
    total["export_income_p"] = round(float(actual.get("export_income_p") or 0.0) + today_export, 2)
    total["cost_p"] = round(total["import_cost_p"] - total["export_income_p"], 2)
    tomorrow = attrs.get("tomorrow") if isinstance(attrs.get("tomorrow"), dict) else {}
    tomorrow.update({"import_cost_p": tomorrow_import, "export_income_p": tomorrow_export, "cost_p": tomorrow_cost})

    no_totals = attrs.get("no_slots_totals") if isinstance(attrs.get("no_slots_totals"), dict) else {}
    for label, day in (("today", now.date()), ("tomorrow", now.date() + timedelta(days=1))):
        _, _, cost = _day_costs(no_slots, day, tz)
        if isinstance(no_totals.get(label), dict):
            no_totals[label]["cost_p"] = cost

    attrs["axle_pricing"] = {
        "entity": AXLE_ENTITY,
        "event_type": "export",
        "start": event_start.isoformat(),
        "end": event_end.isoformat(),
        "reward_rate_p_per_kwh": reward,
        "pricing_applied": True,
        "physical_plan_modified": False,
    }
    return True


_base_make_forecast = base.make_forecast


def make_forecast_with_axle(client, store, cfg, now):
    payload, reasons = _base_make_forecast(client, store, cfg, now)
    axle_state = client.state_optional(AXLE_ENTITY)
    axle_attrs = axle_state.get("attributes") if isinstance(axle_state, dict) else {}

    actual_credit = _update_actual_records(store, client, cfg, axle_attrs if isinstance(axle_attrs, dict) else {}, now, AXLE_EXPORT_REWARD_P_PER_KWH)
    _apply_actual_credit(payload, actual_credit)

    pricing_applied = apply_axle_pricing(payload, axle_state, now)
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if isinstance(attrs, dict):
        pricing = attrs.get("axle_pricing") if isinstance(attrs.get("axle_pricing"), dict) else {}
        pricing["actual"] = actual_credit
        pricing["actual_meter_based"] = True
        pricing["actual_credit_persisted_for_day"] = True
        attrs["axle_pricing"] = pricing

    if float(actual_credit.get("replacement_delta_p") or 0.0) != 0.0:
        reasons.append(
            "Realised Axle Export income retained from true grid export meter: "
            f"{actual_credit['export_kwh']:.3f}kWh, {actual_credit['axle_export_income_p']:.2f}p at 100p/kWh"
        )
    if pricing_applied:
        reasons.append("Axle Export reward pricing applied to overlapping forecast export at 100p/kWh")
    return payload, reasons


base.make_forecast = make_forecast_with_axle

if __name__ == "__main__":
    base.main()
