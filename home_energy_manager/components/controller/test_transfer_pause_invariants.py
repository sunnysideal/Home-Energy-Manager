from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import controller_plan_applier as applier


TZ = ZoneInfo('Europe/London')
ROOT = Path(__file__).resolve().parent


class Controller:
    def __init__(self, now):
        self._now = now
        self.tz = TZ

    def now(self):
        return self._now

    @staticmethod
    def tstr(value):
        return value.strftime('%H:%M:%S')


class Log:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(message % args)

    def info(self, message, *args):
        self.messages.append(message % args)


def plan(charge_start='2026-09-14T00:58:00+01:00', charge_end='2026-09-14T06:00:00+01:00', charge_kwh=12.96,
         discharge_start='2026-09-14T20:00:00+01:00', discharge_end='2026-09-14T20:00:00+01:00', discharge_kwh=0.0):
    return {
        'offpeak': {'start': '2026-09-13T23:00:00+01:00', 'end': '2026-09-14T06:00:00+01:00', 'rate_p': 6.99},
        'charge': {'start': charge_start, 'end': charge_end, 'rate_w': 3455, 'target_soc': 100,
                   'planned_kwh': charge_kwh, 'kind': 'calibration_forecast_recharge'},
        'discharge': {'start': discharge_start, 'end': discharge_end, 'rate_w': 6000, 'target_soc': 4,
                      'planned_kwh': discharge_kwh, 'kind': 'none' if discharge_kwh <= 0 else 'calibration_to_reserve'},
    }


def test_future_calibration_charge_truncates_pause_both_before_charge():
    controller = Controller(datetime(2026, 9, 13, 23, 30, tzinfo=TZ)); log = Log(); p = plan()
    pause = {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '06:00:00'}
    resolved = applier.resolve_pause_transfer_conflicts(controller, p, pause, log)
    assert resolved == {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '00:58:00'}


def test_active_calibration_charge_disables_pause_both():
    controller = Controller(datetime(2026, 9, 14, 1, 0, tzinfo=TZ)); log = Log(); p = plan()
    pause = {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '06:00:00'}
    resolved = applier.resolve_pause_transfer_conflicts(controller, p, pause, log)
    assert resolved['mode'] == 'Disabled'
    assert resolved['start'] == resolved['end'] == '00:00:00'
    assert any('PauseBoth -> Charge' in item for item in log.messages)


def test_active_charge_disables_pause_charge_too():
    controller = Controller(datetime(2026, 9, 14, 1, 0, tzinfo=TZ)); log = Log(); p = plan()
    resolved = applier.resolve_pause_transfer_conflicts(controller, p, {'mode': 'PauseCharge', 'start': '23:00:00', 'end': '06:00:00'}, log)
    assert resolved['mode'] == 'Disabled'


def test_zero_duration_charge_does_not_release_pause():
    controller = Controller(datetime(2026, 9, 14, 1, 0, tzinfo=TZ)); log = Log()
    p = plan(charge_start='2026-09-14T06:00:00+01:00', charge_end='2026-09-14T06:00:00+01:00', charge_kwh=0.0)
    pause = {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '06:00:00'}
    assert applier.resolve_pause_transfer_conflicts(controller, p, pause, log) == pause


def test_active_forced_discharge_disables_pause_both():
    controller = Controller(datetime(2026, 9, 13, 23, 20, tzinfo=TZ)); log = Log()
    p = plan(charge_start='2026-09-14T01:00:00+01:00', charge_end='2026-09-14T06:00:00+01:00',
             discharge_start='2026-09-13T23:10:00+01:00', discharge_end='2026-09-13T23:30:00+01:00', discharge_kwh=1.0)
    resolved = applier.resolve_pause_transfer_conflicts(controller, p, {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '01:00:00'}, log)
    assert resolved['mode'] == 'Disabled'
    assert any('PauseBoth -> Discharge' in item for item in log.messages)


def test_active_forced_discharge_disables_pause_discharge():
    controller = Controller(datetime(2026, 9, 13, 23, 20, tzinfo=TZ)); log = Log()
    p = plan(discharge_start='2026-09-13T23:10:00+01:00', discharge_end='2026-09-13T23:30:00+01:00', discharge_kwh=1.0)
    resolved = applier.resolve_pause_transfer_conflicts(controller, p, {'mode': 'PauseDischarge', 'start': '23:00:00', 'end': '23:30:00'}, log)
    assert resolved['mode'] == 'Disabled'


def test_pause_both_is_not_changed_outside_transfer():
    controller = Controller(datetime(2026, 9, 13, 23, 30, tzinfo=TZ)); log = Log()
    p = plan(charge_start='2026-09-14T02:00:00+01:00', charge_end='2026-09-14T06:00:00+01:00')
    pause = {'mode': 'PauseBoth', 'start': '23:00:00', 'end': '01:00:00'}
    assert applier.resolve_pause_transfer_conflicts(controller, p, pause, log) == pause


def test_expired_calibration_charge_is_disabled_not_replayed():
    controller = Controller(datetime(2026, 9, 14, 7, 5, tzinfo=TZ)); log = Log(); p = plan()
    applier.repair_expired_charge(controller, p, log)
    assert p['charge']['start'] == p['charge']['end']
    assert p['charge']['planned_kwh'] == 0.0
    assert p['charge']['kind'] == 'calibration_recharge_missed'
    assert any('Missed controller charge window' in item for item in log.messages)


def test_charge_is_not_marked_missed_while_window_is_active():
    controller = Controller(datetime(2026, 9, 14, 5, 59, tzinfo=TZ)); log = Log(); p = plan()
    applier.repair_expired_charge(controller, p, log)
    assert p['charge']['kind'] == 'calibration_forecast_recharge'
    assert p['charge']['planned_kwh'] > 0


def test_hard_rule_documents_transfer_pause_invariant():
    rules = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    assert 'Active battery transfer overrides a blocking pause' in rules
    assert 'expired controller-owned charge slot' in rules
