"""Learn electrical energy and thermal response for normal ASHP DHW cycles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import sqlite3


@dataclass(frozen=True)
class CycleEnergyFit:
    intercept_kwh: float
    upper_kwh_per_c: float
    lower_kwh_per_c: float
    typical_power_kw: float
    upper_c_per_kwh: float
    lower_c_per_kwh: float
    sample_count: int
    rmse_kwh: float

    def predict(self, upper_temp_c: float, lower_temp_c: float, target_temp_c: float) -> float:
        upper_deficit = max(target_temp_c - upper_temp_c, 0.0)
        lower_deficit = max(target_temp_c - lower_temp_c, 0.0)
        return max(
            0.0,
            self.intercept_kwh
            + self.upper_kwh_per_c * upper_deficit
            + self.lower_kwh_per_c * lower_deficit,
        )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _solve_3x3(a: list[list[float]], b: list[float]) -> list[float] | None:
    m = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-9:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        scale = m[col][col]
        m[col] = [value / scale for value in m[col]]
        for row in range(3):
            if row == col:
                continue
            factor = m[row][col]
            m[row] = [x - factor * y for x, y in zip(m[row], m[col])]
    return [m[i][3] for i in range(3)]


def learn_cycle_energy(db: sqlite3.Connection, *, minimum_cycles: int = 5) -> CycleEnergyFit | None:
    rows = db.execute(
        "SELECT start_ts,end_ts,start_upper_c,start_lower_c,end_upper_c,end_lower_c,"
        "target_temp_c,electrical_kwh FROM dhw_heating_cycles "
        "WHERE valid=1 AND cycle_type='normal_dhw' AND target_temp_c IS NOT NULL "
        "AND electrical_kwh IS NOT NULL ORDER BY start_ts DESC LIMIT 60"
    ).fetchall()
    samples: list[tuple[float, float, float, float, float, float]] = []
    for row in rows:
        try:
            start_ts = datetime.fromisoformat(str(row[0]))
            end_ts = datetime.fromisoformat(str(row[1]))
            upper = float(row[2]); lower = float(row[3])
            end_upper = float(row[4]); end_lower = float(row[5])
            target = float(row[6]); energy = float(row[7])
        except (TypeError, ValueError):
            continue
        values = (upper, lower, end_upper, end_lower, target, energy)
        if not all(math.isfinite(v) for v in values):
            continue
        duration_h = (end_ts - start_ts).total_seconds() / 3600.0
        upper_deficit = max(target - upper, 0.0)
        lower_deficit = max(target - lower, 0.0)
        if energy <= 0.0 or energy > 20.0 or duration_h <= 0.0 or duration_h > 6.0:
            continue
        if upper_deficit + lower_deficit < 0.5:
            continue
        upper_rise = max(end_upper - upper, 0.0)
        lower_rise = max(end_lower - lower, 0.0)
        samples.append((upper_deficit, lower_deficit, energy, duration_h, upper_rise, lower_rise))
    if len(samples) < minimum_cycles:
        return None

    ridge = 1e-3
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for upper_deficit, lower_deficit, energy, _, _, _ in samples:
        x = [1.0, upper_deficit, lower_deficit]
        for i in range(3):
            xty[i] += x[i] * energy
            for j in range(3):
                xtx[i][j] += x[i] * x[j]
    for i in range(3):
        xtx[i][i] += ridge

    solved = _solve_3x3(xtx, xty)
    if solved is None:
        return None
    intercept, upper_coef, lower_coef = solved
    if upper_coef < 0.0 or lower_coef < 0.0:
        return None
    if intercept < -1.0 or intercept > 5.0 or upper_coef > 1.0 or lower_coef > 1.0:
        return None

    power_values = [energy / duration_h for _, _, energy, duration_h, _, _ in samples]
    upper_response = [upper_rise / energy for _, _, energy, _, upper_rise, _ in samples if upper_rise > 0]
    lower_response = [lower_rise / energy for _, _, energy, _, _, lower_rise in samples if lower_rise > 0]
    typical_power_kw = _median(power_values)
    upper_c_per_kwh = _median(upper_response)
    lower_c_per_kwh = _median(lower_response)
    if not (0.1 <= typical_power_kw <= 10.0):
        return None
    if not (0.0 < upper_c_per_kwh <= 30.0 and 0.0 < lower_c_per_kwh <= 30.0):
        return None

    errors = []
    for upper_deficit, lower_deficit, energy, _, _, _ in samples:
        predicted = max(0.0, intercept + upper_coef * upper_deficit + lower_coef * lower_deficit)
        errors.append((predicted - energy) ** 2)
    rmse = math.sqrt(sum(errors) / len(errors))
    return CycleEnergyFit(
        intercept, upper_coef, lower_coef, typical_power_kw,
        upper_c_per_kwh, lower_c_per_kwh, len(samples), rmse,
    )


def persist_cycle_energy_fit(db: sqlite3.Connection, fit: CycleEnergyFit) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("dhw_cycle_intercept_kwh", fit.intercept_kwh),
        ("dhw_cycle_upper_kwh_per_c", fit.upper_kwh_per_c),
        ("dhw_cycle_lower_kwh_per_c", fit.lower_kwh_per_c),
        ("dhw_cycle_power_kw", fit.typical_power_kw),
        ("dhw_cycle_upper_c_per_kwh", fit.upper_c_per_kwh),
        ("dhw_cycle_lower_c_per_kwh", fit.lower_c_per_kwh),
    ]
    with db:
        for name, value in rows:
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                "sample_count=excluded.sample_count,updated_at=excluded.updated_at,error=excluded.error",
                (name, value, fit.sample_count, now, fit.rmse_kwh),
            )
