"""Exercise the production battery planner across repeated and skipped hours."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from controller_battery import SOC_BANDS,choose_rate,latest_charge_start_for_rate

TZ=ZoneInfo("Europe/London")
UTC=timezone.utc

def model():
    return {"bands":[{"soc_lo":lo,"soc_hi":hi,"effective_factor":1.0}
                     for lo,hi in SOC_BANDS],
            "top_completion_allowance_minutes":0.0}

def stub():
    class C:
        tz=TZ
        c={"preferred_charge_c_rate":0.25,"max_charge_c_rate":0.4,"charge_safety_margin_minutes":10}
        _battery_model_source="forecaster"
        _battery_model_attrs=model()
        def intervals(self,s,attr="forecast_no_slots"):
            starts=[datetime.fromisoformat(row["start"]).astimezone(TZ) for row in s["attributes"][attr]]
            return [(st, (st.astimezone(UTC)+timedelta(minutes=30)).astimezone(TZ),None,row)
                    for st,row in zip(starts,s["attributes"][attr])]
    return C()

@pytest.mark.parametrize("day,expected_minutes",[
    ("2026-10-24",480),("2027-03-27",360),
])
def test_production_charge_rate_uses_actual_overnight_minutes(day,expected_minutes):
    start=datetime.fromisoformat(day+"T23:00:00").replace(tzinfo=TZ)
    finish=(datetime.fromisoformat(day)+timedelta(days=1)).replace(hour=6,tzinfo=TZ)
    window={"start":start,"end":finish}
    assert int((finish.astimezone(UTC)-start.astimezone(UTC)).total_seconds()/60)==expected_minutes
    rate=choose_rate(stub(),4,100,13.5,window,6000)
    assert rate>0

def test_latest_charge_start_uses_extra_autumn_hour():
    start=datetime(2026,10,24,23,tzinfo=TZ)
    end=datetime(2026,10,25,6,tzinfo=TZ)
    c=stub()
    rows=[{"start":(start.astimezone(UTC)+timedelta(minutes=i*30)).astimezone(TZ).isoformat(),
           "soc":50.0} for i in range(17)]
    forecast={"attributes":{"forecast_no_slots":rows}}
    result,_,ok=latest_charge_start_for_rate(c,forecast,75,3000,12,4,0,
                        {"start":start,"end":end})
    assert ok
    # 25% of 12kWh at 3kW needs 60 minutes, plus 10-minute margin.
    # Charge can start after the repeated hour, approaching the 06:00 deadline.
    assert result.astimezone(UTC)>datetime(2026,10,25,3,tzinfo=UTC)
    assert result.astimezone(UTC)<end.astimezone(UTC)
