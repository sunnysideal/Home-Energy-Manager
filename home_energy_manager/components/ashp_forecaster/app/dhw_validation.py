"""Validate shadow DHW forecasts and learned standing-loss/heating models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3


HORIZON_BUCKETS = [
    ("0_6h", 0.0, 6.0),
    ("6_12h", 6.0, 12.0),
    ("12_24h", 12.0, 24.0),
    ("24_36h", 24.0, 36.0),
    ("36_48h", 36.0, 48.1),
]
PASSIVE_READY_RMSE_C = 0.35
HEATING_READY_RMSE_KWH = 0.60
PASSIVE_BAD_RMSE_C = 0.75
HEATING_BAD_RMSE_KWH = 1.00


@dataclass(frozen=True)
class HorizonMetric:
    name: str
    count: int
    upper_mae_c: float | None
    lower_mae_c: float | None


@dataclass(frozen=True)
class EnergyComparison:
    count: int
    days: int
    thermal_mae_kwh: float | None
    legacy_mae_kwh: float | None

    @property
    def thermal_not_worse(self) -> bool:
        return bool(
            self.thermal_mae_kwh is not None
            and self.legacy_mae_kwh is not None
            and self.thermal_mae_kwh <= self.legacy_mae_kwh
        )


@dataclass(frozen=True)
class ConfidenceResult:
    confidence: float
    trial_ready: bool
    thermal_ready: bool
    promotion_ready: bool
    performance_bad: bool
    passive_samples: int
    cycle_count: int
    demand_days: int
    draw_count: int
    validation_count: int
    upper_mae_c: float | None
    lower_mae_c: float | None
    energy_validation_count: int = 0
    energy_validation_days: int = 0
    thermal_energy_mae_kwh: float | None = None
    legacy_energy_mae_kwh: float | None = None
    passive_rmse_c: float | None = None
    cycle_rmse_kwh: float | None = None
    passive_ready: bool = False
    heating_ready: bool = False


def _nearest_sample(db: sqlite3.Connection, target: datetime, *, tolerance_minutes: float, require_energy: bool = False):
    tolerance = timedelta(minutes=tolerance_minutes)
    fields = "timestamp,upper_temp_c,lower_temp_c,dhw_energy_total_kwh"
    energy_clause = " AND dhw_energy_total_kwh IS NOT NULL" if require_energy else ""
    rows = db.execute(
        f"SELECT {fields} FROM dhw_thermal_samples WHERE valid=1 "
        "AND timestamp>=? AND timestamp<=?" + energy_clause,
        ((target - tolerance).isoformat(), (target + tolerance).isoformat()),
    ).fetchall()
    if not rows:
        return None
    return min(rows, key=lambda row: abs((datetime.fromisoformat(str(row[0])).astimezone(timezone.utc) - target).total_seconds()))


def apply_actuals(db: sqlite3.Connection, *, tolerance_minutes: float = 4.0) -> int:
    now = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT forecast_ts,target_ts FROM dhw_forecast_validation WHERE actual_upper_c IS NULL AND target_ts<=? ORDER BY target_ts",
        (now.isoformat(),),
    ).fetchall()
    updated = 0
    with db:
        for forecast_ts, target_ts_raw in rows:
            try:
                target = datetime.fromisoformat(str(target_ts_raw)).astimezone(timezone.utc)
            except ValueError:
                continue
            nearest = _nearest_sample(db, target, tolerance_minutes=tolerance_minutes)
            if nearest is None or nearest[1] is None or nearest[2] is None:
                continue
            db.execute(
                "UPDATE dhw_forecast_validation SET actual_upper_c=?,actual_lower_c=? WHERE forecast_ts=? AND target_ts=?",
                (float(nearest[1]), float(nearest[2]), forecast_ts, target_ts_raw),
            )
            updated += 1
    return updated


def apply_actual_energy(db: sqlite3.Connection, *, tolerance_minutes: float = 4.0) -> int:
    now = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT forecast_ts,target_ts FROM dhw_forecast_validation WHERE actual_dhw_kwh IS NULL AND target_ts<=? ORDER BY target_ts",
        (now.isoformat(),),
    ).fetchall()
    updated = 0
    with db:
        for forecast_ts, target_ts_raw in rows:
            try:
                end = datetime.fromisoformat(str(target_ts_raw)).astimezone(timezone.utc)
            except ValueError:
                continue
            start = end - timedelta(minutes=30)
            before = _nearest_sample(db, start, tolerance_minutes=tolerance_minutes, require_energy=True)
            after = _nearest_sample(db, end, tolerance_minutes=tolerance_minutes, require_energy=True)
            if before is None or after is None or before[3] is None or after[3] is None:
                continue
            delta = float(after[3]) - float(before[3])
            if delta < -1e-6 or delta > 10.0:
                continue
            db.execute(
                "UPDATE dhw_forecast_validation SET actual_dhw_kwh=? WHERE forecast_ts=? AND target_ts=?",
                (max(0.0, delta), forecast_ts, target_ts_raw),
            )
            updated += 1
    return updated


def horizon_metrics(db: sqlite3.Connection, *, lookback_days: int = 7) -> list[HorizonMetric]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT forecast_ts,target_ts,predicted_upper_c,predicted_lower_c,actual_upper_c,actual_lower_c FROM dhw_forecast_validation "
        "WHERE forecast_ts>=? AND actual_upper_c IS NOT NULL AND actual_lower_c IS NOT NULL",
        (cutoff,),
    ).fetchall()
    buckets: dict[str, list[tuple[float, float]]] = {name: [] for name, _, _ in HORIZON_BUCKETS}
    for row in rows:
        try:
            forecast_ts = datetime.fromisoformat(str(row[0])); target_ts = datetime.fromisoformat(str(row[1]))
            horizon_h = (target_ts - forecast_ts).total_seconds() / 3600.0
            upper_error = abs(float(row[2]) - float(row[4])); lower_error = abs(float(row[3]) - float(row[5]))
        except (TypeError, ValueError):
            continue
        for name, low, high in HORIZON_BUCKETS:
            if low <= horizon_h < high:
                buckets[name].append((upper_error, lower_error)); break
    out = []
    for name, _, _ in HORIZON_BUCKETS:
        values = buckets[name]
        out.append(HorizonMetric(name, len(values),
            sum(v[0] for v in values) / len(values) if values else None,
            sum(v[1] for v in values) / len(values) if values else None))
    return out


def energy_comparison(db: sqlite3.Connection, *, lookback_days: int = 14, max_horizon_hours: float = 6.0) -> EnergyComparison:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT forecast_ts,target_ts,predicted_dhw_kwh,legacy_dhw_kwh,actual_dhw_kwh FROM dhw_forecast_validation "
        "WHERE forecast_ts>=? AND predicted_dhw_kwh IS NOT NULL AND legacy_dhw_kwh IS NOT NULL AND actual_dhw_kwh IS NOT NULL",
        (cutoff,),
    ).fetchall()
    thermal_errors: list[float] = []; legacy_errors: list[float] = []; days: set[str] = set()
    for forecast_raw, target_raw, thermal_raw, legacy_raw, actual_raw in rows:
        try:
            forecast = datetime.fromisoformat(str(forecast_raw)).astimezone(timezone.utc)
            target = datetime.fromisoformat(str(target_raw)).astimezone(timezone.utc)
            horizon_h = (target - forecast).total_seconds() / 3600.0
            if not 0.0 <= horizon_h <= max_horizon_hours:
                continue
            thermal = max(0.0, float(thermal_raw)); legacy = max(0.0, float(legacy_raw)); actual = max(0.0, float(actual_raw))
        except (TypeError, ValueError):
            continue
        thermal_errors.append(abs(thermal - actual)); legacy_errors.append(abs(legacy - actual)); days.add(target.date().isoformat())
    if not thermal_errors:
        return EnergyComparison(0, 0, None, None)
    return EnergyComparison(len(thermal_errors), len(days), sum(thermal_errors)/len(thermal_errors), sum(legacy_errors)/len(legacy_errors))


def _parameter_sample_count(db: sqlite3.Connection, names: list[str]) -> int:
    if not names:
        return 0
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(f"SELECT sample_count FROM dhw_model_parameters WHERE name IN ({placeholders})", names).fetchall()
    return min((int(row[0]) for row in rows), default=0)


def _parameter_fit_error(db: sqlite3.Connection, names: list[str]) -> float | None:
    if not names:
        return None
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(f"SELECT error FROM dhw_model_parameters WHERE name IN ({placeholders})", names).fetchall()
    errors = [float(row[0]) for row in rows if row[0] is not None]
    return max(errors) if errors else None


def _combined_temperature_metric(metrics: list[HorizonMetric], names: tuple[str, ...]) -> tuple[int, float | None, float | None]:
    selected = [metric for metric in metrics if metric.name in names and metric.count > 0]
    count = sum(metric.count for metric in selected)
    if count <= 0:
        return 0, None, None
    upper = sum(float(metric.upper_mae_c or 0.0) * metric.count for metric in selected) / count
    lower = sum(float(metric.lower_mae_c or 0.0) * metric.count for metric in selected) / count
    return count, upper, lower


def confidence_result(db: sqlite3.Connection) -> ConfidenceResult:
    passive_names = ["dhw_upper_loss_w_per_k", "dhw_lower_loss_w_per_k", "dhw_coupling_w_per_k"]
    cycle_names = ["dhw_cycle_intercept_kwh", "dhw_cycle_upper_kwh_per_c", "dhw_cycle_lower_kwh_per_c"]
    passive_samples = _parameter_sample_count(db, passive_names)
    cycle_count = _parameter_sample_count(db, cycle_names)
    passive_rmse = _parameter_fit_error(db, passive_names)
    cycle_rmse = _parameter_fit_error(db, cycle_names)
    row = db.execute("SELECT MAX(sample_days) FROM dhw_demand_profile").fetchone()
    demand_days = int(row[0]) if row and row[0] is not None else 0
    row = db.execute("SELECT COUNT(*) FROM dhw_draw_events").fetchone(); draw_count = int(row[0]) if row else 0

    metrics = horizon_metrics(db)
    validation_count, upper_mae, lower_mae = _combined_temperature_metric(metrics, ("0_6h", "6_12h"))
    energy = energy_comparison(db)

    row = db.execute("SELECT timestamp,valid FROM dhw_thermal_samples ORDER BY timestamp DESC LIMIT 1").fetchone()
    sensor_score = 0.0
    if row:
        try:
            age_m = (datetime.now(timezone.utc) - datetime.fromisoformat(str(row[0])).astimezone(timezone.utc)).total_seconds() / 60.0
            sensor_score = 1.0 if bool(row[1]) and age_m <= 15.0 else 0.0
        except ValueError:
            pass

    passive_ready = bool(passive_samples >= 24 and passive_rmse is not None and passive_rmse <= PASSIVE_READY_RMSE_C)
    heating_ready = bool(cycle_count >= 5 and cycle_rmse is not None and cycle_rmse <= HEATING_READY_RMSE_KWH)

    passive_sample_score = min(passive_samples / 24.0, 1.0)
    cycle_sample_score = min(cycle_count / 5.0, 1.0)
    passive_quality_score = max(0.0, 1.0 - (passive_rmse or PASSIVE_BAD_RMSE_C) / PASSIVE_BAD_RMSE_C) if passive_rmse is not None else 0.0
    cycle_quality_score = max(0.0, 1.0 - (cycle_rmse or HEATING_BAD_RMSE_KWH) / HEATING_BAD_RMSE_KWH) if cycle_rmse is not None else 0.0
    demand_score = min(demand_days / 7.0, 1.0)
    draw_score = min(draw_count / 10.0, 1.0)
    confidence = (
        0.10 * sensor_score
        + 0.20 * passive_sample_score
        + 0.15 * passive_quality_score
        + 0.20 * cycle_sample_score
        + 0.20 * cycle_quality_score
        + 0.10 * demand_score
        + 0.05 * draw_score
    )

    # Demand history remains necessary to place expected showers/draws in time, but the
    # number of detected draws is not a readiness gate: irregular dishwasher/washing-up
    # use is inherently unpredictable and must not veto an otherwise sound tank model.
    trial_ready = bool(sensor_score == 1.0 and passive_samples >= 24 and cycle_count >= 5 and demand_days >= 7)

    # Readiness is deliberately about the two predictable physical capabilities:
    # standing-loss behaviour and electrical energy needed to heat from measured starting
    # temperatures to target. Long-horizon tank-temperature errors remain diagnostic only.
    thermal_ready = bool(trial_ready and passive_ready and heating_ready)
    promotion_ready = thermal_ready

    # Demote only when one of those learned physical fits is clearly poor. Slot-level
    # forecast error versus legacy can be dominated by unforecastable draws and therefore
    # remains a diagnostic comparator rather than a production veto.
    performance_bad = bool(
        (passive_samples >= 24 and passive_rmse is not None and passive_rmse > PASSIVE_BAD_RMSE_C)
        or (cycle_count >= 5 and cycle_rmse is not None and cycle_rmse > HEATING_BAD_RMSE_KWH)
    )

    return ConfidenceResult(
        confidence=min(max(confidence, 0.0), 1.0), trial_ready=trial_ready,
        thermal_ready=thermal_ready, promotion_ready=promotion_ready, performance_bad=performance_bad,
        passive_samples=passive_samples, cycle_count=cycle_count, demand_days=demand_days,
        draw_count=draw_count, validation_count=validation_count, upper_mae_c=upper_mae,
        lower_mae_c=lower_mae, energy_validation_count=energy.count,
        energy_validation_days=energy.days, thermal_energy_mae_kwh=energy.thermal_mae_kwh,
        legacy_energy_mae_kwh=energy.legacy_mae_kwh, passive_rmse_c=passive_rmse,
        cycle_rmse_kwh=cycle_rmse, passive_ready=passive_ready, heating_ready=heating_ready,
    )


def persist_confidence(db: sqlite3.Connection, result: ConfidenceResult) -> None:
    now = datetime.now(timezone.utc).isoformat()
    values = [
        ("dhw_model_confidence", result.confidence, result.validation_count, result.upper_mae_c),
        ("dhw_trial_ready", 1.0 if result.trial_ready else 0.0, result.validation_count, None),
        ("dhw_thermal_ready", 1.0 if result.thermal_ready else 0.0, result.validation_count, result.lower_mae_c),
        ("dhw_promotion_ready", 1.0 if result.promotion_ready else 0.0, result.cycle_count, result.cycle_rmse_kwh),
        ("dhw_performance_bad", 1.0 if result.performance_bad else 0.0, result.cycle_count, result.cycle_rmse_kwh),
        ("dhw_passive_model_ready", 1.0 if result.passive_ready else 0.0, result.passive_samples, result.passive_rmse_c),
        ("dhw_heating_model_ready", 1.0 if result.heating_ready else 0.0, result.cycle_count, result.cycle_rmse_kwh),
        ("dhw_energy_validation_days", float(result.energy_validation_days), result.energy_validation_count, None),
    ]
    if result.passive_rmse_c is not None:
        values.append(("dhw_passive_rmse_c", result.passive_rmse_c, result.passive_samples, None))
    if result.cycle_rmse_kwh is not None:
        values.append(("dhw_cycle_rmse_kwh", result.cycle_rmse_kwh, result.cycle_count, None))
    if result.thermal_energy_mae_kwh is not None:
        values.append(("dhw_thermal_energy_mae_kwh", result.thermal_energy_mae_kwh, result.energy_validation_count, None))
    if result.legacy_energy_mae_kwh is not None:
        values.append(("dhw_legacy_energy_mae_kwh", result.legacy_energy_mae_kwh, result.energy_validation_count, None))
    with db:
        for name, value, count, error in values:
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value,sample_count=excluded.sample_count,updated_at=excluded.updated_at,error=excluded.error",
                (name, value, count, now, error),
            )
