# Home Energy Manager

Home Energy Manager is a Home Assistant app for forecasting household energy use and controlling a compatible home battery around tariffs, solar generation, heat-pump demand and optional smart-charging/grid-service events.

It combines:

- ASHP central-heating and hot-water forecasting;
- whole-home load, PV, battery, import/export and cost forecasting;
- battery planning and control;
- optional EV Smart Charging support;
- optional Axle VPP event support.

## Install

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open **Repositories**.
3. Add:

   `https://github.com/sunnysideal/Home-Energy-Manager`

4. Refresh the App store.
5. Install **Home Energy Manager**.
6. Open the **Configuration** tab and enter the Home Assistant entities for your installation.
7. Leave **Operation mode** set to `forecast_only` for the first start.
8. Start the app and check the forecast/health entities before enabling battery control.

Home Energy Manager currently supports `aarch64` and `amd64` Home Assistant systems.

## Before enabling control

`forecast_only` is the safe first-run mode. In this mode the forecasters run, but Home Energy Manager does not change inverter settings.

Do not enable a control mode until the battery, inverter, grid, tariff, solar and heat-pump inputs required for your installation have been checked in Home Assistant.

Only one system should actively control the inverter schedule at a time. Disable any other automation or controller that writes the same charge, discharge or pause settings before enabling Home Energy Manager control.

## Documentation

See [`home_energy_manager/DOCS.md`](home_energy_manager/DOCS.md) for the complete fresh-install guide, configuration reference, operating modes, output entities and troubleshooting.

The app-specific overview is in [`home_energy_manager/README.md`](home_energy_manager/README.md).

## License

Home Energy Manager is licensed under the [MIT License](LICENSE). Third-party notices are available in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
