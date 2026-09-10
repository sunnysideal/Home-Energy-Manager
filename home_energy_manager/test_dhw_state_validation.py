import sqlite3
from datetime import datetime, timedelta, timezone

from components.ashp_forecaster.app.dhw_model import ensure_dhw_model_schema
from components.ashp_forecaster.app.dhw_state_validation import state_metrics


def _validation(db, forecast, target, pu, pl, au, al):
    db.execute(
        "INSERT INTO dhw_forecast_validation(forecast_ts,target_ts,predicted_upper_c,predicted_lower_c,actual_upper_c,actual_lower_c,model_source) VALUES(?,?,?,?,?,?,?)",
        (forecast.isoformat(), target.isoformat(), pu, pl, au, al, "thermal"),
    )


def test_state_metrics_split_passive_draw_heating_and_post_heating():
    db = sqlite3.connect(":memory:")
    ensure_dhw_model_schema(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    forecast = now - timedelta(hours=1)
    targets = [forecast + timedelta(minutes=15*i) for i in range(1, 5)]

    # Passive target.
    db.execute("INSERT INTO dhw_thermal_samples(timestamp,upper_temp_c,lower_temp_c,dhw_heating,valid) VALUES(?,?,?,?,1)", (targets[0].isoformat(), 50, 30, 0))
    _validation(db, forecast, targets[0], 51, 32, 50, 30)

    # Draw/post-draw target.
    db.execute("INSERT INTO dhw_draw_events(timestamp,estimated_thermal_kwh,confidence) VALUES(?,?,?)", ((targets[1]-timedelta(minutes=10)).isoformat(), 0.5, 1.0))
    db.execute("INSERT INTO dhw_thermal_samples(timestamp,upper_temp_c,lower_temp_c,dhw_heating,valid) VALUES(?,?,?,?,1)", (targets[1].isoformat(), 48, 25, 0))
    _validation(db, forecast, targets[1], 50, 29, 48, 25)

    # Heating takes precedence over a recent draw.
    db.execute("INSERT INTO dhw_thermal_samples(timestamp,upper_temp_c,lower_temp_c,dhw_heating,valid) VALUES(?,?,?,?,1)", (targets[2].isoformat(), 52, 35, 1))
    _validation(db, forecast, targets[2], 55, 41, 52, 35)

    # Post-heating takes precedence over the still-recent draw.
    db.execute("INSERT INTO dhw_heating_cycles(start_ts,end_ts,cycle_type,valid) VALUES(?,?,?,1)", ((targets[2]-timedelta(minutes=15)).isoformat(), (targets[3]-timedelta(minutes=5)).isoformat(), "normal"))
    db.execute("INSERT INTO dhw_thermal_samples(timestamp,upper_temp_c,lower_temp_c,dhw_heating,valid) VALUES(?,?,?,?,1)", (targets[3].isoformat(), 54, 40, 0))
    _validation(db, forecast, targets[3], 58, 48, 54, 40)
    db.commit()

    metrics = {m.state: m for m in state_metrics(db)}
    assert metrics["passive_no_draw"].count == 1
    assert metrics["passive_no_draw"].upper_mae_c == 1.0
    assert metrics["passive_no_draw"].lower_mae_c == 2.0
    assert metrics["draw_or_post_draw"].count == 1
    assert metrics["draw_or_post_draw"].upper_mae_c == 2.0
    assert metrics["draw_or_post_draw"].lower_mae_c == 4.0
    assert metrics["heating"].count == 1
    assert metrics["heating"].upper_mae_c == 3.0
    assert metrics["heating"].lower_mae_c == 6.0
    assert metrics["post_heating"].count == 1
    assert metrics["post_heating"].upper_mae_c == 4.0
    assert metrics["post_heating"].lower_mae_c == 8.0
