# Home Energy Controller

See the package documentation in `../../DOCS.md` for current controller behaviour and configuration.

## Calibration diagnostics

The controller publishes calibration history and emits structured INFO-level diagnostics on every controller publish/planning cycle. The calibration status includes the current state and reason, current observed SOC, configured full/deep intervals and floor, last full and deep timestamps, their ages, due/remaining/overdue values, any pending low endpoint, and the most recent below-40%, below-20%, and low-SOC observations.

State changes are logged explicitly, for example `normal -> top_due reason=full_charge_age`, including the timestamps, ages, and configured thresholds that caused the decision. Meaningful calibration observations are also logged when 100% is reached, 40%/20% are crossed downward, the configured low floor is reached, and a deep calibration completes.

The same calculated age/due fields are included in the `calibration` attributes of `sensor.home_energy_controller`, so the log and Home Assistant status use one diagnostic calculation.

Existing dedicated timestamp sensors remain available:

- `sensor.home_energy_manager_last_full_charge`
- `sensor.home_energy_manager_last_deep_cycle`
- `sensor.home_energy_manager_last_below_40_soc`
- `sensor.home_energy_manager_last_below_20_soc`
- `sensor.home_energy_manager_last_low_soc`

This diagnostic reporting does not change calibration scheduling or inverter-control behaviour.
