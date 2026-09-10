"""Split DHW temperature forecast error by observed operating state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3


@dataclass(frozen=True)
class StateMetric:
    state: str
    count: int
    upper_mae_c: float | None
    lower_mae_c: float | None


def _dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def _state_for_target(db: sqlite3.Connection, target: datetime) -> str:
    sample = db.execute(
        "SELECT timestamp,dhw_heating FROM dhw_thermal_samples "
        "WHERE valid=1 AND timestamp<=? ORDER BY timestamp DESC LIMIT 1",
        (target.isoformat(),),
    ).fetchone()
    if sample is not None:
        try:
            sample_ts = _dt(sample[0])
            if bool(sample[1]) and target - sample_ts <= timedelta(minutes=10):
                return "heating"
        except ValueError:
            pass

    cycle = db.execute(
        "SELECT end_ts FROM dhw_heating_cycles WHERE valid=1 AND end_ts<=? "
        "ORDER BY end_ts DESC LIMIT 1",
        (target.isoformat(),),
    ).fetchone()
    if cycle is not None:
        try:
            if target - _dt(cycle[0]) <= timedelta(minutes=60):
                return "post_heating"
        except ValueError:
            pass

    draw = db.execute(
        "SELECT timestamp FROM dhw_draw_events WHERE timestamp<=? "
        "ORDER BY timestamp DESC LIMIT 1",
        (target.isoformat(),),
    ).fetchone()
    if draw is not None:
        try:
            if target - _dt(draw[0]) <= timedelta(minutes=60):
                return "draw_or_post_draw"
        except ValueError:
            pass

    return "passive_no_draw"


def state_metrics(
    db: sqlite3.Connection,
    *,
    lookback_days: int = 7,
    max_horizon_hours: float = 12.0,
) -> list[StateMetric]:
    """Return 0-12h temperature MAE split by actual tank operating state.

    State precedence is heating, post-heating, draw/post-draw, then passive/no-draw.
    Post-event windows are one hour so the result highlights where model error is introduced,
    without letting a single event contaminate the rest of the horizon.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT forecast_ts,target_ts,predicted_upper_c,predicted_lower_c,"
        "actual_upper_c,actual_lower_c FROM dhw_forecast_validation "
        "WHERE forecast_ts>=? AND actual_upper_c IS NOT NULL AND actual_lower_c IS NOT NULL",
        (cutoff,),
    ).fetchall()
    names = ("passive_no_draw", "draw_or_post_draw", "heating", "post_heating")
    errors: dict[str, list[tuple[float, float]]] = {name: [] for name in names}

    for row in rows:
        try:
            forecast = _dt(row[0])
            target = _dt(row[1])
            horizon_h = (target - forecast).total_seconds() / 3600.0
            if not 0.0 <= horizon_h <= max_horizon_hours:
                continue
            upper_error = abs(float(row[2]) - float(row[4]))
            lower_error = abs(float(row[3]) - float(row[5]))
        except (TypeError, ValueError):
            continue
        errors[_state_for_target(db, target)].append((upper_error, lower_error))

    out: list[StateMetric] = []
    for name in names:
        values = errors[name]
        if not values:
            out.append(StateMetric(name, 0, None, None))
            continue
        out.append(
            StateMetric(
                name,
                len(values),
                sum(value[0] for value in values) / len(values),
                sum(value[1] for value in values) / len(values),
            )
        )
    return out
