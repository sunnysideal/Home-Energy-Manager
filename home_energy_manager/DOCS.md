
## v0.1.15 — local-to-GitHub migration support

This release adds a safe transfer path for learned state when moving from a local
Home Assistant installation to the GitHub repository installation.

The app maps its private `addon_config` folder at `/config` and adds an optional
`migration_action` setting:

- `none` — normal operation (default).
- `export` — before starting the engines, create
  `/config/home_energy_manager_migration_v1.zip` from the persistent learned-state files.
  SQLite databases are copied with SQLite's backup API and integrity-checked.
- `import` — before starting the engines, validate and restore that bundle into a fresh
  `/data` directory. Checksums and `PRAGMA integrity_check` are verified first. The import
  refuses to overwrite existing learned-state files and writes a one-time import marker.

For migration: upgrade the local app to 0.1.15, set `migration_action: export`, copy the
bundle from `/addon_configs/local_home_energy_manager/` into the GitHub-installed app's
`/addon_configs/<repository-id>_home_energy_manager/` folder, stop the local app, set the
GitHub app to `migration_action: import`, and start it. After a successful import, set the
action back to `none`. Supervisor options themselves are deliberately not included in the
bundle; copy those through the Home Assistant app configuration UI.


## 0.1.5 compatibility fixes

- Existing installations no longer fail configuration validation when `export_generated_solar_threshold_w` is absent; the controller retains its 100 W code default.
- The controller auto-discovers the Home Energy Forecast sensor when an MQTT registry migration has given it a different entity ID.
- MQTT discovery now supplies `default_entity_id` so fresh installs request canonical short entity IDs such as `sensor.home_energy_forecast`.
- The launcher now reports the package version consistently.

# Home Energy Manager v0.1.4

Single Home Assistant add-on package containing three deliberately separate components:

- **ASHP Energy Forecaster v0.2.0**
- **Home Energy Forecaster v0.2.10**
- **Home Energy Controller v0.1.27**

The merge is packaging/orchestration only. The three engines remain separate processes,
separate source directories, separate configuration namespaces, and separate databases.
They continue to communicate through the same Home Assistant entities/API as the current
standalone add-ons.

## Important before first start

**Stop and disable the three standalone add-ons before starting Home Energy Manager.**

In particular, do not run the standalone Home Energy Controller at the same time as this
package: both would be capable of writing the inverter.

The new add-on has its own `/data` directory, so existing SQLite learning/cache files are
not automatically copied from the old add-ons. The forecasters can rebuild history from
Home Assistant Recorder data. Controller charge-curve learning starts fresh unless its
database is migrated manually.

## Source layout

```text
home_energy_manager/
  AGENTS.md
  launcher.py
  components/
    ashp_forecaster/
    home_forecaster/
    controller/
  config.yaml
  Dockerfile
  run.sh
  test_architecture.py
```

`launcher.py` contains no energy logic. It only splits the single add-on configuration into
private component option files and supervises the three child processes.

## Configuration

The Home Assistant add-on options page now has three top-level namespaces:

- `ashp_forecaster`
- `home_forecaster`
- `controller`

All existing component options remain inside their corresponding namespace.

## Existing Home Assistant entity interfaces

The package intentionally preserves the existing entity interfaces for this first merged
release, including:

- `sensor.ashp_forecast_next_48h`
- `sensor.home_energy_forecast`
- `sensor.home_energy_controller`

This keeps the merge low risk. A native Home Assistant integration can be considered
separately later.

## Persistent files

The components retain distinct files:

- `/data/ashp_forecast.db`
- `/data/home_energy_forecaster.db`
- `/data/last_forecast.json`
- `/data/controller.db`

## Development rule

Read `AGENTS.md` before changing package architecture, and read
`components/controller/AGENTS.md` before changing controller behaviour.


## v0.1.1 — true meter import/export + EV load declaration

Under `home_forecaster.meter`, optional true utility-meter cumulative entities can
be configured:

```yaml
meter:
  import_energy_total_kwh: ""
  export_energy_total_kwh: ""
```

A configured true-meter entity is preferred when live/numeric; otherwise that
direction falls back to the existing inverter/battery cumulative entity in
`home_forecaster.tariff`.

EV relationship to the battery house-load sensor is declared under:

```yaml
load:
  ev_included_in_battery_load: false
  ev_charging_entity: binary_sensor.octopus_slot_actually_charging
```

The default is `false`, matching a topology where the EV charger is outside the
battery house-load measurement. EV load is never forecast. If set to `true`,
EV-active/unknown intervals are excluded from baseline learning, recent-load
correction, and load forecast comparison rather than teaching the model EV demand.

When true utility-meter export is active, the controller uses it directly for
`export_generated`; the EV-adjusted house-export proxy is used only on fallback
battery/inverter export metering.


## v0.1.2 — controller refresh-handshake recovery

The merged-package startup race is fixed in two layers:

1. If a controller refresh request is already pending after the Home Energy
   Forecaster finishes startup/history rebuilding, its first published forecast
   is tagged `controller_refresh` rather than `startup`.
2. The controller refresh barrier has a failsafe timeout, configurable as
   `controller_refresh_timeout_seconds` (default 90 seconds). If an explicit
   refresh is missed or misclassified, the next fresh forecast clears the
   barrier and control resumes.

The normal post-write refresh remains display-only and is not fed back into
another control pass.


## v0.1.3 — Home Assistant MQTT Discovery devices

The existing output entity IDs are preserved, but when MQTT is available they
are now created through Home Assistant MQTT Discovery and grouped into three
devices:

- **Home Energy Manager – ASHP Forecaster**
  - `sensor.ashp_forecast_next_30m`
  - `sensor.ashp_forecast_remaining_today`
  - `sensor.ashp_forecast_next_24h`
  - `sensor.ashp_forecast_next_48h`
  - `sensor.ashp_forecast_ch_next_48h`
  - `sensor.ashp_forecast_dhw_next_48h`
  - `sensor.ashp_forecast_kwh_per_degree_day`
- **Home Energy Manager – Home Forecaster**
  - `sensor.home_energy_forecast`
  - `sensor.home_energy_forecast_health`
  - `sensor.home_energy_forecast_comparison`
- **Home Energy Manager – Controller**
  - `sensor.home_energy_controller`

MQTT is a Home Assistant presentation/state transport only. The ASHP forecaster,
home forecaster, and controller remain separate processes and MQTT is not their
business-logic bus.

### Broker setup

`mqtt.enabled` defaults to `true`. The add-on first asks Supervisor for the
configured MQTT service/broker. Explicit host/port/credentials can be supplied
under the top-level `mqtt` section if required.

If MQTT cannot be connected during component startup, that component logs a
warning and uses its existing Home Assistant REST state publishing path for that
run. Energy forecasting/control therefore does not depend on MQTT being healthy.

Discovery and state messages are retained. Each component publishes an
availability topic with an MQTT last-will of `offline`.

`mqtt.migrate_legacy_states` defaults to `true`; before first discovery of an
entity in a process run, the old raw REST-created state is removed so Home
Assistant can register the MQTT entity under the existing entity ID instead of
creating a suffixed duplicate.

The internal controller refresh request entity remains REST-published deliberately;
it is an internal handshake rather than a user-facing device entity.


## v0.1.4 — Export Generated direct-solar pause

The controller now uses the home forecaster's no-slots PV forecast to keep `PauseCharge` active across the continuous meaningful-solar window in `export_generated` mode. This favours direct PV export and avoids unnecessary battery round-trip losses. Instantaneous PV is not used for the decision; confirmed Intelligent cheap charging can still override the pause when charging is genuinely required.

## v0.1.7

- Normalise every inverter schedule/pause time to whole-minute resolution before writing GivTCP select entities.
- Fix Export Generated solar PauseCharge startup boundaries such as `11:30:27`, which Home Assistant/GivTCP rejected with HTTP 500.
- Controller v0.1.33.




## v0.1.9

- Explicitly configured smart-meter import/export cumulative energy entities are now authoritative.
- The forecaster no longer silently switches to inverter/battery cumulative totals after a transient state lookup failure.
- Forecast logs now identify the selected grid import/export source and entity.
- If a configured smart-meter entity is unavailable or non-numeric, the forecast fails visibly with the entity and source named, rather than changing accounting models.
- Export Generated logging now calls the raw value `raw_export_sensor` rather than the misleading `raw_meter`.
- Controller v0.1.35.



## v0.1.14: battery learning visibility

- Exposes `sensor.home_energy_manager_battery_charge_curve` with chart-friendly SOC-band curve data in the `curve` attribute.
- Exposes `sensor.home_energy_manager_battery_learning` with session, observation and learning status.
- Exposes configured charge, discharge and round-trip efficiency sensors from the Home Forecaster.
- The package does not yet learn a discharge curve or inverter idle loss; these are explicitly reported as unsupported in the learning-status attributes rather than publishing invented measurements.

## Licence

Home Energy Manager is released under the MIT License. See the repository root `LICENSE` and
`THIRD_PARTY_NOTICES.md` files for details.
