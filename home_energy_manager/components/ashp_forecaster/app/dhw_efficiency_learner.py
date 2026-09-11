"""Learn a shadow temperature-dependent DHW electrical-cost curve.

This learner is diagnostic only.  It reads ASHP-owned DHW thermal samples and derives a
relative electrical-cost multiplier versus effective tank temperature.  It deliberately
does not alter the production DHW forecast; the existing cycle-energy model remains the
absolute calibration anchor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import sqlite3


BIN_WIDTH_C = 5.0
MIN_INTERVAL_RISE_C = 0.03
MAX_INTERVAL_RISE_C = 3.0
MIN_BIN_SAMPLES = 4
MIN_TOTAL_SAMPLES = 20
MIN_POPULATED_BINS = 3


@dataclass(frozen=True)
class EfficiencyBin:
    center_c: float
    raw_cost_kwh_per_c: float
    multiplier: float
    sample_count: int


@dataclass(frozen=True)
class TemperatureEfficiencyFit:
    bins: tuple[EfficiencyBin, ...]
    sample_count: int
    min_temp_c: float
    max_temp_c: float
    rmse_multiplier: float

    def multiplier_at(self, temperature_c: float) -> float:
        if not self.bins:
            return 1.0
        if temperature_c <= self.bins[0].center_c:
            return self.bins[0].multiplier
        if temperature_c >= self.bins[-1].center_c:
            return self.bins[-1].multiplier
        for left, right in zip(self.bins, self.bins[1:]):
            if left.center_c <= temperature_c <= right.center_c:
                span = right.center_c - left.center_c
                fraction = 0.0 if span <= 0.0 else (temperature_c - left.center_c) / span
                return left.multiplier + fraction * (right.multiplier - left.multiplier)
        return 1.0

    def integrated_multiplier(self, start_c: float, target_c: float, *, step_c: float = 0.25) -> float:
        """Return the multiplier-weighted temperature lift in degrees C."""
        if target_c <= start_c:
            return 0.0
        cursor = start_c
        total = 0.0
        while cursor < target_c:
            nxt = min(target_c, cursor + step_c)
            midpoint = (cursor + nxt) / 2.0
            total += (nxt - cursor) * self.multiplier_at(midpoint)
            cursor = nxt
        return total


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _isotonic_non_decreasing(values: list[float], weights: list[int]) -> list[float]:
    """Weighted pool-adjacent-violators fit."""
    blocks: list[list[float]] = []  # [start, end, weighted_value, weight]
    for idx, (value, weight) in enumerate(zip(values, weights)):
        blocks.append([float(idx), float(idx), float(value), float(weight)])
        while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            total_weight = left[3] + right[3]
            merged = (left[2] * left[3] + right[2] * right[3]) / total_weight
            blocks.append([left[0], right[1], merged, total_weight])
    out = [0.0] * len(values)
    for start, end, value, _ in blocks:
        for idx in range(int(start), int(end) + 1):
            out[idx] = value
    return out


def learn_temperature_efficiency(db: sqlite3.Connection) -> TemperatureEfficiencyFit | None:
    rows = db.execute(
        "SELECT timestamp,upper_temp_c,lower_temp_c,dhw_heating,immersion_heating,"
        "dhw_energy_delta_kwh,valid FROM dhw_thermal_samples "
        "WHERE upper_temp_c IS NOT NULL AND lower_temp_c IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    draw_rows = db.execute("SELECT timestamp FROM dhw_draw_events ORDER BY timestamp").fetchall()
    draw_times = []
    for row in draw_rows:
        try:
            draw_times.append(datetime.fromisoformat(str(row[0])))
        except ValueError:
            continue

    observations: list[tuple[float, float]] = []
    previous = None
    draw_idx = 0
    for row in rows:
        try:
            ts = datetime.fromisoformat(str(row[0]))
            upper = float(row[1])
            lower = float(row[2])
            heating = bool(row[3])
            immersion = bool(row[4])
            energy = float(row[5]) if row[5] is not None else None
            valid = bool(row[6])
        except (TypeError, ValueError):
            previous = None
            continue
        if not valid or not (math.isfinite(upper) and math.isfinite(lower)):
            previous = None
            continue
        if previous is None:
            previous = (ts, upper, lower)
            continue
        prev_ts, prev_upper, prev_lower = previous
        previous = (ts, upper, lower)
        if not heating or immersion or energy is None or not math.isfinite(energy):
            continue
        duration_min = (ts - prev_ts).total_seconds() / 60.0
        if duration_min <= 0.0 or duration_min > 20.0 or energy <= 0.0 or energy > 1.5:
            continue

        while draw_idx < len(draw_times) and draw_times[draw_idx] <= prev_ts:
            draw_idx += 1
        if draw_idx < len(draw_times) and prev_ts < draw_times[draw_idx] <= ts:
            continue

        upper_rise = upper - prev_upper
        lower_rise = lower - prev_lower
        effective_rise = (upper_rise + lower_rise) / 2.0
        if effective_rise < MIN_INTERVAL_RISE_C or effective_rise > MAX_INTERVAL_RISE_C:
            continue
        if upper_rise < -0.25 or lower_rise < -0.25:
            continue
        effective_temp = (prev_upper + prev_lower) / 2.0
        if not 15.0 <= effective_temp <= 70.0:
            continue
        observations.append((effective_temp, energy / effective_rise))

    if len(observations) < MIN_TOTAL_SAMPLES:
        return None

    grouped: dict[float, list[float]] = {}
    for temp, cost in observations:
        lower_edge = math.floor(temp / BIN_WIDTH_C) * BIN_WIDTH_C
        center = lower_edge + BIN_WIDTH_C / 2.0
        grouped.setdefault(center, []).append(cost)

    populated = [(center, values) for center, values in sorted(grouped.items()) if len(values) >= MIN_BIN_SAMPLES]
    if len(populated) < MIN_POPULATED_BINS:
        return None

    centers = [center for center, _ in populated]
    raw_costs = [_median(values) for _, values in populated]
    counts = [len(values) for _, values in populated]
    monotonic_costs = _isotonic_non_decreasing(raw_costs, counts)
    weighted_mean = sum(value * count for value, count in zip(monotonic_costs, counts)) / sum(counts)
    if not math.isfinite(weighted_mean) or weighted_mean <= 0.0:
        return None
    multipliers = [value / weighted_mean for value in monotonic_costs]
    bins = tuple(
        EfficiencyBin(center, raw, multiplier, count)
        for center, raw, multiplier, count in zip(centers, raw_costs, multipliers, counts)
    )

    squared_errors = []
    for temp, cost in observations:
        nearest = min(range(len(centers)), key=lambda idx: abs(centers[idx] - temp))
        predicted_cost = multipliers[nearest] * weighted_mean
        squared_errors.append(((cost - predicted_cost) / weighted_mean) ** 2)
    rmse = math.sqrt(sum(squared_errors) / len(squared_errors)) if squared_errors else 0.0
    return TemperatureEfficiencyFit(
        bins=bins,
        sample_count=len(observations),
        min_temp_c=min(temp for temp, _ in observations),
        max_temp_c=max(temp for temp, _ in observations),
        rmse_multiplier=rmse,
    )


def predict_cycle_with_temperature_curve(cycle_fit, efficiency_fit: TemperatureEfficiencyFit,
                                         upper_temp_c: float, lower_temp_c: float,
                                         target_temp_c: float) -> float:
    upper_lift = efficiency_fit.integrated_multiplier(upper_temp_c, target_temp_c)
    lower_lift = efficiency_fit.integrated_multiplier(lower_temp_c, target_temp_c)
    return max(
        0.0,
        cycle_fit.intercept_kwh
        + cycle_fit.upper_kwh_per_c * upper_lift
        + cycle_fit.lower_kwh_per_c * lower_lift,
    )


def evaluate_shadow_cycle_model(db: sqlite3.Connection, cycle_fit,
                                efficiency_fit: TemperatureEfficiencyFit) -> tuple[int, float, float]:
    rows = db.execute(
        "SELECT start_upper_c,start_lower_c,target_temp_c,electrical_kwh "
        "FROM dhw_heating_cycles WHERE valid=1 AND cycle_type='normal_dhw' "
        "AND target_temp_c IS NOT NULL AND electrical_kwh IS NOT NULL "
        "ORDER BY start_ts DESC LIMIT 60"
    ).fetchall()
    baseline_errors: list[float] = []
    curve_errors: list[float] = []
    for row in rows:
        try:
            upper, lower, target, actual = map(float, row)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (upper, lower, target, actual)) or actual <= 0.0:
            continue
        baseline = cycle_fit.predict(upper, lower, target)
        curve = predict_cycle_with_temperature_curve(cycle_fit, efficiency_fit, upper, lower, target)
        baseline_errors.append(abs(baseline - actual))
        curve_errors.append(abs(curve - actual))
    if not baseline_errors:
        return (0, 0.0, 0.0)
    return (
        len(baseline_errors),
        sum(baseline_errors) / len(baseline_errors),
        sum(curve_errors) / len(curve_errors),
    )


def persist_temperature_efficiency_fit(db: sqlite3.Connection, fit: TemperatureEfficiencyFit,
                                       *, baseline_mae_kwh: float | None = None,
                                       curve_mae_kwh: float | None = None,
                                       validation_cycles: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[str, float, int, float | None]] = [
        ("dhw_efficiency_min_temp_c", fit.min_temp_c, fit.sample_count, fit.rmse_multiplier),
        ("dhw_efficiency_max_temp_c", fit.max_temp_c, fit.sample_count, fit.rmse_multiplier),
        ("dhw_efficiency_rmse_multiplier", fit.rmse_multiplier, fit.sample_count, fit.rmse_multiplier),
    ]
    for item in fit.bins:
        suffix = str(int(round(item.center_c * 10)))
        rows.append((f"dhw_efficiency_multiplier_{suffix}", item.multiplier, item.sample_count, fit.rmse_multiplier))
    if baseline_mae_kwh is not None:
        rows.append(("dhw_efficiency_shadow_baseline_mae_kwh", baseline_mae_kwh, validation_cycles, None))
    if curve_mae_kwh is not None:
        rows.append(("dhw_efficiency_shadow_curve_mae_kwh", curve_mae_kwh, validation_cycles, None))
    with db:
        for name, value, sample_count, error in rows:
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                "sample_count=excluded.sample_count,updated_at=excluded.updated_at,error=excluded.error",
                (name, value, sample_count, now, error),
            )
