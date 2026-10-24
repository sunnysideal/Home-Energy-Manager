"""Clock change: tariff projection, smart discounts, and full local-day coverage."""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

ROOT=Path(__file__).parent
TZ=ZoneInfo("Europe/London")
UTC=timezone.utc


def calendar():
    path=ROOT/"components/home_forecaster/app/tariff_calendar.py"
    spec=importlib.util.spec_from_file_location("dst_tariff_calendar",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(hour=hour,minute=minute).astimezone(UTC)


def day_rates(day, cheap_start=23, cheap_end=6, extra_smart=False, gap=False):
    lib=calendar()
    output=[]
    for a,b,local in lib._day_slots(day,TZ):
        hour=local.hour
        regular=(hour<cheap_end or hour>=cheap_start)
        price=6.66 if regular else 29.634
        if extra_smart and (hour==9 or hour==10):price=6.66
        if gap and hour==13 and local.minute==0:continue
        output.append({"start":a,"end":b,"rate_p":price})
    return output


@pytest.mark.parametrize("day",[
    date(2026,10,22),date(2026,10,23),date(2026,10,24),
    date(2026,10,25),date(2026,10,26),
    date(2027,3,26),date(2027,3,27),date(2027,3,28),
    date(2027,3,29),date(2027,3,30)])
def test_local_calendar_days_complete_even_across_dst(day):
    projected,reason=calendar().project_next_local_day(day_rates(day),day,TZ)
    assert projected,reason
    expected_day=day+timedelta(days=1)
    assert len(projected)==len(list(calendar()._day_slots(expected_day,TZ)))
    for row in projected:
        clock=row["start"].astimezone(TZ)
        assert row["end"]-row["start"]==timedelta(minutes=30)
        assert clock.date()==expected_day
        assert row["rate_p"]==(6.66 if clock.hour<6 or clock.hour>=23 else 29.634)


@pytest.mark.parametrize("source,target,count",[
    (date(2026,10,24),date(2026,10,25),50),
    (date(2027,3,27),date(2027,3,28),46),
])
def test_transitions_contain_exact_utc_halfhours(source,target,count):
    rows,reason=calendar().project_next_local_day(day_rates(source),source,TZ)
    assert reason=="projected_local_calendar"
    assert len(rows)==count
    assert rows[0]["start"]==datetime.combine(target,datetime.min.time(),tzinfo=TZ).astimezone(UTC)
    assert rows[-1]["end"]==datetime.combine(target+timedelta(days=1),datetime.min.time(),tzinfo=TZ).astimezone(UTC)
    assert all(rows[i]["end"]==rows[i+1]["start"] for i in range(len(rows)-1))
    assert calendar().covers_window(rows,stamp(source,23) if False else
           datetime.combine(source,datetime.min.time(),tzinfo=TZ).replace(hour=23).astimezone(UTC),
           datetime.combine(target,datetime.min.time(),tzinfo=TZ).replace(hour=6).astimezone(UTC),6.66)


def test_smart_daytime_discount_is_not_copied_into_future():
    day=date(2026,10,24)
    rows,reason=calendar().project_next_local_day(day_rates(day,extra_smart=True),day,TZ)
    assert rows,reason
    assert all(r["rate_p"]==29.634 for r in rows
               if r["start"].astimezone(TZ).hour in (9,10))


@pytest.mark.parametrize("source", [date(2026,10,24),date(2027,3,27)])
def test_unverified_or_gapped_tariff_rejected(source):
    rows,reason=calendar().project_next_local_day(day_rates(source,gap=True),source,TZ)
    assert rows==[]
    assert reason=="incomplete_or_conflicting_source_tariff"


def test_competing_prices_in_same_settlement_slot_rejected():
    day=date(2026,10,24)
    rows=day_rates(day)
    conflict=dict(rows[4],rate_p=22.0)
    result,reason=calendar().project_next_local_day(rows+[conflict],day,TZ)
    assert result==[]
    assert reason=="incomplete_or_conflicting_source_tariff"


def test_spring_day_source_can_be_projected_to_complete_normal_day():
    source=date(2027,3,28)
    rows,reason=calendar().project_next_local_day(day_rates(source),source,TZ)
    assert rows and len(rows)==48
    assert reason=="projected_with_spring_hour_interpolation"


def test_autumn_repeated_hour_has_two_distinct_utc_instants():
    day=date(2026,10,24)
    rows,_=calendar().project_next_local_day(day_rates(day),day,TZ)
    duplicate=[r for r in rows if r["start"].astimezone(TZ).hour==1]
    assert len(duplicate)==4
    assert len({r["start"] for r in duplicate})==4


def test_release_image_packages_calendar_helper():
    docker=(ROOT/"Dockerfile").read_text()
    assert "COPY components/home_forecaster/app/tariff_calendar.py /app/runtime/home_forecaster/tariff_calendar.py" in docker
