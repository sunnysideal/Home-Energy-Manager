"""Learn electrical energy required for normal ASHP DHW heating cycles."""
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
        "SELECT start_upper_c,start_lower_c,target_temp_c,electrical_kwh "
        "FROM dhw_heating_cycles WHERE valid=1 AND cycle_type='normal_dhw' "
        "AND target_temp_c IS NOT NULL AND electrical_kwh IS NOT NULL "
        "ORDER BY start_ts DESC LIMIT 60"
    ).fetchall()
    samples: list[tuple[float, float, float]] = []
    for row in rows:
        try:
            upper = float(row[0]); lower = float(row[1]); target = float(row[2]); energy = float(row[3])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (upper, lower, target, energy)):
            continue
        upper_deficit = max(target - upper, 0.0)
        lower_deficit = max(target - lower, 0.0)
        if energy <= 0.0 or energy > 20.0 or upper_deficit + lower_deficit < 0.5:
            continue
        samples.append((upper_deficit, lower_deficit, energy))
    if len(samples) < minimum_cycles:
        return None

    # Small ridge term prevents unstable fits while the training set is still small.
    ridge = 1e-3
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for upper_deficit, lower_deficit, energy in samples:
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

    # Negative temperature-deficit coefficients are physically implausible here.
    if upper_coef < 0.0 or lower_coef < 0.0:
        return None
    if intercept < -1.0 or intercept > 5.0 or upper_coef > 1.0 or lower_coef > 1.0:
        return None

    errors = []
    for upper_deficit, lower_deficit, energy in samples:
        predicted = max(0.0, intercept + upper_coef * upper_deficit + lower_coef * lower_deficit)
        errors.append((predicted - energy) ** 2)
    rmse = math.sqrt(sum(errors) / len(errors))
    return CycleEnergyFit(intercept, upper_coef, lower_coef, len(samples), rmse)


def persist_cycle_energy_fit(db: sqlite3.Connection, fit: CycleEnergyFit) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("dhw_cycle_intercept_kwh", fit.intercept_kwh),
        ("dhw_cycle_upper_kwh_per_c", fit.upper_kwh_per_c),
        ("dhw_cycle_lower_kwh_per_c", fit.lower_kwh_per_c),
    ]
    with db:
        for name, value in rows:
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                "sample_count=excluded.sample_count,updated_at=excluded.updated_at,error=excluded.error",
                (name, value, fit.sample_count, now, fit.rmse_kwh),
            )
