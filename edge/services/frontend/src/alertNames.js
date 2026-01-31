// Human-friendly names for Victron MQTT metrics
const ALERT_NAME_MAP = {
  // Battery alarms
  'victron_battery_alarms_chargeblocked_value': 'Battery Charge Blocked',
  'victron_battery_alarms_dischargeblocked_value': 'Battery Discharge Blocked',
  'victron_battery_alarms_highchargecurrent_value': 'High Charge Current',
  'victron_battery_alarms_highdischargecurrent_value': 'High Discharge Current',
  'victron_battery_alarms_highcellvoltage_value': 'High Cell Voltage',
  'victron_battery_alarms_lowvoltage_value': 'Low Battery Voltage',
  'victron_battery_alarms_hightemperature_value': 'High Battery Temp',
  'victron_battery_alarms_lowtemperature_value': 'Low Battery Temp',
  'victron_battery_alarms_highchargetemperature_value': 'High Charge Temp',
  'victron_battery_alarms_lowchargetemperature_value': 'Low Charge Temp',
  'victron_battery_alarms_cellimbalance_value': 'Cell Imbalance',
  'victron_battery_alarms_internalfailure_value': 'Internal Failure',

  // VEBus / Inverter alarms
  'victron_vebus_alarms_gridlost_value': 'Grid Lost',
  'victron_vebus_alarms_bmsconnectionlost_value': 'BMS Connection Lost',
  'victron_vebus_alarms_mainsimbalance_value': 'Mains Imbalance',
  'victron_vebus_alarms_inverterimbalance_value': 'Inverter Imbalance',
  'victron_vebus_alarms_hightemperature_value': 'Inverter High Temp',
  'victron_vebus_alarms_lowbattery_value': 'Low Battery',
  'victron_vebus_alarms_overload_value': 'Overload',
  'victron_vebus_alarms_ripple_value': 'Ripple',
  'victron_vebus_alarms_highdccurrent_value': 'High DC Current',
  'victron_vebus_alarms_highdcvoltage_value': 'High DC Voltage',
  'victron_vebus_alarms_phaserotation_value': 'Phase Rotation',
  'victron_vebus_alarms_temperaturesensor_value': 'Temp Sensor Error',
  'victron_vebus_alarms_voltagesensor_value': 'Voltage Sensor Error',

  // VEBus settings alarms
  'victron_vebus_settings_alarm_system_gridlost_value': 'Grid Lost (Setting)',
  'victron_settings_settings_alarm_system_gridlost_value': 'Grid Lost (System)',
  'victron_settings_settings_alarm_vebus_highdccurrent_value': 'High DC Current (Setting)',
  'victron_settings_settings_alarm_vebus_highdcripple_value': 'High DC Ripple (Setting)',
  'victron_settings_settings_alarm_vebus_highdcvoltage_value': 'High DC Voltage (Setting)',
  'victron_settings_settings_alarm_vebus_hightemperature_value': 'High Temp (Setting)',
  'victron_settings_settings_alarm_vebus_inverteroverload_value': 'Inverter Overload (Setting)',
  'victron_settings_settings_alarm_vebus_lowbattery_value': 'Low Battery (Setting)',
  'victron_settings_settings_alarm_vebus_temperaturesenseerror_value': 'Temp Sensor Error (Setting)',
  'victron_settings_settings_alarm_vebus_vebuserror_value': 'VEBus Error',
  'victron_settings_settings_alarm_vebus_voltagesenseerror_value': 'Voltage Sensor Error (Setting)',

  // Generator alarms
  'victron_settings_settings_generator0_alarms_autostartdisabled_value': 'Gen0 Autostart Disabled',
  'victron_settings_settings_generator0_alarms_nogeneratoratacin_value': 'Gen0 No Generator at AC In',
  'victron_settings_settings_generator1_alarms_autostartdisabled_value': 'Gen1 Autostart Disabled',
  'victron_settings_settings_generator1_alarms_nogeneratoratacin_value': 'Gen1 No Generator at AC In',

  // VE.CAN alarms
  'victron_vecan_alarms_sameuniquenameused_value': 'VE.CAN Duplicate Name',
};

/**
 * Convert a raw Victron metric name to a human-friendly name.
 * Uses explicit mapping if available, otherwise applies generic transform.
 */
export function formatAlertName(rawName) {
  if (!rawName) return '';

  // Check explicit mapping first
  const mapped = ALERT_NAME_MAP[rawName.toLowerCase()];
  if (mapped) return mapped;

  // Generic transform: strip prefix/suffix, replace underscores, title case
  let name = rawName;

  // Strip victron_ prefix
  if (name.toLowerCase().startsWith('victron_')) {
    name = name.slice(8);
  }

  // Remove common middle segments
  name = name.replace(/settings_settings_/gi, '');
  name = name.replace(/alarms_/gi, '');

  // Strip common suffixes
  const suffixes = ['_value', '_repeat', '_state', '_min', '_max'];
  for (const suffix of suffixes) {
    if (name.toLowerCase().endsWith(suffix)) {
      name = name.slice(0, -suffix.length);
      break;
    }
  }

  // Replace underscores with spaces and title case
  return name
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

export default ALERT_NAME_MAP;
