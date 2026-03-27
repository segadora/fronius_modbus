from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory

DOMAIN = 'fronius_modbus'
CONNECTION_MODBUS = 'modbus'
DEFAULT_NAME = 'Fronius'
ENTITY_PREFIX = 'fm'
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_PORT = 502
DEFAULT_INVERTER_UNIT_ID = 1
DEFAULT_METER_UNIT_ID = 200
DEFAULT_METER_UNIT_IDS = [DEFAULT_METER_UNIT_ID]
DEFAULT_AUTO_ENABLE_MODBUS = True
DEFAULT_RESTRICT_MODBUS_TO_THIS_IP = False
API_USERNAME = "customer"
CONF_RECONFIGURE_REQUIRED = "_reconfigure_required"
MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX = "legacy_modbus_only_reconfigure_"
JSON_API_LOW_FIRMWARE_ISSUE_ID_PREFIX = "json_api_low_firmware_"
CONF_INVERTER_UNIT_ID = 'inverter_modbus_unit_id'
CONF_METER_UNIT_ID = 'meter_modbus_unit_id'
CONF_METER_UNIT_IDS = 'meter_modbus_unit_ids'
CONF_API_USERNAME = 'api_username'
CONF_API_PASSWORD = 'api_password'
CONF_AUTO_ENABLE_MODBUS = 'auto_enable_modbus'
CONF_RESTRICT_MODBUS_TO_THIS_IP = 'restrict_modbus_to_this_ip'
ATTR_MANUFACTURER = 'Fronius'
SUPPORTED_MANUFACTURERS = ['Fronius']
SUPPORTED_MODELS = ['Primo GEN24', 'Symo GEN24', 'Verto']

API_BATTERY_MODE = {
    0: 'Auto',
    1: 'Manual',
}

API_SOC_MODE = {
    'auto': 'Automatic',
    'manual': 'Manual',
}

STORAGE_EXT_CONTROL_MODE = {
    0: 'Auto',
    1: 'PV Charge Limit',
    2: 'Discharge Limit',
    3: 'PV Charge and Discharge Limit',
    4: 'Charge from Grid',
    5: 'Discharge to Grid',
    6: 'Block Discharging',
    7: 'Block Charging',
}

STORAGE_MODBUS_SELECT_TYPES = [
    ['Storage Control Mode', 'ext_control_mode', STORAGE_EXT_CONTROL_MODE],
]

STORAGE_API_SELECT_TYPES = [
    ['Battery API mode', 'api_battery_mode', API_BATTERY_MODE],
]

STORAGE_API_SWITCH_TYPES = [
    ['Charge from AC', 'api_charge_from_ac', 'mdi:power-plug-battery'],
    ['Charge from grid', 'api_charge_from_grid', 'mdi:transmission-tower-export'],
]

STORAGE_MODBUS_NUMBER_TYPES = [
    ['Grid discharge power', 'grid_discharge_power', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxDisChaRte'}],
    ['Grid charge power', 'grid_charge_power', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxChaRte'}],
    ['Discharge limit', 'discharge_limit',  {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxDisChaRte'}],
    ['PV charge limit', 'charge_limit', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxChaRte'}],
    ['SoC Minimum', 'soc_minimum', {'min': 5, 'max': 100, 'step': 1, 'mode':'box', 'unit': '%'}],
]

STORAGE_API_NUMBER_TYPES = [
    ['Target feed in', 'api_battery_power', {'min': -20000, 'max': 20000, 'step': 10, 'mode': 'box', 'unit': 'W'}],
    ['SoC Maximum', 'soc_maximum', {'min': 0, 'max': 100, 'step': 1, 'mode': 'box', 'unit': '%'}],
]

INVERTER_NUMBER_TYPES = [
    ['AC limit rate', 'ac_limit_rate', {'min': 0, 'max': 50000, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'max_power'}],
    ['Power factor', 'power_factor', {'min': -1, 'max': 1, 'step': 0.001, 'mode':'box', 'unit': None}],
]

INVERTER_SELECT_TYPES = [
    ['AC limit enable', 'ac_limit_enable', {0: 'Disabled', 1: 'Enabled'}],
    ['Power factor control', 'power_factor_enable', {0: 'Disabled', 1: 'Enabled'}],
    ['Inverter connection', 'Conn', {0: 'Disabled', 1: 'Enabled'}],
]

INVERTER_API_SWITCH_TYPES = [
    ['JSON API', 'api_solar_api_enabled', 'mdi:api', EntityCategory.DIAGNOSTIC],
]

INVERTER_SENSOR_TYPES = {
    'A': ['AC current', 'A', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphA': ['AC current L1', 'AphA', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'acpower': ['AC power', 'acpower', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'acenergy': ['AC energy', 'acenergy', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'tempcab': ['Temperature', 'tempcab', SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, '°C', 'mdi:thermometer', None],
    'pv_power': ['PV power', 'pv_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:solar-power', None],
    'load': ['Load', 'load', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'pv_connection': ['PV connection', 'pv_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'ecp_connection': ['Electrical connection', 'ecp_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'status': ['Status', 'status', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'statusvendor': ['Vendor status', 'statusvendor', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'line_frequency': ['Line frequency', 'line_frequency', SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, 'Hz', None, None],
    'inverter_controls': ['Control mode', 'inverter_controls', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'vref': ['Reference voltage', 'vref', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', EntityCategory.DIAGNOSTIC],
    'vrefofs': ['Reference voltage offset', 'vrefofs', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', EntityCategory.DIAGNOSTIC],
    'max_power': ['Maximum power', 'max_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'events2': ['Events', 'events2', None, None, None, None, EntityCategory.DIAGNOSTIC],    

    'grid_status': ['Grid status', 'grid_status', None, None, None, None, EntityCategory.DIAGNOSTIC],

    'Conn': ['Connection control', 'Conn', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'WMaxLim_Ena': ['Throttle control', 'WMaxLim_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'OutPFSet_Ena': ['Fixed power factor status', 'OutPFSet_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'VArPct_Ena': ['Limit VAr control', 'VArPct_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'PhVphA': ['AC voltage L1-N', 'PhVphA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'unit_id': ['Modbus ID', 'i_unit_id', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'ac_limit_rate': ['AC limit rate', 'ac_limit_rate', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:chart-line', None],
    'ac_limit_enable': ['AC limit enabled', 'ac_limit_enable', None, None, None, 'mdi:power-plug', EntityCategory.DIAGNOSTIC],
    'isolation_resistance': ['Isolation Resistance', 'isolation_resistance', None, SensorStateClass.MEASUREMENT, 'MΩ', 'mdi:omega', None],
}

INVERTER_WEB_SENSOR_TYPES = {
    'api_modbus_mode': ['Web API Modbus mode', 'api_modbus_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_control': ['Web API Modbus control', 'api_modbus_control', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_sunspec_mode': ['Web API SunSpec mode', 'api_modbus_sunspec_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_restriction': ['Web API Modbus restriction', 'api_modbus_restriction', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_restriction_ip': ['Web API Modbus restriction IP', 'api_modbus_restriction_ip', None, None, None, None, EntityCategory.DIAGNOSTIC],
}

MPPT_MODULE_SENSOR_TYPES = [
    ['DC current', 'dc_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    ['DC voltage', 'dc_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    ['DC power', 'dc_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:solar-power', None],
    ['Lifetime energy', 'lifetime_energy', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:solar-panel', None],
]

INVERTER_SYMO_SENSOR_TYPES = {
    'AphB': ['AC current L2', 'AphB', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphC': ['AC current L3', 'AphC', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'PhVphB': ['AC voltage L2-N', 'PhVphB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphC': ['AC voltage L3-N', 'PhVphC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphAB': ['AC voltage L1-L2', 'PPVphAB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphBC': ['AC voltage L2-L3', 'PPVphBC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphCA': ['AC voltage L3-L1', 'PPVphCA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
}

INVERTER_STORAGE_SENSOR_TYPES = {
    'storage_charge_current': ['Storage charging current', 'storage_charge_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    'storage_charge_voltage': ['Storage charging voltage', 'storage_charge_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'storage_charge_power': ['Storage charging power', 'storage_charge_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:home-battery', None],
    'storage_charge_lfte': ['Storage charging lifetime energy', 'storage_charge_lfte', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:home-battery', None],
    'storage_discharge_current': ['Storage discharging current', 'storage_discharge_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    'storage_discharge_voltage': ['Storage discharging voltage', 'storage_discharge_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'storage_discharge_power': ['Storage discharging power', 'storage_discharge_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:home-battery', None],
    'storage_discharge_lfte': ['Storage discharging lifetime energy', 'storage_discharge_lfte', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:home-battery', None],
    'storage_connection': ['Storage connection', 'storage_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
}


METER_SENSOR_TYPES = {
    'A': ['AC current', 'A', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphA': ['AC current L1', 'AphA', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphB': ['AC current L2', 'AphB', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphC': ['AC current L3', 'AphC', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'power': ['Power', 'power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphA': ['Power L1', 'WphA', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphB': ['Power L2', 'WphB', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphC': ['Power L3', 'WphC', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'exported': ['Exported', 'exported', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'imported': ['Imported', 'imported', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'line_frequency': ['Line frequency', 'line_frequency', SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, 'Hz', None, None],
    'PhVphA': ['AC voltage L1-N', 'PhVphA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphB': ['AC voltage L2-N', 'PhVphB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphC': ['AC voltage L3-N', 'PhVphC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPV': ['AC voltage Line to Line', 'PPV', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'unit_id': ['Modbus ID', 'unit_id', None, None, None, None, EntityCategory.DIAGNOSTIC],
}

SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS = (
    "AphB",
    "AphC",
    "WphB",
    "WphC",
    "PhVphB",
    "PhVphC",
    "PPV",
)

STORAGE_SENSOR_TYPES = {
    'control_mode': ['Core storage control mode', 'control_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'charge_status': ['Charge status', 'charge_status', None, None, None, None, None, EntityCategory.DIAGNOSTIC],
    'max_charge': ['Max charging power', 'max_charge', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:gauge', EntityCategory.DIAGNOSTIC],
    'soc': ['State of charge', 'soc', SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT, '%', None, None],
    'charging_power': ['Charging power', 'charging_power',  None, None, '%', 'mdi:gauge', EntityCategory.DIAGNOSTIC],
    'discharging_power': ['Discharging power', 'discharging_power',  None, None, '%', 'mdi:gauge', EntityCategory.DIAGNOSTIC],
    'soc_minimum': ['SoC Minimum', 'soc_minimum',  None, None, '%', 'mdi:gauge', None],
    'grid_charging': ['Grid charging', 'grid_charging',  None, None, None, None, EntityCategory.DIAGNOSTIC],
    'WHRtg': ['Capacity', 'WHRtg',  SensorDeviceClass.ENERGY, SensorStateClass.MEASUREMENT, 'Wh', None, EntityCategory.DIAGNOSTIC],
    'MaxChaRte': ['Maximum charge rate', 'MaxChaRte',  SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', None, EntityCategory.DIAGNOSTIC],
    'MaxDisChaRte': ['Maximum discharge rate', 'MaxDisChaRte',  SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', None, EntityCategory.DIAGNOSTIC],
}
