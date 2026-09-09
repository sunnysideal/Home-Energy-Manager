"""Learn passive two-zone DHW loss/coupling parameters from quiet observations."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WATER_KWH_PER_LITRE_C = 4.186 / 3600.0


@dataclass(frozen=True)
class PassiveFit:
    upper_loss_w_per_k: float
    lower_loss_w_per_k: float
    coupling_w_per_k: float
    sample_count: int
    rmse_c: float


def _solve_3x3(a: list[list[float]], b: list[float]) -> list[float] | None:
    m = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-10:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        scale = m[col][col]
        for j in range(col, 4):
            m[col][j] /= scale
        for row in range(3):
            if row == col:
                continue
            factor = m[row][col]
            for j in range(col, 4):
                m[row][j] -= factor * m[col][j]
    return [m[i][3] for i in range(3)]


def _least_squares(rows: list[tuple[list[float], float]]) -> list[float] | None:
    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    for x, y in rows:
        for i in range(3):
            atb[i] += x[i] * y
            for j in range(3):
                ata[i][j] += x[i] * x[j]
    return _solve_3x3(ata, atb)


def _quiet_intervals(
    db: sqlite3.Connection,
    *,
    volume_l: float,
    upper_fraction: float,
    default_ambient_c: float,
    history_days: int,
) -> list[tuple[list[float], float, tuple[float, float, float, float, float]]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).isoformat()
    samples = list(db.execute(
        "SELECT timestamp,upper_temp_c,lower_temp_c,dhw_heating,immersion_heating,"
        "ambient_temp_c,valid FROM dhw_thermal_samples "
        "WHERE timestamp>=? AND upper_temp_c IS NOT NULL AND lower_temp_c IS NOT NULL "
        "ORDER BY timestamp",
        (cutoff,),
    ))
    draw_times = {
        str(row[0]) for row in db.execute(
            "SELECT timestamp FROM dhw_draw_events WHERE timestamp>=?", (cutoff,)
        )
    }
    upper_cap = volume_l * upper_fraction * WATER_KWH_PER_LITRE_C
    lower_cap = volume_l * (1.0 - upper_fraction) * WATER_KWH_PER_LITRE_C
    rows: list[tuple[list[float], float, tuple[float, float, float, float, float]]] = []

    for previous, current in zip(samples, samples[1:]):
        try:
            prev_ts = datetime.fromisoformat(str(previous[0]))
            cur_ts = datetime.fromisoformat(str(current[0]))
            pu, pl = float(previous[1]), float(previous[2])
            cu, cl = float(current[1]), float(current[2])
        except (TypeError, ValueError):
            continue
        seconds = (cur_ts - prev_ts).total_seconds()
        if seconds < 60.0 or seconds > 15.0 * 60.0:
            continue
        if not bool(previous[6]) or not bool(current[6]):
            continue
        if bool(previous[3]) or bool(current[3]) or bool(previous[4]) or bool(current[4]):
            continue
        if str(current[0]) in draw_times:
            continue
        # Conservative quiet-period gate. Normal standing loss/coupling is much slower;
        # larger changes are likely draws, heating transitions or sensor artefacts.
        if abs(cu - pu) > 0.60 or abs(cl - pl) > 0.60:
            continue
        ambient_raw = previous[5]
        ambient = float(ambient_raw) if ambient_raw is not None else default_ambient_c
        if not all(math.isfinite(v) for v in (pu, pl, cu, cl, ambient)):
            continue
        if pu < pl - 0.5:
            continue

        hours = seconds / 3600.0
        scale = hours / 1000.0
        upper_ambient = max(pu - ambient, 0.0)
        lower_ambient = max(pl - ambient, 0.0)
        gap = max(pu - pl, 0.0)
        upper_drop_kwh = upper_cap * (pu - cu)
        lower_drop_kwh = lower_cap * (pl - cl)

        # Upper: drop = K_upper*dT_ambient + K_coupling*dT_zone
        rows.append(([upper_ambient * scale, 0.0, gap * scale], upper_drop_kwh,
                     (pu, pl, cu, cl, hours)))
        # Lower: drop = K_lower*dT_ambient - K_coupling*dT_zone
        rows.append(([0.0, lower_ambient * scale, -gap * scale], lower_drop_kwh,
                     (pu, pl, cu, cl, hours)))
    return rows


def learn_passive_parameters(
    db: sqlite3.Connection,
    *,
    volume_l: float = 250.0,
    upper_fraction: float = 0.45,
    default_ambient_c: float = 20.0,
    history_days: int = 14,
    minimum_intervals: int = 24,
) -> PassiveFit | None:
    if volume_l <= 0 or not 0.1 <= upper_fraction <= 0.9:
        return None
    observations = _quiet_intervals(
        db,
        volume_l=volume_l,
        upper_fraction=upper_fraction,
        default_ambient_c=default_ambient_c,
        history_days=history_days,
    )
    interval_count = len(observations) // 2
    if interval_count < minimum_intervals:
        return None
    solution = _least_squares([(x, y) for x, y, _ in observations])
    if solution is None:
        return None
    upper_loss, lower_loss, coupling = solution
    if not all(math.isfinite(v) for v in solution):
        return None
    # Physical plausibility bounds prevent a small/noisy dataset from creating a
    # parameter set that could later destabilise simulation.
    if not (0.0 <= upper_loss <= 10.0 and 0.0 <= lower_loss <= 10.0 and 0.0 <= coupling <= 30.0):
        return None

    upper_cap = volume_l * upper_fraction * WATER_KWH_PER_LITRE_C
    lower_cap = volume_l * (1.0 - upper_fraction) * WATER_KWH_PER_LITRE_C
    squared_errors: list[float] = []
    for x, y, context in observations:
        predicted = sum(coef * value for coef, value in zip(solution, x))
        pu, pl, cu, cl, _ = context
        capacity = upper_cap if x[0] else lower_cap
        if capacity > 0:
            squared_errors.append(((y - predicted) / capacity) ** 2)
    rmse = math.sqrt(sum(squared_errors) / len(squared_errors)) if squared_errors else 0.0
    return PassiveFit(upper_loss, lower_loss, coupling, interval_count, rmse)


def persist_passive_fit(db: sqlite3.Connection, fit: PassiveFit) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db:
        for name, value in (
            ("upper_loss_w_per_k", fit.upper_loss_w_per_k),
            ("lower_loss_w_per_k", fit.lower_loss_w_per_k),
            ("coupling_w_per_k", fit.coupling_w_per_k),
        ):
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "value=excluded.value,sample_count=excluded.sample_count,"
                "updated_at=excluded.updated_at,error=excluded.error",
                (name, value, fit.sample_count, now, fit.rmse_c),
            )
