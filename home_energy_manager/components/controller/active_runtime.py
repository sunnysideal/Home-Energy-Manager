#!/usr/bin/env python3
"""Active-controller entrypoint enforcing user-owned charge target SOC.

The planner may calculate logical SOC objectives, but Home Energy Manager must
never write the inverter charge-target entity. Charging is controlled by slot
timing and rate; the user's inverter target remains untouched.
"""
import asyncio

import minimise_export_runtime as runtime

core = runtime.core
_original_ensure = core.Controller.ensure


async def _ensure_without_charge_target(self, field, entity, desired, window_end):
    if field == 'charge_target':
        core.LOG.info(
            'Charge target write suppressed: logical_target=%s entity=%s; '
            'charge quantity is controlled by duration/rate',
            desired, entity,
        )
        return True
    return await _original_ensure(self, field, entity, desired, window_end)


core.Controller.ensure = _ensure_without_charge_target

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
