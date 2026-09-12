"""Controller forecast discovery and controller-input normalisation.

This module owns interpretation of the Home Energy Forecaster's controller_inputs
contract. It deliberately preserves the legacy controller mutation/persistence
semantics while moving that responsibility out of the orchestration core.
"""

import logging
from zoneinfo import ZoneInfo

LOG = logging.getLogger('home_energy_controller')

REQUIRED_DISCOVERED = [
    'battery_soc_entity','battery_capacity_entity','battery_reserve_entity',
    'pv_energy_total_entity','grid_import_energy_total_entity','grid_export_energy_total_entity',
    'inverter_max_charge_rate_entity','inverter_max_discharge_rate_entity',
    'eco_mode_entity','charge_schedule_enable_entity','discharge_schedule_enable_entity',
    'charge_slot_1_start_entity','charge_slot_1_end_entity','charge_slot_1_target_entity','charge_rate_entity',
    'discharge_slot_1_start_entity','discharge_slot_1_end_entity','discharge_slot_1_target_entity','discharge_rate_entity',
    'pause_mode_entity','pause_start_entity','pause_end_entity'
]

ENTITY_MAPPING = {
    'battery_soc':'battery_soc_entity',
    'battery_capacity':'battery_capacity_entity',
    'battery_reserve':'battery_reserve_entity',
    'pv_energy_total':'pv_energy_total_entity',
    'grid_import_energy_total':'grid_import_energy_total_entity',
    'grid_export_energy_total':'grid_export_energy_total_entity',
    'inverter_max_charge_rate':'inverter_max_charge_rate_entity',
    'inverter_max_discharge_rate':'inverter_max_discharge_rate_entity',
    'eco_mode':'eco_mode_entity',
    'charge_schedule_enable':'charge_schedule_enable_entity',
    'discharge_schedule_enable':'discharge_schedule_enable_entity',
    'charge_slot_1_start':'charge_slot_1_start_entity',
    'charge_slot_1_end':'charge_slot_1_end_entity',
    'charge_slot_1_target':'charge_slot_1_target_entity',
    'charge_rate':'charge_rate_entity',
    'discharge_slot_1_start':'discharge_slot_1_start_entity',
    'discharge_slot_1_end':'discharge_slot_1_end_entity',
    'discharge_slot_1_target':'discharge_slot_1_target_entity',
    'discharge_rate':'discharge_rate_entity',
    'pause_mode':'pause_mode_entity',
    'pause_start':'pause_start_entity',
    'pause_end':'pause_end_entity',
}


def discover_from_forecast(controller, state):
    """Apply the forecaster discovery contract to an existing controller."""
    attrs=state.get('attributes',{}) if isinstance(state,dict) else {}
    ci=attrs.get('controller_inputs')
    if not isinstance(ci,dict):
        controller.discovery_ready=False
        controller.err('input','controller_inputs','Forecast sensor does not expose controller_inputs; Home Energy Forecaster v0.2.8+ is required',100)
        return False
    if int(ci.get('version') or 0) < 3:
        controller.discovery_ready=False
        controller.err('input','controller_inputs','Forecast controller_inputs v3 required; update Home Energy Forecaster to v0.2.8+',100)
        return False
    controller.refresh_request_entity=str(ci.get('refresh_request_entity') or '').strip()
    if not controller.refresh_request_entity:
        controller.discovery_ready=False
        controller.err('input','controller_inputs','Forecast controller_inputs.refresh_request_entity is missing',100)
        return False
    ents=ci.get('entities')
    if not isinstance(ents,dict):
        controller.discovery_ready=False
        controller.err('input','controller_inputs','Forecast controller_inputs.entities is missing/invalid',100)
        return False
    for src,dst in ENTITY_MAPPING.items():
        controller.c[dst]=str(ents.get(src,'')).strip()
    vals=ci.get('values') if isinstance(ci.get('values'),dict) else {}
    metering=attrs.get('metering') if isinstance(attrs.get('metering'),dict) else {}
    meter_import_entity=str(metering.get('import_entity') or '').strip()
    meter_export_entity=str(metering.get('export_entity') or '').strip()
    if meter_import_entity:
        controller.c['grid_import_energy_total_entity']=meter_import_entity
    if meter_export_entity:
        controller.c['grid_export_energy_total_entity']=meter_export_entity
    ci_import_source=str(vals.get('grid_import_meter_source') or '').strip()
    ci_export_source=str(vals.get('grid_export_meter_source') or '').strip()
    meter_import_source=str(metering.get('import_source') or '').strip()
    meter_export_source=str(metering.get('export_source') or '').strip()
    if meter_import_source and ci_import_source and meter_import_source != ci_import_source:
        LOG.warning('Forecast meter provenance mismatch for import: metering=%s controller_inputs=%s; using metering', meter_import_source, ci_import_source)
    if meter_export_source and ci_export_source and meter_export_source != ci_export_source:
        LOG.warning('Forecast meter provenance mismatch for export: metering=%s controller_inputs=%s; using metering', meter_export_source, ci_export_source)
    controller.c['grid_import_meter_source']=meter_import_source or ci_import_source or 'battery'
    controller.c['grid_export_meter_source']=meter_export_source or ci_export_source or 'battery'
    controller.c['ev_included_in_battery_load']=bool(metering.get('ev_included_in_battery_load', vals.get('ev_included_in_battery_load',False)))
    tz=str(ci.get('timezone') or controller.c.get('timezone') or 'Europe/London')
    try:
        controller.tz=ZoneInfo(tz)
    except Exception:
        controller.discovery_ready=False
        controller.err('input','timezone',f'Invalid timezone from forecast: {tz}',90)
        return False
    missing=[k for k in REQUIRED_DISCOVERED if not str(controller.c.get(k,'')).strip()]
    if missing:
        controller.discovery_ready=False
        controller.err('input','controller_inputs','Forecast controller_inputs missing: '+', '.join(missing[:5]),100)
        return False
    controller.discovery_ready=True
    controller.clear('input','controller_inputs'); controller.clear('input','timezone')
    if controller.db.ok:
        controller.db.set('controller_inputs',{
            'version':ci.get('version'),
            'timezone':tz,
            'refresh_request_entity':controller.refresh_request_entity,
            'entities':{src:controller.c.get(dst) for src,dst in ENTITY_MAPPING.items()},
            'grid_import_meter_source':controller.c.get('grid_import_meter_source'),
            'grid_export_meter_source':controller.c.get('grid_export_meter_source'),
            'ev_included_in_battery_load':controller.c.get('ev_included_in_battery_load',False),
        })
    return True
