#!/usr/bin/env python3
"""Compatibility import for the existing calibration recharge planner.

Calibration state, MQTT command handling and SoC observation are explicitly
owned by calibration_coordinator, installed by active_runtime. This module
remains an import link until the planning and Docker stages of issue #116.
"""
import asyncio

import manual_low_calibration_runtime as runtime

core = runtime.core

if __name__ == '__main__':
    try:
        asyncio.run(core.main())
    except KeyboardInterrupt:
        pass
