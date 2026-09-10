#!/usr/bin/env python3
"""Home Forecaster entry point with Axle export-reward pricing.

The base forecaster continues to own all physical load/PV/battery simulation. This
wrapper only changes the monetary value of forecast export that overlaps a
package-normalised Axle Export event. It never changes battery behaviour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import main as base

AXLE_ENTITY = "sensor.home_energy_manager_axle"
AXLE_EXPORT_REWARD_P_PER_KWH = 100.0


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
    # event_available is true for upcoming/active events. A finished event is not
    # used for forecast repricing; historical actual accounting remains unchanged.
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
    if apply_axle_pricing(payload, axle_state, now):
        reasons.append("Axle Export reward pricing applied to overlapping forecast export at 100p/kWh")
    return payload, reasons


base.make_forecast = make_forecast_with_axle

if __name__ == "__main__":
    base.main()
