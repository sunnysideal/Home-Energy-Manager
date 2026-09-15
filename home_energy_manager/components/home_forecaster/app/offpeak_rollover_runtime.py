#!/usr/bin/env python3
"""Tariff-window rollover guard for Home Forecaster issue #91."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import battery_model_forecast_runtime as runtime
import battery_model_seed

base=runtime.base
_original_select_controller_offpeak=base.select_controller_offpeak
_original_publish_battery_model=runtime.model.publish_shadow_model


def _shift_local_day(value:datetime,days:int,tz)->datetime:
    local=value.astimezone(tz); day=local.date()+timedelta(days=days)
    return datetime.combine(day,local.timetz().replace(tzinfo=None),tz)


def select_controller_offpeak(import_rates,now,tz):
    selected=_original_select_controller_offpeak(import_rates,now,tz)
    if selected is not None:return selected
    now_utc=now.astimezone(timezone.utc)
    blocks=sorted(base.find_overnight_blocks(import_rates,tz),key=lambda item:item[0].astimezone(timezone.utc))
    active=next((block for block in blocks if block[0].astimezone(timezone.utc)<=now_utc<block[1].astimezone(timezone.utc)),None)
    if active is not None:
        start,end,rate=active
        next_start=_shift_local_day(start,1,tz); next_end=_shift_local_day(end,1,tz)
        while next_start.astimezone(timezone.utc)<=now_utc:
            next_start=_shift_local_day(next_start,1,tz); next_end=_shift_local_day(next_end,1,tz)
        base.LOG.info("Regular overnight cheap block: following window inferred from active tariff block %s->%s as %s->%s",start.isoformat(),end.isoformat(),next_start.isoformat(),next_end.isoformat())
        return next_start,next_end,rate
    raise base.HAError("Regular overnight cheap block could not be identified from tariff data; fixed tariff hours are not assumed")


def publish_battery_model_with_seed(client,store,cfg,now):
    runtime.model._ensure_schema(store)
    battery_model_seed.seed_if_needed(client,store,runtime.model,now)
    return _original_publish_battery_model(client,store,cfg,now)


base.select_controller_offpeak=select_controller_offpeak
runtime.model.publish_shadow_model=publish_battery_model_with_seed

if __name__=="__main__":base.main()
