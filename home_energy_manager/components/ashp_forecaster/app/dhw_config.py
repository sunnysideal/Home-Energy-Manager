"""Configuration model for the ASHP forecaster DHW thermal model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DHWThermalConfig:
    upper_temperature_entity: str
    lower_temperature_entity: str | None
    tank_volume_l: float = 250.0
    ambient_temperature_entity: str | None = None
    minimum_useful_temperature_c: float = 40.0

    @property
    def enabled(self) -> bool:
        return bool(self.upper_temperature_entity and self.lower_temperature_entity)

    @classmethod
    def from_options(
        cls,
        options: dict[str, Any],
        *,
        legacy_temperature_entity: str = "sensor.dhw_temperature",
    ) -> "DHWThermalConfig":
        """Build thermal config while preserving the legacy single-sensor setup.

        The existing dhw_tank_temperature_entity remains the upper-temperature
        fallback.  A missing/blank lower sensor keeps the thermal model disabled,
        so legacy forecasting remains authoritative until both sensors are supplied.
        """
        upper = str(
            options.get("dhw_upper_temperature_entity")
            or options.get("dhw_tank_temperature_entity")
            or legacy_temperature_entity
        ).strip()
        lower_raw = options.get("dhw_lower_temperature_entity")
        lower = str(lower_raw).strip() if lower_raw not in (None, "") else None
        ambient_raw = options.get("dhw_ambient_temperature_entity")
        ambient = str(ambient_raw).strip() if ambient_raw not in (None, "") else None
        volume = float(options.get("dhw_tank_volume_l", 250.0))
        minimum = float(options.get("dhw_minimum_useful_temperature_c", 40.0))
        if not 50.0 <= volume <= 1000.0:
            raise ValueError("dhw_tank_volume_l must be between 50 and 1000 litres")
        if not 20.0 <= minimum <= 60.0:
            raise ValueError("dhw_minimum_useful_temperature_c must be between 20 and 60 C")
        return cls(
            upper_temperature_entity=upper,
            lower_temperature_entity=lower,
            tank_volume_l=volume,
            ambient_temperature_entity=ambient,
            minimum_useful_temperature_c=minimum,
        )
