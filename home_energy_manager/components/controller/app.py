# IMPORTANT PROJECT INSTRUCTIONS
# Before modifying controller behaviour, read AGENTS.md.
# AGENTS.md contains authoritative hard invariants and change-control rules.
# Optimisation logic must never override those rules without explicit user approval.
"""Controller composition root.

The established controller implementation is retained as a compatibility core
while responsibilities are extracted incrementally. New infrastructure and
input adapters are composed here so the public controller entrypoint stays
small and each extraction can remain behaviour-preserving.
"""

import os

import controller_legacy_core as _legacy
from common.mqtt import MQTTPublisher
from controller_db import DB
from controller_ha import HA
from controller_utils import as_float, parse_dt, clamp, iso, weighted_quantile, recency_weight
from controller_discovery import discover_from_forecast as _discover_from_forecast

# Re-export the established core surface for runtime policy layers and tests.
from controller_legacy_core import *  # noqa: F401,F403

# The merged add-on injects the package version at runtime. Keep the legacy
# module's globals aligned because inherited methods resolve globals there.
_legacy.VERSION = os.environ.get('HOME_ENERGY_MANAGER_VERSION', _legacy.VERSION).strip() or _legacy.VERSION
VERSION = _legacy.VERSION


class Controller(_legacy.Controller):
    """Controller with forecast discovery delegated to controller_discovery."""

    def discover_from_forecast(self, state):
        return _discover_from_forecast(self, state)


# main() was defined in the compatibility module and therefore resolves its
# Controller global there. Rebind it so process construction uses this subclass.
_legacy.Controller = Controller
