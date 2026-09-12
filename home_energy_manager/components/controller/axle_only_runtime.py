#!/usr/bin/env python3
"""Axle-only entrypoint with user-owned inverter charge target SOC."""
import asyncio

import axle_only as base

_original_controller_entities = base.AxleOnlyController.controller_entities
_original_write = base.AxleOnlyController.write


def _controller_entities(self, forecast):
    ents, ci = _original_controller_entities(self, forecast)
    self._charge_target_entity = str(ents.get('charge_slot_1_target') or '')
    return ents, ci


async def _write_without_charge_target(self, entity_id, value):
    if entity_id and entity_id == getattr(self, '_charge_target_entity', ''):
        base.LOG.info(
            'Charge target write suppressed: logical_target=%s entity=%s; '
            'charge quantity is controlled by duration/rate',
            value, entity_id,
        )
        return
    return await _original_write(self, entity_id, value)


base.AxleOnlyController.controller_entities = _controller_entities
base.AxleOnlyController.write = _write_without_charge_target

if __name__ == '__main__':
    asyncio.run(base.amain())
