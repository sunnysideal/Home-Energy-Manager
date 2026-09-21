"""Behaviour tests for ordinary cheap-rate Eco discharge and charge sizing."""
import asyncio
from datetime import datetime, timedelta, timezone

from controller_battery import choose_rate_and_start, projected_charge_start_soc, soc_at
from controller_minimise_offpeak import coordinate_minimise_offpeak

TZ = timezone.utc
START = datetime(2026, 9, 22, 23, tzinfo=TZ)
END = START + timedelta(hours=7)


class Log:
    def info(self, *_args):
        pass


class Controller:
    LOG = Log()
    tz = TZ

    def __init__(self, now, live_soc=None):
        self.clock = now
        self.current_soc = live_soc
        self.c = {'charge_safety_margin_minutes': 10,
                  'preferred_charge_c_rate': 0.5, 'max_charge_c_rate': 0.5}
        self._battery_model_source = 'forecaster'
        self._battery_model_attrs = {
            'bands': [
                {'soc_lo': lo, 'soc_hi': hi, 'effective_factor': 1.0}
                for lo, hi in ((0, 10), (10, 20), (20, 30), (30, 40),
                               (40, 50), (50, 60), (60, 70), (70, 80),
                               (80, 90), (90, 95), (95, 98), (98, 99),
                               (99, 100))
            ],
            'top_completion_allowance_minutes': 0.0,
        }

    def now(self):
        return self.clock

    def operation_mode(self):
        return 'minimise_export'

    def calibration_state(self):
        return 'disabled'

    def current_active_offpeak(self):
        return {'start': START, 'end': END, 'rate_p': 7} if START <= self.clock < END else None

    async def num(self, name, *_args):
        return {'battery_capacity_entity': 10.0,
                'inverter_max_charge_rate_entity': 5000.0,
                'battery_reserve_entity': 20.0}[name], True

    async def live_soc(self):
        return self.current_soc

    def intervals(self, state, attr):
        assert attr == 'forecast_no_slots'
        return state['intervals']

    def soc_at(self, state, when, attr='forecast_no_slots'):
        return soc_at(self, state, when, attr)

    def projected_charge_start_soc(self, state, when, adjust, reserve):
        return projected_charge_start_soc(self, state, when, adjust, reserve)

    def charge_minutes(self, soc, target, rate, capacity):
        return max(0, target-soc)*capacity/100/(rate/1000)*60

    def choose_rate_and_start(self, state, window, target, capacity, reserve, adjust, max_charge, earliest=None):
        return choose_rate_and_start(self, state, window, target, capacity, reserve, adjust, max_charge, earliest)

    def choose_rate(self, soc, target, capacity, window, max_charge):
        return int(max_charge)

    def tstr(self, when):
        return when.strftime('%H:%M:%S')


def forecast(start_soc=80, loss_per_hour=5, end=END):
    intervals = []
    t = START
    while t < end:
        stop = min(end, t + timedelta(minutes=30))
        # Forecast slots store SOC at slot start; represent the no-charge path.
        intervals.append((t, stop, 7, {'soc': max(20, start_soc-loss_per_hour*(t-START).total_seconds()/3600)}))
        t = stop
    return {'intervals': intervals}


def plan(target=80):
    return {'charge': {'target_soc': target, 'rate_w': 5000},
            'pause': {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '06:00:00'}}


def run(controller, state, initial=None):
    return asyncio.run(coordinate_minimise_offpeak(
        controller, state, initial or plan(), {'start': START, 'end': END, 'rate_p': 7}))


def test_household_load_is_included_in_charge_start_and_pause_is_disabled():
    result = run(Controller(START-timedelta(hours=1)), forecast())
    charge = result['charge']
    assert result['pause']['mode'] == 'Disabled'
    assert START <= datetime.fromisoformat(charge['start']) < END
    assert charge['planned_kwh'] > 0
    assert result['forecast']['projected_charge_start_soc'] < 80
    assert result['forecast']['joint_pause_charge_needs_charge'] is True


def test_live_soc_drop_during_cheap_window_brings_charge_forward():
    state = forecast()
    normal = run(Controller(START+timedelta(hours=2), 70), state)
    lower = run(Controller(START+timedelta(hours=2), 50), state)
    assert datetime.fromisoformat(lower['charge']['start']) < datetime.fromisoformat(normal['charge']['start'])
    assert lower['charge']['planned_kwh'] > normal['charge']['planned_kwh']
    assert lower['pause']['mode'] == 'Disabled'


def test_no_charge_if_predicted_soc_stays_above_target():
    result = run(Controller(START-timedelta(hours=1)), forecast(start_soc=95,loss_per_hour=0), plan(target=80))
    assert result['charge']['planned_kwh'] == 0
    assert result['pause']['mode'] == 'Disabled'


def test_incomplete_forecast_does_not_delay_overnight_charge():
    result = run(Controller(START-timedelta(hours=1)), forecast(end=START+timedelta(hours=3)))
    assert datetime.fromisoformat(result['charge']['start']) == START
    assert result['pause']['mode'] == 'Disabled'


def test_higher_priority_calibration_and_confirmed_ev_are_untouched():
    controller = Controller(START)
    controller.calibration_state = lambda: 'awaiting_deep_low'
    original = plan()
    assert run(controller, forecast(), original) is original
    controller.calibration_state = lambda: 'disabled'
    original = plan()
    original['intelligent_go'] = {'confirmed': True}
    assert run(controller, forecast(), original) is original
