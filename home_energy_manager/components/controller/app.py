# IMPORTANT PROJECT INSTRUCTIONS
# Before modifying controller behaviour, read AGENTS.md.
# AGENTS.md contains authoritative hard invariants and change-control rules.
# Optimisation logic must never override those rules without explicit user approval.
"""Controller composition root.

The established controller implementation is retained as a compatibility core
while responsibilities are extracted incrementally. New infrastructure, input
adapters and bounded policy calculations are composed here so each extraction
can remain behaviour-preserving.
"""

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

# Re-export the established core surface for runtime policy layers and tests.
from controller_legacy_core import *  # noqa: F401,F403

# The merged add-on injects the package version at runtime. Keep the legacy
# module's globals aligned because inherited methods resolve globals there.
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
        return _export_generated_topup_need(
            self, state, export_info, arrival_soc, cap, reserve, discharge_rate_w, window
        )


# main() was defined in the compatibility module and therefore resolves its
# Controller global there. Rebind it so process construction uses this subclass.
_legacy.Controller = Controller
