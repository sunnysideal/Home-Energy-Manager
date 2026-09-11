# Home Energy Manager — Authoritative Architecture Instructions

This file is authoritative for the merged add-on package.

The package deliberately contains four separate functional components:

1. `components/ashp_forecaster`
2. `components/home_forecaster`
3. `components/axle`
4. `components/controller`

## Hard architecture rules

- The four components MUST remain separate modules/processes.
- Do not merge their business logic into one Python module or one shared runtime object.
- Do not move forecasting logic into the controller.
- Do not move controller decision/write logic into either forecaster or the Axle adapter.
- Do not add direct Python imports between the four component directories.
- For now, inter-component communication remains through Home Assistant entities/API, preserving the same behavioural interfaces used by the standalone add-ons.
- Each component has its own configuration namespace. Components that learn persistent state own their own database/files; the Axle adapter is intentionally stateless.
- `launcher.py` is orchestration only: split config, start/stop processes, propagate shutdown, and fail the package if a component exits unexpectedly. It must contain no energy logic.
- Shared helpers may only be introduced for genuinely infrastructure-only concerns and must not blur ownership of forecasting or control behaviour.
- `common/mqtt.py` is HA presentation/transport infrastructure only. It MUST NOT contain forecasting, tariff, battery-planning, ASHP, Axle-event policy, or controller policy.
- MQTT MUST NOT be used as the internal business-logic bus between the four components. Existing component boundaries/interfaces remain authoritative.
- MQTT publishing may fall back to the existing REST state path for the entire process run if the broker is unavailable; this must not change energy logic.
- The controller's own `components/controller/AGENTS.md` remains authoritative for all controller hard rules and MUST be read before controller behaviour is changed.
- A change to one component should not alter another component's behaviour unless the user explicitly requests a cross-component interface change.
- Any future architectural change that weakens these separation rules requires explicit user approval first.

## Component ownership

### ASHP Forecaster
Owns:
- CH forecast
- DHW forecast
- ASHP historical learning
- summer/winter heating-state interpretation
- DHW priority behaviour

Does not own:
- whole-house battery simulation
- tariff planning
- inverter writes

### Home Energy Forecaster
Owns:
- selection of true utility-meter import/export entities with inverter/battery fallback
- exclusion of EV charging from learned/forecast household load
- household baseline forecast
- PV/load/tariff/battery simulation
- actual/forecast aggregation
- controller input/discovery payload
- forecast refresh handshake

Does not own:
- inverter control decisions/writes
- ASHP CH/DHW modelling

### Axle Adapter
Owns:
- reading the installed HACS Axle VPP integration entities from Home Assistant
- validating and normalising the next Axle event into a stable package-owned entity
- reporting whether that event is Import or Export, upcoming or active

Does not own:
- Axle authentication or direct Axle API access
- battery planning or inverter writes
- tariff/load/PV forecasting

### Controller
Owns:
- battery operating decisions
- inverter writes
- hard controller invariants
- charge-curve learning
- EV Smart Charging / Power Down / Axle policy

Does not own:
- reimplementation of load/PV/ASHP forecasting
- direct Axle API access or HACS integration internals

## GitHub / CI workflow

- Before committing code changes, inspect the repository's current GitHub Actions workflow(s) relevant to the changed component.
- Confirm test and implementation assumptions match the actual CI environment, including Python version, working directory, import paths, installed dependencies, and commands executed.
- Do not assume that focused tests passing means the full CI workflow will pass.
- Before pushing, reproduce the exact relevant CI commands where practical. At minimum, review the changed code against the exact commands and environment defined by the workflow.
- After pushing, inspect the GitHub Actions run for the actual current head commit or pull request; do not rely on a successful run from an earlier commit.
- Do not merge to `main` unless the full required CI workflow for the current head commit has completed successfully.
- If CI fails, inspect the failing step/output, fix the underlying cause, and confirm a new CI run for the corrected head commit is green before merging.

## Release checklist

Before merging or packaging:
- read this file;
- read `components/controller/AGENTS.md` for controller changes;
- inspect the relevant current GitHub Actions workflow and confirm the test environment/commands used by CI;
- compile all four Python components;
- parse the merged `config.yaml`;
- run controller invariant tests;
- run `test_architecture.py`;
- verify no cross-component Python imports were introduced;
- verify launcher contains no energy/control calculations;
- confirm the full required GitHub CI workflow for the current head commit has completed successfully before merging to `main`.
