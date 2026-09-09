"""Validate shadow DHW forecasts against observed tank temperatures and DHW energy."""
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
    thermal_ready: bool
    promotion_ready: bool
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


def _nearest_sample(
    db: sqlite3.Connection,
    target: datetime,
    *,
    tolerance_minutes: float,
    require_energy: bool = False,
):
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
    return min(
        rows,
        key=lambda row: abs(
            (datetime.fromisoformat(str(row[0])).astimezone(timezone.utc) - target).total_seconds()
        ),
    )


def apply_actuals(db: sqlite3.Connection, *, tolerance_minutes: float = 4.0) -> int:
    """Fill actual temperatures for due validation rows from the nearest real sample."""
    now = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT forecast_ts,target_ts FROM dhw_forecast_validation "
        "WHERE actual_upper_c IS NULL AND target_ts<=? ORDER BY target_ts",
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
                "UPDATE dhw_forecast_validation SET actual_upper_c=?,actual_lower_c=? "
                "WHERE forecast_ts=? AND target_ts=?",
                (float(nearest[1]), float(nearest[2]), forecast_ts, target_ts_raw),
            )
            updated += 1
    return updated


def apply_actual_energy(db: sqlite3.Connection, *, tolerance_minutes: float = 4.0) -> int:
    """Back-fill observed electrical kWh for each 30-minute validation interval."""
    now = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT forecast_ts,target_ts FROM dhw_forecast_validation "
        "WHERE actual_dhw_kwh IS NULL AND target_ts<=? ORDER BY target_ts",
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
            # Reject meter reset/discontinuity or physically implausible half-hour use.
            if delta < -1e-6 or delta > 10.0:
                continue
            db.execute(
                "UPDATE dhw_forecast_validation SET actual_dhw_kwh=? "
                "WHERE forecast_ts=? AND target_ts=?",
                (max(0.0, delta), forecast_ts, target_ts_raw),
            )
            updated += 1
    return updated


def horizon_metrics(db: sqlite3.Connection, *, lookback_days: int = 7) -> list[HorizonMetric]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT forecast_ts,target_ts,predicted_upper_c,predicted_lower_c,actual_upper_c,actual_lower_c "
        "FROM dhw_forecast_validation WHERE forecast_ts>=? AND actual_upper_c IS NOT NULL "
        "AND actual_lower_c IS NOT NULL",
        (cutoff,),
    ).fetchall()
    buckets: dict[str, list[tuple[float, float]]] = {name: [] for name, _, _ in HORIZON_BUCKETS}
    for row in rows:
        try:
            forecast_ts = datetime.fromisoformat(str(row[0]))
            target_ts = datetime.fromisoformat(str(row[1]))
            horizon_h = (target_ts - forecast_ts).total_seconds() / 3600.0
            upper_error = abs(float(row[2]) - float(row[4]))
            lower_error = abs(float(row[3]) - float(row[5]))
        except (TypeError, ValueError):
            continue
        for name, low, high in HORIZON_BUCKETS:
            if low <= horizon_h < high:
                buckets[name].append((upper_error, lower_error))
                break
    out = []
    for name, _, _ in HORIZON_BUCKETS:
        values = buckets[name]
        if not values:
            out.append(HorizonMetric(name, 0, None, None))
        else:
            out.append(HorizonMetric(
                name,
                len(values),
                sum(v[0] for v in values) / len(values),
                sum(v[1] for v in values) / len(values),
            ))
    return out


def energy_comparison(
    db: sqlite3.Connection,
    *,
    lookback_days: int = 14,
    max_horizon_hours: float = 6.0,
) -> EnergyComparison:
    """Compare thermal and legacy 30-minute DHW kWh against the same actual intervals."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT forecast_ts,target_ts,predicted_dhw_kwh,legacy_dhw_kwh,actual_dhw_kwh "
        "FROM dhw_forecast_validation WHERE forecast_ts>=? AND predicted_dhw_kwh IS NOT NULL "
        "AND legacy_dhw_kwh IS NOT NULL AND actual_dhw_kwh IS NOT NULL",
        (cutoff,),
    ).fetchall()
    thermal_errors: list[float] = []
    legacy_errors: list[float] = []
    days: set[str] = set()
    for forecast_raw, target_raw, thermal_raw, legacy_raw, actual_raw in rows:
        try:
            forecast = datetime.fromisoformat(str(forecast_raw)).astimezone(timezone.utc)
            target = datetime.fromisoformat(str(target_raw)).astimezone(timezone.utc)
            horizon_h = (target - forecast).total_seconds() / 3600.0
            if not 0.0 <= horizon_h <= max_horizon_hours:
                continue
            thermal = max(0.0, float(thermal_raw))
            legacy = max(0.0, float(legacy_raw))
            actual = max(0.0, float(actual_raw))
        except (TypeError, ValueError):
            continue
        thermal_errors.append(abs(thermal - actual))
        legacy_errors.append(abs(legacy - actual))
        days.add(target.date().isoformat())
    if not thermal_errors:
        return EnergyComparison(0, 0, None, None)
    return EnergyComparison(
        count=len(thermal_errors),
        days=len(days),
        thermal_mae_kwh=sum(thermal_errors) / len(thermal_errors),
        legacy_mae_kwh=sum(legacy_errors) / len(legacy_errors),
    )


def _parameter_sample_count(db: sqlite3.Connection, names: list[str]) -> int:
    if not names:
        return 0
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(
        f"SELECT sample_count FROM dhw_model_parameters WHERE name IN ({placeholders})",
        names,
    ).fetchall()
    return min((int(row[0]) for row in rows), default=0)


def confidence_result(db: sqlite3.Connection) -> ConfidenceResult:
    passive_samples = _parameter_sample_count(
        db,
        ["dhw_upper_loss_w_per_k", "dhw_lower_loss_w_per_k", "dhw_coupling_w_per_k"],
    )
    cycle_count = _parameter_sample_count(
        db,
        ["dhw_cycle_intercept_kwh", "dhw_cycle_upper_kwh_per_c", "dhw_cycle_lower_kwh_per_c"],
    )
    row = db.execute("SELECT MAX(sample_days) FROM dhw_demand_profile").fetchone()
    demand_days = int(row[0]) if row and row[0] is not None else 0
    row = db.execute("SELECT COUNT(*) FROM dhw_draw_events").fetchone()
    draw_count = int(row[0]) if row else 0

    metrics = horizon_metrics(db)
    near = next(metric for metric in metrics if metric.name == "0_6h")
    validation_count = near.count
    upper_mae = near.upper_mae_c
    lower_mae = near.lower_mae_c
    energy = energy_comparison(db)

    row = db.execute(
        "SELECT timestamp,valid FROM dhw_thermal_samples ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    sensor_score = 0.0
    if row:
        try:
            age_m = (datetime.now(timezone.utc) - datetime.fromisoformat(str(row[0])).astimezone(timezone.utc)).total_seconds() / 60.0
            sensor_score = 1.0 if bool(row[1]) and age_m <= 15.0 else 0.0
        except ValueError:
            sensor_score = 0.0

    passive_score = min(passive_samples / 24.0, 1.0)
    cycle_score = min(cycle_count / 5.0, 1.0)
    demand_score = min(demand_days / 7.0, 1.0)
    draw_score = min(draw_count / 10.0, 1.0)
    validation_score = 0.0
    if validation_count >= 20 and upper_mae is not None and lower_mae is not None:
        validation_score = max(0.0, 1.0 - upper_mae / 4.0) * 0.5 + max(0.0, 1.0 - lower_mae / 8.0) * 0.5

    confidence = (
        0.10 * sensor_score
        + 0.20 * passive_score
        + 0.25 * cycle_score
        + 0.15 * demand_score
        + 0.10 * draw_score
        + 0.20 * validation_score
    )
    thermal_ready = bool(
        sensor_score == 1.0
        and passive_samples >= 24
        and cycle_count >= 5
        and demand_days >= 7
        and draw_count >= 10
        and validation_count >= 20
        and upper_mae is not None and upper_mae <= 2.0
        and lower_mae is not None and lower_mae <= 4.0
    )
    # A readiness flag only: production remains on the legacy forecast until a separate
    # promotion commit explicitly switches the source. Require a full week of energy
    # comparison and the thermal forecast to be no worse than legacy on the same intervals.
    promotion_ready = bool(
        thermal_ready
        and energy.count >= 20
        and energy.days >= 7
        and energy.thermal_not_worse
    )
    return ConfidenceResult(
        confidence=min(max(confidence, 0.0), 1.0),
        thermal_ready=thermal_ready,
        promotion_ready=promotion_ready,
        passive_samples=passive_samples,
        cycle_count=cycle_count,
        demand_days=demand_days,
        draw_count=draw_count,
        validation_count=validation_count,
        upper_mae_c=upper_mae,
        lower_mae_c=lower_mae,
        energy_validation_count=energy.count,
        energy_validation_days=energy.days,
        thermal_energy_mae_kwh=energy.thermal_mae_kwh,
        legacy_energy_mae_kwh=energy.legacy_mae_kwh,
    )


def persist_confidence(db: sqlite3.Connection, result: ConfidenceResult) -> None:
    now = datetime.now(timezone.utc).isoformat()
    values = [
        ("dhw_model_confidence", result.confidence, result.validation_count, result.upper_mae_c),
        ("dhw_thermal_ready", 1.0 if result.thermal_ready else 0.0, result.validation_count, result.lower_mae_c),
        ("dhw_promotion_ready", 1.0 if result.promotion_ready else 0.0, result.energy_validation_count, result.thermal_energy_mae_kwh),
    ]
    if result.thermal_energy_mae_kwh is not None:
        values.append(("dhw_thermal_energy_mae_kwh", result.thermal_energy_mae_kwh, result.energy_validation_count, None))
    if result.legacy_energy_mae_kwh is not None:
        values.append(("dhw_legacy_energy_mae_kwh", result.legacy_energy_mae_kwh, result.energy_validation_count, None))
    with db:
        for name, value, count, error in values:
            db.execute(
                "INSERT INTO dhw_model_parameters(name,value,sample_count,updated_at,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                "sample_count=excluded.sample_count,updated_at=excluded.updated_at,error=excluded.error",
                (name, value, count, now, error),
            )
