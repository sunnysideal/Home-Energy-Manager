from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from statistics import median
from typing import Any


DEFAULT_WINDOW_DAYS = 30
DEFAULT_MAX_HORIZON_HOURS = 48.0
DEFAULT_MIN_OBSERVATIONAL_SAMPLES = 12
DEFAULT_MIN_OBSERVATIONAL_DAYS = 2
DEFAULT_MIN_RELIABLE_SAMPLES = 48
DEFAULT_MIN_RELIABLE_DAYS = 3
DEFAULT_MIN_MODEL_SAMPLES = 100
DEFAULT_MIN_MODEL_DAYS = 3
DEFAULT_MIN_MODEL_BANDS = 2
DEFAULT_MIN_VALIDATED_SPAN_C = 2.0
DEFAULT_HOLDOUT_FRACTION = 0.30
DEFAULT_MIN_MODEL_IMPROVEMENT_PCT = 5.0
DEFAULT_MAX_ABS_BIAS_C = 5.0

HORIZON_BUCKETS = (
    ("0_6h", 0.0, 6.0),
    ("6_12h", 6.0, 12.0),
    ("12_24h", 12.0, 24.0),
    ("24_36h", 24.0, 36.0),
    ("36_48h", 36.0, 48.000001),
)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("temperature bias timestamps must be timezone-aware")
    return value.isoformat()


def _score(errors: list[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in errors if math.isfinite(float(value))]
    if not clean:
        return {"samples": 0, "mean_bias_c": None, "mae_c": None, "rmse_c": None}
    count = len(clean)
    return {
        "samples": count,
        "mean_bias_c": sum(clean) / count,
        "mae_c": sum(abs(value) for value in clean) / count,
        "rmse_c": math.sqrt(sum(value * value for value in clean) / count),
    }


def _improvement_pct(raw_mae: float | None, corrected_mae: float | None) -> float | None:
    if raw_mae is None or corrected_mae is None or raw_mae <= 0:
        return None
    return (raw_mae - corrected_mae) / raw_mae * 100.0


def _clamp(value: float, limit: float) -> float:
    limit = abs(float(limit))
    return max(-limit, min(limit, float(value)))


def _horizon_name(hours: float) -> str:
    for name, lower, upper in HORIZON_BUCKETS:
        if hours >= lower and hours < upper:
            return name
    return HORIZON_BUCKETS[-1][0]


def _band_label(lower: float | None, upper: float | None) -> str:
    def fmt(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value).replace(".", "p")

    if lower is None:
        return f"lt_{fmt(float(upper))}c"
    if upper is None:
        return f"ge_{fmt(float(lower))}c"
    return f"{fmt(float(lower))}_{fmt(float(upper))}c"


def temperature_bands(winter_threshold_c: float) -> list[tuple[str, float | None, float | None]]:
    threshold = float(winter_threshold_c)
    boundaries: list[float] = [0.0]
    cursor = 2.0
    while cursor < threshold:
        boundaries.append(cursor)
        cursor += 2.0
    if threshold > 0.0:
        boundaries.append(threshold)
    for value in (13.0, 15.0):
        if value > threshold:
            boundaries.append(value)
    boundaries = sorted(set(boundaries))

    bands: list[tuple[str, float | None, float | None]] = []
    first = boundaries[0] if boundaries else threshold
    bands.append((_band_label(None, first), None, first))
    for lower, upper in zip(boundaries, boundaries[1:]):
        bands.append((_band_label(lower, upper), lower, upper))
    last = boundaries[-1] if boundaries else threshold
    bands.append((_band_label(last, None), last, None))
    return bands


def _in_band(value: float, lower: float | None, upper: float | None) -> bool:
    return (lower is None or value >= lower) and (upper is None or value < upper)


def _evidence_state(samples: int, days: int) -> str:
    if samples >= DEFAULT_MIN_RELIABLE_SAMPLES and days >= DEFAULT_MIN_RELIABLE_DAYS:
        return "reliable"
    if samples >= DEFAULT_MIN_OBSERVATIONAL_SAMPLES and days >= DEFAULT_MIN_OBSERVATIONAL_DAYS:
        return "observational"
    return "insufficient"


def _linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if variance <= 1e-9:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / variance
    intercept = mean_y - slope * mean_x
    return intercept, slope


def _horizon_corrections(rows: list[dict[str, Any]], max_abs_bias_c: float) -> tuple[float, dict[str, float]]:
    errors = [row["error"] for row in rows]
    global_correction = _clamp(float(median(errors)), max_abs_bias_c) if errors else 0.0
    corrections: dict[str, float] = {}
    for name, lower, upper in HORIZON_BUCKETS:
        bucket = [row["error"] for row in rows if row["horizon"] >= lower and row["horizon"] < upper]
        corrections[name] = _clamp(float(median(bucket)), max_abs_bias_c) if bucket else global_correction
    return global_correction, corrections


def _contiguous_reliable_heating_range(
    bands: list[tuple[str, float | None, float | None]],
    summaries: dict[str, dict[str, Any]],
    winter_threshold_c: float,
) -> tuple[float, float, list[str]] | None:
    candidates: list[tuple[str, float, float]] = []
    threshold = float(winter_threshold_c)
    for name, lower, upper in bands:
        if summaries[name]["evidence"] != "reliable":
            continue
        bounded_lower = -50.0 if lower is None else float(lower)
        bounded_upper = 50.0 if upper is None else float(upper)
        bounded_upper = min(bounded_upper, threshold)
        if bounded_lower >= threshold or bounded_upper <= bounded_lower:
            continue
        candidates.append((name, bounded_lower, bounded_upper))
    if not candidates:
        return None

    runs: list[list[tuple[str, float, float]]] = []
    for candidate in candidates:
        if not runs or abs(runs[-1][-1][2] - candidate[1]) > 1e-9:
            runs.append([candidate])
        else:
            runs[-1].append(candidate)
    best = max(runs, key=lambda run: (run[-1][2] - run[0][1], sum(summaries[item[0]]["samples"] for item in run)))
    return best[0][1], best[-1][2], [item[0] for item in best]


def temperature_shadow_analysis(
    db: sqlite3.Connection,
    *,
    now: datetime,
    winter_threshold_c: float,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_horizon_hours: float = DEFAULT_MAX_HORIZON_HOURS,
    max_abs_bias_c: float = DEFAULT_MAX_ABS_BIAS_C,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    min_model_improvement_pct: float = DEFAULT_MIN_MODEL_IMPROVEMENT_PCT,
) -> dict[str, Any]:
    """Evaluate temperature-dependent weather bias in shadow only.

    Temperature bands mature independently. The candidate linear models are fitted
    only across the contiguous reliable heating-active temperature range and are
    scored on later calendar days than the training data. No returned correction is
    applied to the production weather forecast by this module.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    threshold = float(winter_threshold_c)
    cutoff = now - timedelta(days=max(1, int(window_days)))
    rows = db.execute(
        """
        SELECT horizon_hours, forecast_temperature_c, actual_temperature_c,
               actual_observed_at, error_c
        FROM weather_forecast_observations
        WHERE actual_temperature_c IS NOT NULL
          AND actual_observed_at IS NOT NULL
          AND error_c IS NOT NULL
          AND actual_observed_at >= ?
          AND actual_observed_at <= ?
          AND horizon_hours >= 0
          AND horizon_hours <= ?
        ORDER BY actual_observed_at, issued_at, target_ts
        """,
        (_iso(cutoff), _iso(now), float(max_horizon_hours)),
    ).fetchall()

    samples: list[dict[str, Any]] = []
    for horizon, forecast, actual, observed_at, error in rows:
        try:
            values = (float(horizon), float(forecast), float(actual), float(error))
            stamp = _parse_dt(str(observed_at))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in values):
            continue
        h, f, a, e = values
        samples.append({"horizon": h, "forecast": f, "actual": a, "error": e, "observed": stamp, "day": stamp.date().isoformat()})

    global_correction, horizon_corrections = _horizon_corrections(samples, max_abs_bias_c)

    def baseline_correction(row: dict[str, Any]) -> float:
        return horizon_corrections.get(_horizon_name(row["horizon"]), global_correction)

    bands = temperature_bands(threshold)
    band_results: dict[str, dict[str, Any]] = {}
    for name, lower, upper in bands:
        selected = [row for row in samples if _in_band(row["actual"], lower, upper)]
        errors = [row["error"] for row in selected]
        shadow_errors = [row["error"] - baseline_correction(row) for row in selected]
        raw = _score(errors)
        shadow = _score(shadow_errors)
        days = len({row["day"] for row in selected})
        band_results[name] = {
            "lower_c": lower,
            "upper_c": upper,
            "samples": len(selected),
            "distinct_days": days,
            "evidence": _evidence_state(len(selected), days),
            "median_bias_c": float(median(errors)) if errors else None,
            "raw_mae_c": raw["mae_c"],
            "raw_rmse_c": raw["rmse_c"],
            "shadow_mae_c": shadow["mae_c"],
            "shadow_rmse_c": shadow["rmse_c"],
            "improvement_pct": _improvement_pct(raw["mae_c"], shadow["mae_c"]),
        }

    heating_rows = [row for row in samples if row["actual"] < threshold]
    heating_raw = _score([row["error"] for row in heating_rows])
    heating_shadow = _score([row["error"] - baseline_correction(row) for row in heating_rows])
    heating_days = len({row["day"] for row in heating_rows})
    heating_active = {
        "threshold_c": threshold,
        "samples": len(heating_rows),
        "distinct_days": heating_days,
        "evidence": _evidence_state(len(heating_rows), heating_days),
        "median_bias_c": float(median([row["error"] for row in heating_rows])) if heating_rows else None,
        "raw_mae_c": heating_raw["mae_c"],
        "raw_rmse_c": heating_raw["rmse_c"],
        "shadow_mae_c": heating_shadow["mae_c"],
        "shadow_rmse_c": heating_shadow["rmse_c"],
        "improvement_pct": _improvement_pct(heating_raw["mae_c"], heating_shadow["mae_c"]),
    }

    reliable_range = _contiguous_reliable_heating_range(bands, band_results, threshold)
    candidate: dict[str, Any] = {
        "eligible": False,
        "selected_model": "global_horizon",
        "reason": "insufficient_reliable_temperature_range",
        "validated_min_c": None,
        "validated_max_c": None,
        "validated_bands": [],
        "training_days": 0,
        "holdout_days": 0,
        "training_samples": 0,
        "holdout_samples": 0,
        "temperature_linear": None,
        "temperature_plus_horizon": None,
        "baseline_holdout_mae_c": None,
        "best_candidate_holdout_mae_c": None,
        "best_candidate_improvement_pct": None,
        "required_improvement_pct": float(min_model_improvement_pct),
    }

    if reliable_range is not None:
        lower, upper, reliable_bands = reliable_range
        candidate["validated_min_c"] = lower
        candidate["validated_max_c"] = upper
        candidate["validated_bands"] = reliable_bands
        span = upper - lower
        model_rows = [row for row in heating_rows if row["actual"] >= lower and row["actual"] < upper]
        unique_days = sorted({row["day"] for row in model_rows})
        candidate["training_days"] = 0
        candidate["holdout_days"] = 0
        if (
            len(reliable_bands) >= DEFAULT_MIN_MODEL_BANDS
            and span >= DEFAULT_MIN_VALIDATED_SPAN_C
            and len(model_rows) >= DEFAULT_MIN_MODEL_SAMPLES
            and len(unique_days) >= DEFAULT_MIN_MODEL_DAYS
        ):
            holdout_days_count = max(1, int(math.ceil(len(unique_days) * float(holdout_fraction))))
            if holdout_days_count >= len(unique_days):
                holdout_days_count = len(unique_days) - 1
            train_days = set(unique_days[:-holdout_days_count])
            holdout_days = set(unique_days[-holdout_days_count:])
            train_rows = [row for row in model_rows if row["day"] in train_days]
            holdout_rows = [row for row in model_rows if row["day"] in holdout_days]
            candidate["training_days"] = len(train_days)
            candidate["holdout_days"] = len(holdout_days)
            candidate["training_samples"] = len(train_rows)
            candidate["holdout_samples"] = len(holdout_rows)

            if train_rows and holdout_rows:
                train_global, train_horizon = _horizon_corrections(train_rows, max_abs_bias_c)

                def train_baseline(row: dict[str, Any]) -> float:
                    return train_horizon.get(_horizon_name(row["horizon"]), train_global)

                baseline_errors = [row["error"] - train_baseline(row) for row in holdout_rows]
                baseline_score = _score(baseline_errors)
                candidate["baseline_holdout_mae_c"] = baseline_score["mae_c"]

                temp_fit = _linear_fit([(row["actual"], row["error"]) for row in train_rows])
                residual_fit = _linear_fit([(row["actual"], row["error"] - train_baseline(row)) for row in train_rows])

                model_results: list[tuple[str, dict[str, Any]]] = []
                if temp_fit is not None:
                    intercept, slope = temp_fit
                    corrected = [row["error"] - _clamp(intercept + slope * row["actual"], max_abs_bias_c) for row in holdout_rows]
                    score = _score(corrected)
                    result = {
                        "intercept_c": intercept,
                        "slope_c_per_c": slope,
                        "holdout_mae_c": score["mae_c"],
                        "holdout_rmse_c": score["rmse_c"],
                        "improvement_vs_global_horizon_pct": _improvement_pct(baseline_score["mae_c"], score["mae_c"]),
                    }
                    candidate["temperature_linear"] = result
                    model_results.append(("temperature_linear", result))

                if residual_fit is not None:
                    intercept, slope = residual_fit
                    corrected = [
                        row["error"] - train_baseline(row) - _clamp(intercept + slope * row["actual"], max_abs_bias_c)
                        for row in holdout_rows
                    ]
                    score = _score(corrected)
                    result = {
                        "residual_intercept_c": intercept,
                        "residual_slope_c_per_c": slope,
                        "holdout_mae_c": score["mae_c"],
                        "holdout_rmse_c": score["rmse_c"],
                        "improvement_vs_global_horizon_pct": _improvement_pct(baseline_score["mae_c"], score["mae_c"]),
                    }
                    candidate["temperature_plus_horizon"] = result
                    model_results.append(("temperature_plus_horizon", result))

                if model_results:
                    best_name, best = min(model_results, key=lambda item: float("inf") if item[1]["holdout_mae_c"] is None else item[1]["holdout_mae_c"])
                    improvement = best["improvement_vs_global_horizon_pct"]
                    candidate["best_candidate_holdout_mae_c"] = best["holdout_mae_c"]
                    candidate["best_candidate_improvement_pct"] = improvement
                    candidate["eligible"] = True
                    if improvement is not None and improvement >= float(min_model_improvement_pct):
                        candidate["selected_model"] = best_name
                        candidate["reason"] = "heldout_heating_active_improvement_met"
                    else:
                        candidate["selected_model"] = "global_horizon"
                        candidate["reason"] = "heldout_improvement_below_threshold"
                else:
                    candidate["reason"] = "temperature_variance_too_small"
            else:
                candidate["reason"] = "chronological_holdout_unavailable"
        else:
            candidate["reason"] = "reliable_range_not_mature_enough"

    return {
        "calibration_applied": False,
        "window_days": max(1, int(window_days)),
        "window_start": _iso(cutoff),
        "window_end": _iso(now),
        "winter_threshold_c": threshold,
        "samples": len(samples),
        "distinct_days": len({row["day"] for row in samples}),
        "bands": band_results,
        "heating_active": heating_active,
        "candidate_model": candidate,
        "evidence_thresholds": {
            "observational_samples": DEFAULT_MIN_OBSERVATIONAL_SAMPLES,
            "observational_days": DEFAULT_MIN_OBSERVATIONAL_DAYS,
            "reliable_samples": DEFAULT_MIN_RELIABLE_SAMPLES,
            "reliable_days": DEFAULT_MIN_RELIABLE_DAYS,
            "model_samples": DEFAULT_MIN_MODEL_SAMPLES,
            "model_days": DEFAULT_MIN_MODEL_DAYS,
            "model_bands": DEFAULT_MIN_MODEL_BANDS,
            "model_span_c": DEFAULT_MIN_VALIDATED_SPAN_C,
        },
    }
