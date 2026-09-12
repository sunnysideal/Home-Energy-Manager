# IMPORTANT PROJECT INSTRUCTIONS
# Before modifying controller behaviour, read AGENTS.md.
# AGENTS.md contains authoritative hard invariants and change-control rules.
# Optimisation logic must never override those rules without explicit user approval.
"""Controller composition root for staged behaviour-preserving extractions."""
from datetime import datetime, timedelta
import os
import controller_legacy_core as _legacy
from common.mqtt import MQTTPublisher
from controller_db import DB
from controller_ha import HA
from controller_utils import as_float, parse_dt, clamp, iso, weighted_quantile, recency_weight
from controller_discovery import discover_from_forecast as _discover_from_forecast
from controller_export_generated import (
    update_effective_export_accounting as _update_effective_export_accounting,
    effective_export_accounting as _effective_export_accounting,
    export_generated_inputs as _export_generated_inputs,
    forecast_export_kwh_between as _forecast_export_kwh_between,
    forced_incremental_grid_export_kwh as _forced_incremental_grid_export_kwh,
    export_generated_end as _export_generated_end,
    export_generated_guardrail_end as _export_generated_guardrail_end,
    export_generated_topup_need as _export_generated_topup_need,
)
from controller_power_down import (
    power_down_protected_soc as _power_down_protected_soc,
    power_down_info as _power_down_info,
)
from controller_battery import (
    soc_at as _soc_at, forecast_battery_kwh_between as _forecast_battery_kwh_between,
    planned_discharge_soc_adjustment as _planned_discharge_soc_adjustment,
    projected_charge_start_soc as _projected_charge_start_soc,
    latest_charge_start_for_rate as _latest_charge_start_for_rate,
    choose_rate_and_start as _choose_rate_and_start, band_factor as _band_factor,
    dwell as _dwell, charge_minutes as _charge_minutes, choose_rate as _choose_rate,
)
from controller_tariff import (
    offpeak_from_forecast as _offpeak_from_forecast, persist_offpeak as _persist_offpeak,
    fallback_offpeak as _fallback_offpeak, current_active_offpeak as _current_active_offpeak,
)
from controller_plan_applier import apply_plan as _apply_plan, apply_safe_fallback as _apply_safe_fallback
from controller_legacy_core import *  # noqa: F401,F403

_legacy.VERSION = os.environ.get('HOME_ENERGY_MANAGER_VERSION', _legacy.VERSION).strip() or _legacy.VERSION
VERSION = _legacy.VERSION

class Controller(_legacy.Controller):
    """Controller composed from the staged behaviour-preserving extractions."""
    LOG = _legacy.LOG
    def discover_from_forecast(self, state): return _discover_from_forecast(self, state)
    async def update_effective_export_accounting(self): return await _update_effective_export_accounting(self)
    def effective_export_accounting(self): return _effective_export_accounting(self)
    def export_generated_inputs(self, state): return _export_generated_inputs(self, state)
    def forecast_export_kwh_between(self, state, start, end, attr='forecast_no_slots'): return _forecast_export_kwh_between(self, state, start, end, attr)
    def forced_incremental_grid_export_kwh(self, state, start, end, rate_w): return _forced_incremental_grid_export_kwh(self, state, start, end, rate_w)
    def export_generated_end(self, state, start, latest, remaining_kwh, rate_w): return _export_generated_end(self, state, start, latest, remaining_kwh, rate_w)
    def export_generated_guardrail_end(self, state, start, latest, rate_w, arrival_soc, cap, reserve): return _export_generated_guardrail_end(self, state, start, latest, rate_w, arrival_soc, cap, reserve)
    def export_generated_topup_need(self, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window): return _export_generated_topup_need(self, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window)
    def power_down_protected_soc(self, state, session_end, next_offpeak_start, cap, reserve): return _power_down_protected_soc(self, state, session_end, next_offpeak_start, cap, reserve)
    async def power_down_info(self, state, window, cap, reserve, discharge_rate_w): return await _power_down_info(self, state, window, cap, reserve, discharge_rate_w)
    def soc_at(self,state,when,attr='forecast_no_slots'):return _soc_at(self,state,when,attr)
    def forecast_battery_kwh_between(self,state,start,end,attr='forecast_no_slots'):return _forecast_battery_kwh_between(self,state,start,end,attr)
    def planned_discharge_soc_adjustment(self,state,ds,de,rate_w,cap,reserve):return _planned_discharge_soc_adjustment(self,state,ds,de,rate_w,cap,reserve)
    def projected_charge_start_soc(self,state,when,adjust,reserve):return _projected_charge_start_soc(self,state,when,adjust,reserve)
    def latest_charge_start_for_rate(self,state,window,target,rate,cap,reserve,adjust,earliest=None):return _latest_charge_start_for_rate(self,state,window,target,rate,cap,reserve,adjust,earliest)
    def choose_rate_and_start(self,state,window,target,cap,reserve,adjust,hw,earliest=None):return _choose_rate_and_start(self,state,window,target,cap,reserve,adjust,hw,earliest)
    def band_factor(self,band):return _band_factor(self,band)
    def dwell(self):return _dwell(self)
    def charge_minutes(self,soc,target,rate,cap):return _charge_minutes(self,soc,target,rate,cap)
    def choose_rate(self,soc,target,cap,window,hw):return _choose_rate(self,soc,target,cap,window,hw)
    def offpeak_from_forecast(self,state):return _offpeak_from_forecast(self,state)
    def persist_offpeak(self,window):return _persist_offpeak(self,window)
    def fallback_offpeak(self):return _fallback_offpeak(self)
    def current_active_offpeak(self):return _current_active_offpeak(self)
    def pause_plan(self, window, state=None):
        plan = super().pause_plan(window, state)
        if self.operation_mode() == 'minimise_export' and plan.get('mode') == 'PauseDischarge':
            plan = dict(plan)
            plan['mode'] = 'PauseBoth'
        return plan
    def _active_or_next_offpeak(self, window):
        active = self.current_active_offpeak()
        now = self.now()
        if active and active['start'] <= now < active['end']:
            return active, True
        local_start = window['start'].astimezone(self.tz)
        local_end = window['end'].astimezone(self.tz)
        previous_day = local_start.date() - timedelta(days=1)
        previous = {
            'start': datetime.combine(previous_day, local_start.timetz().replace(tzinfo=None), self.tz),
            'end': datetime.combine(local_end.date() - timedelta(days=1), local_end.timetz().replace(tzinfo=None), self.tz),
            'rate_p': window['rate_p'],
        }
        if previous['start'] <= now < previous['end']:
            return previous, True
        return window, False
    async def _coordinate_minimise_offpeak(self, state, plan, window, fallback=False):
        """Plan regular cheap-rate preservation and charging as one sequence.

        PauseBoth means household load does not consume battery energy. Therefore
        a delayed charge must be sized from the SOC being preserved, not from the
        no-slots SOC later in the cheap window. The resulting regular sequence is
        PauseBoth -> Charge, with either phase allowed to have zero duration.
        """
        if fallback or not plan or self.operation_mode() != 'minimise_export':
            return plan
        if plan.get('intelligent_go', {}).get('confirmed'):
            return plan
        if self.calibration_state() in ('awaiting_deep_low', 'deep_recharge'):
            return plan

        control_window, active = self._active_or_next_offpeak(window)
        target = as_float((plan.get('charge') or {}).get('target_soc'))
        if target is None:
            return plan

        capacity, _ = await self.num('battery_capacity_entity', 'battery_capacity', True)
        max_charge, _ = await self.num('inverter_max_charge_rate_entity', 'max_charge_rate', False)
        reserve, _ = await self.num('battery_reserve_entity', 'battery_reserve', True)
        if capacity is None or capacity <= 0 or reserve is None:
            return plan

        preserved_soc = await self.live_soc() if active else None
        if preserved_soc is None:
            preserved_soc = as_float(state.get('attributes', {}).get('overnight_start_soc_no_slots'))
        if preserved_soc is None:
            preserved_soc = self.soc_at(state, control_window['start'], 'forecast_no_slots')
        if preserved_soc is None:
            return plan
        preserved_soc = clamp(float(preserved_soc), float(reserve), 100.0)
        target = clamp(float(target), float(reserve), 100.0)

        now = self.now()
        earliest = max(control_window['start'], now) if active else control_window['start']
        charge = dict(plan.get('charge') or {})
        safety_minutes = max(0.0, float(self.c.get('charge_safety_margin_minutes', 10)))
        needs_charge = target > preserved_soc + 0.5

        if needs_charge:
            rate = self.choose_rate(preserved_soc, target, capacity, control_window, max_charge)
            charge_minutes = self.charge_minutes(preserved_soc, target, rate, capacity) + safety_minutes
            charge_start = max(earliest, control_window['end'] - timedelta(minutes=charge_minutes))
            charge_end = control_window['end'].replace(second=0, microsecond=0)
            planned_kwh = capacity * max(0.0, target - preserved_soc) / 100.0
        else:
            rate = int(round(as_float(charge.get('rate_w')) or min(max_charge or capacity * 250.0, capacity * 250.0)))
            charge_start = control_window['start'].replace(second=0, microsecond=0)
            charge_end = charge_start
            planned_kwh = 0.0

        pause_start = control_window['start'].replace(second=0, microsecond=0)
        pause_end = charge_start.replace(second=0, microsecond=0)
        if pause_end > pause_start:
            pause = {'mode': 'PauseBoth', 'start': self.tstr(pause_start), 'end': self.tstr(pause_end)}
        else:
            pause = {'mode': 'Disabled', 'start': '00:00:00', 'end': '00:00:00'}

        charge.update({
            'start': iso(charge_start.replace(second=0, microsecond=0)),
            'end': iso(charge_end),
            'rate_w': int(round(rate)),
            'target_soc': int(round(target)),
            'planned_kwh': round(planned_kwh, 3),
        })
        plan['pause'] = pause
        plan['charge'] = charge
        forecast = dict(plan.get('forecast') or {})
        forecast['preserved_offpeak_start_soc'] = round(preserved_soc, 1)
        forecast['joint_pause_charge_plan'] = True
        forecast['joint_pause_charge_needs_charge'] = bool(needs_charge)
        plan['forecast'] = forecast
        self.LOG.info(
            'Minimise export joint offpeak plan: preserved_soc=%.1f%% target=%.1f%% pause=%s-%s charge=%s-%s rate=%dW planned=%.2fkWh',
            preserved_soc, target, pause['start'], pause['end'],
            charge_start.strftime('%H:%M'), charge_end.strftime('%H:%M'), int(round(rate)), planned_kwh,
        )
        return plan
    async def plan(self, state, soc, window, fallback=False):
        plan = await super().plan(state, soc, window, fallback)
        plan = await self._coordinate_minimise_offpeak(state, plan, window, fallback)
        if plan and plan.get('intelligent_go', {}).get('confirmed'):
            intelligent = plan['intelligent_go']
            if intelligent.get('pause_mode') == 'PauseDischarge':
                intelligent['pause_mode'] = 'PauseBoth'
        return plan
    async def apply(self,plan):return await _apply_plan(self,plan,_legacy.LOG)
    async def safe(self,window,cap=None,hw=None):return await _apply_safe_fallback(self,window,cap,hw)

_legacy.Controller = Controller
