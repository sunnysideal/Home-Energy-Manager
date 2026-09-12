# IMPORTANT PROJECT INSTRUCTIONS
# Before modifying controller behaviour, read AGENTS.md.
# AGENTS.md contains authoritative hard invariants and change-control rules.
# Optimisation logic must never override those rules without explicit user approval.
"""Controller composition root for staged behaviour-preserving extractions."""
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
    offpeak_from_forecast as _offpeak_from_forecast,
    persist_offpeak as _persist_offpeak,
    fallback_offpeak as _fallback_offpeak,
    current_active_offpeak as _current_active_offpeak,
)
from controller_legacy_core import *  # noqa: F401,F403

_legacy.VERSION = os.environ.get('HOME_ENERGY_MANAGER_VERSION', _legacy.VERSION).strip() or _legacy.VERSION
VERSION = _legacy.VERSION

class Controller(_legacy.Controller):
    """Controller composed from the staged behaviour-preserving extractions."""
    def discover_from_forecast(self, state):
        return _discover_from_forecast(self, state)
    async def update_effective_export_accounting(self):
        return await _update_effective_export_accounting(self)
    def effective_export_accounting(self):
        return _effective_export_accounting(self)
    def export_generated_inputs(self, state):
        return _export_generated_inputs(self, state)
    def forecast_export_kwh_between(self, state, start, end, attr='forecast_no_slots'):
        return _forecast_export_kwh_between(self, state, start, end, attr)
    def forced_incremental_grid_export_kwh(self, state, start, end, rate_w):
        return _forced_incremental_grid_export_kwh(self, state, start, end, rate_w)
    def export_generated_end(self, state, start, latest, remaining_kwh, rate_w):
        return _export_generated_end(self, state, start, latest, remaining_kwh, rate_w)
    def export_generated_guardrail_end(self, state, start, latest, rate_w, arrival_soc, cap, reserve):
        return _export_generated_guardrail_end(self, state, start, latest, rate_w, arrival_soc, cap, reserve)
    def export_generated_topup_need(self, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window):
        return _export_generated_topup_need(self, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window)
    def power_down_protected_soc(self, state, session_end, next_offpeak_start, cap, reserve):
        return _power_down_protected_soc(self, state, session_end, next_offpeak_start, cap, reserve)
    async def power_down_info(self, state, window, cap, reserve, discharge_rate_w):
        return await _power_down_info(self, state, window, cap, reserve, discharge_rate_w)
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

_legacy.Controller = Controller
