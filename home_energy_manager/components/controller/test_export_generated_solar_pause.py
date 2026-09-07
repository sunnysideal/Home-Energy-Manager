from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import Controller

TZ=ZoneInfo('Europe/London')

def controller(now, threshold_w=100):
    c=Controller.__new__(Controller)
    c.tz=TZ
    c.c={'operation_mode':'export_generated','export_generated_solar_threshold_w':threshold_w}
    c.now=lambda: now
    return c

def forecast(start, pv_values, minutes=5):
    rows=[]
    for i,pv in enumerate(pv_values):
        rows.append({
            'start':(start+timedelta(minutes=i*minutes)).isoformat(),
            'import_rate_p':30.0,
            'pv_kwh':pv,
        })
    return {'attributes':{'forecast_no_slots':rows}}

def require(cond,msg):
    if not cond: raise AssertionError(msg)

# 100 W over a 5-minute slot is 0.00833 kWh.  Meaningful slots at 09:00 and
# 09:15 must create one continuous window even though 09:05/09:10 are cloudy.
now=datetime(2026,9,6,8,0,tzinfo=TZ)
c=controller(now)
s=forecast(datetime(2026,9,6,9,0,tzinfo=TZ),[0.010,0.0,0.001,0.012,0.002])
w=c.export_generated_solar_window(s)
require(w is not None,'Solar window should exist')
require(w['start']==datetime(2026,9,6,9,0,tzinfo=TZ),'Solar window start wrong')
require(w['end']==datetime(2026,9,6,9,20,tzinfo=TZ),'Solar window must span through last meaningful slot')

# Below-threshold forecast should not pause charge.
s2=forecast(datetime(2026,9,6,9,0,tzinfo=TZ),[0.001,0.002,0.003])
require(c.export_generated_solar_window(s2) is None,'Tiny PV should not create solar window')

# Tomorrow's PV must not create today's inverter pause window.
s3=forecast(datetime(2026,9,7,9,0,tzinfo=TZ),[0.02,0.03])
require(c.export_generated_solar_window(s3) is None,'Tomorrow PV must not create today pause window')

# pause_plan should translate the forecast window to inverter PauseCharge.
offpeak={'start':datetime(2026,9,6,23,30,tzinfo=TZ),'end':datetime(2026,9,7,5,30,tzinfo=TZ),'rate_p':7.0}
pause=c.pause_plan(offpeak,s)
require(pause=={'mode':'PauseCharge','start':'09:00:00','end':'09:20:00'},'PauseCharge plan mismatch')

print('All Export Generated solar-pause tests passed.')
