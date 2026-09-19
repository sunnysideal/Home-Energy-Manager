# Editable import prices in the forecast card

The optional Home Energy Forecast card shows the next half-hour slots and allows you to override an import price for one slot. Overrides are saved by Home Energy Manager and used by its home-energy forecast; they do not change your electricity supplier's tariff.

## Install the card

Copy `www/home-energy-forecast-card.js` from this add-on repository to your Home Assistant `/config/www/home-energy-forecast-card.js`. In **Settings → Dashboards → Resources**, add `/local/home-energy-forecast-card.js` as a JavaScript module. Refresh the browser/app after updating the file.

Add a Manual card to a dashboard:

```yaml
type: custom:home-energy-forecast-card
entity: sensor.home_energy_forecast
title: Home Energy Forecast
rows: 16
edit_entity: text.home_energy_manager_tariff_edit
result_entity: sensor.home_energy_manager_tariff_edit_result
```

MQTT must be available and enabled for editable prices. The add-on discovers the writable text entity and result sensor automatically; do not create an input_text helper. If Home Assistant has assigned a different entity ID, select the actual entity IDs in the card configuration.

## Edit a price

Tap the price in the appropriate half-hour row, enter a price in **p/kWh**, and select **Save**. The card sends the selected row's dated timestamp and price to the add-on using the Home Assistant `text.set_value` action. The result sensor reports whether the edit was saved or rejected. **Restore supplier price** removes the override for that half-hour. Changes appear in the forecast after the next recalculation; saving a command does not mean the supplier tariff was modified.

The editor's existing ingress page also supports managing longer dated price windows. It shares the same persisted override file. If MQTT is unavailable, the forecast remains readable but card edits cannot be submitted.
