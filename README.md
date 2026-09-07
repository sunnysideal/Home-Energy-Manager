# Home Energy Manager

Home Assistant app providing ASHP forecasting, whole-home energy forecasting and battery control
in one package.

The project is licensed under the [MIT License](LICENSE).

## Install in Home Assistant

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open **Repositories** and add the URL of this GitHub repository.
3. Refresh the store and install **Home Energy Manager**.
4. Configure the entity IDs for your own Home Assistant installation before starting the app.

The repository is source-built by Home Assistant. The app currently supports `aarch64` and `amd64`.

## Important

Do not run a standalone Home Energy Controller at the same time as this package. Both could write
to the inverter.

The default configuration intentionally contains no installation-specific GivEnergy serial number,
Octopus MPAN/meter identifiers or credentials. These must be supplied through the app configuration.

## Source layout

The installable app is in [`home_energy_manager/`](home_energy_manager/).
The three engines remain separate source components and are supervised by `launcher.py`.

Historical standalone component packaging files are retained for development under names such as
`standalone-config.yaml`; only the top-level `home_energy_manager/config.yaml` is an installable Home
Assistant app configuration.

## Third-party software

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Direct runtime dependencies are installed at
container build time rather than vendored into this repository.

## Migrating an existing local installation

Version 0.1.16 includes a one-time learned-state migration path. First upgrade the local
Home Energy Manager installation to the local 0.1.16 migration release and set
`migration_action` to `export`. Copy `home_energy_manager_migration_v1.zip` from the
local app config folder into this repository-installed app's config folder, stop the local
app, then set this installation to `migration_action: import` for its first start. After
the import succeeds, return the setting to `none`.

The migration bundle transfers the three learned SQLite databases and the last forecast;
Home Assistant Supervisor options are copied separately in the app configuration UI.
