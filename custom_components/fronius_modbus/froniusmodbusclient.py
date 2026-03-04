#import os, sys; sys.path.append(os.path.dirname(os.path.realpath(__file__)))

"""BYD Battery Box Class"""

import asyncio
import logging
import time
from typing import Optional
from .extmodbusclient import ExtModbusClient
import requests

from .froniusmodbusclient_const import (
    INVERTER_ADDRESS,
    COMMON_ADDRESS,
    NAMEPLATE_ADDRESS,
    STORAGE_ADDRESS,
    METER_ADDRESS,
    EXPORT_LIMIT_RATE_ADDRESS,
    EXPORT_LIMIT_ENABLE_ADDRESS,
    CONN_ADDRESS,
    SUNSPEC_ID_ADDRESS,
    SUNSPEC_FIRST_MODEL_HEADER_ADDRESS,
    SUNSPEC_ID_WORD_0,
    SUNSPEC_ID_WORD_1,
    SUNSPEC_END_MODEL_ID,
    SUNSPEC_SCAN_MAX_MODELS,
    STORAGE_CONTROL_MODE,
    CHARGE_STATUS,
    CHARGE_GRID_STATUS,
    STORAGE_EXT_CONTROL_MODE,
    FRONIUS_INVERTER_STATUS,
    CONNECTION_STATUS_CONDENSED,
    ECP_CONNECTION_STATUS,
    INVERTER_CONTROLS,
    INVERTER_EVENTS,
    CONTROL_STATUS,
    EXPORT_LIMIT_STATUS,
    GRID_STATUS,
#    INVERTER_STATUS,
#    CONNECTION_STATUS,
)

_LOGGER = logging.getLogger(__name__)

class FroniusModbusClient(ExtModbusClient):
    """Hub for BYD Battery Box Interface"""

    def __init__(self, host: str, port: int, inverter_unit_id: int, meter_unit_ids, timeout: int) -> None:
        """Init hub."""
        super(FroniusModbusClient, self).__init__(host = host, port = port, unit_id=inverter_unit_id, timeout=timeout)

        self.initialized = False

        self._inverter_unit_id = inverter_unit_id
        self._meter_unit_ids = meter_unit_ids

        self.meter_configured = False
        self.mppt_configured = False
        self.storage_configured = False
        self.storage_extended_control_mode = 0
        self.max_charge_rate_w = 11000
        self.max_discharge_rate_w = 11000
        self._storage_address = STORAGE_ADDRESS
        self.mppt_module_count = 2
        self.mppt_model_length = 88
        self._sunspec_models_by_id = {}
        self._sunspec_model_headers = []
        self._grid_frequency = 50
        self._grid_frequency_lower_bound = self._grid_frequency - 0.2
        self._grid_frequency_upper_bound = self._grid_frequency + 0.2

        self._inverter_frequency_lower_bound = self._grid_frequency - 5
        self._inverter_frequency_upper_bound = self._grid_frequency + 5

        self._export_limit_enable_mask_until = 0.0
        self.data = {}

    def _map_value(self, values: dict, key: int, field_name: str):
        value = values.get(key)
        if value is None:
            _LOGGER.debug("Unknown %s code %s", field_name, key)
            return f"Unknown ({key})"
        return value

    def _storage_register_address(self, offset: int) -> int:
        return self._storage_address + offset

    def _get_export_limit_rate_sf(self) -> Optional[int]:
        value = self.data.get("export_limit_rate_sf")
        if not self.is_numeric(value):
            return None
        rate_sf = int(value)
        if rate_sf < -6 or rate_sf > 6:
            _LOGGER.error("Invalid export limit scale factor: %s", rate_sf)
            return None
        return rate_sf

    def _get_inverter_max_power_w(self) -> Optional[float]:
        value = self.data.get("max_power")
        if not self.is_numeric(value):
            return None
        max_power_w = float(value)
        if max_power_w <= 0:
            return None
        return max_power_w

    def _export_limit_raw_to_percent(self, raw_value: int) -> Optional[float]:
        rate_sf = self._get_export_limit_rate_sf()
        if rate_sf is None or not self.is_numeric(raw_value):
            return None

        percent = float(raw_value) * (10 ** rate_sf)
        if percent < 0 or percent > 100:
            _LOGGER.error("Export limit percent out of range: raw=%s sf=%s pct=%s", raw_value, rate_sf, percent)
            return None
        return percent

    def _export_limit_raw_to_watts(self, raw_value: int) -> Optional[int]:
        percent = self._export_limit_raw_to_percent(raw_value)
        max_power_w = self._get_inverter_max_power_w()
        if percent is None or max_power_w is None:
            return None
        return int(round(max_power_w * percent / 100.0))

    def _export_limit_watts_to_raw(self, watts: float) -> Optional[int]:
        if not self.is_numeric(watts):
            return None

        max_power_w = self._get_inverter_max_power_w()
        rate_sf = self._get_export_limit_rate_sf()
        if max_power_w is None or rate_sf is None:
            return None

        clamped_watts = min(max(float(watts), 0.0), max_power_w)
        percent = (clamped_watts / max_power_w) * 100.0
        raw_unclamped = percent / (10 ** rate_sf)
        raw_max = int(round(100.0 / (10 ** rate_sf)))
        raw_value = int(round(raw_unclamped))
        return max(0, min(raw_max, raw_value))

    async def _read_export_limit_enable_raw(self) -> Optional[int]:
        regs = await self.get_registers(
            unit_id=self._inverter_unit_id,
            address=EXPORT_LIMIT_ENABLE_ADDRESS,
            count=1,
        )
        if regs is None:
            return None

        enable_raw = self._client.convert_from_registers(
            regs[0:1],
            data_type=self._client.DATATYPE.UINT16,
        )
        if not self.is_numeric(enable_raw):
            return None
        return int(enable_raw)

    def _sanitize_mppt_u16(self, value: Optional[int]) -> Optional[int]:
        if not self.is_numeric(value):
            return None
        sanitized = int(value)
        if sanitized == 0xFFFF:
            return None
        return sanitized

    def _sanitize_mppt_u32(self, value: Optional[int]) -> Optional[int]:
        if not self.is_numeric(value):
            return None
        sanitized = int(value)
        if sanitized == 0xFFFFFFFF:
            return None
        return sanitized

    def _update_storage_base_address(self, mppt_model_length: int, mppt_data_address: Optional[int] = None):
        if not self.is_numeric(mppt_model_length):
            return

        model_length = int(mppt_model_length)
        if model_length <= 0 or model_length > 4096:
            return

        if not self.is_numeric(mppt_data_address):
            return

        # Storage model data starts directly behind the next model header after model 160.
        # candidate = mppt_data_address + model_length + 2 (skip next model header ID + L).
        candidate = int(mppt_data_address) + model_length + 2
        if candidate < 40000 or candidate > 50000:
            return

        self.mppt_model_length = model_length
        self._storage_address = candidate
        self.data["storage_model_address"] = candidate

    def _get_sunspec_model(self, model_id: int):
        models = self._sunspec_models_by_id.get(model_id)
        if not models:
            return None
        return models[0]

    async def _scan_sunspec_models(self, force: bool = False) -> bool:
        if self._sunspec_model_headers and not force:
            return True

        sid_regs = await self.get_registers(
            unit_id=self._inverter_unit_id,
            address=SUNSPEC_ID_ADDRESS,
            count=2,
        )
        if sid_regs is None or len(sid_regs) != 2:
            return False
        if sid_regs[0] != SUNSPEC_ID_WORD_0 or sid_regs[1] != SUNSPEC_ID_WORD_1:
            _LOGGER.error("Invalid SunSpec SID at %s: %s", SUNSPEC_ID_ADDRESS, sid_regs)
            return False

        models_by_id = {}
        model_headers = []
        header_address = SUNSPEC_FIRST_MODEL_HEADER_ADDRESS

        for _ in range(SUNSPEC_SCAN_MAX_MODELS):
            header_regs = await self.get_registers(
                unit_id=self._inverter_unit_id,
                address=header_address,
                count=2,
            )
            if header_regs is None or len(header_regs) != 2:
                return False

            model_id = int(header_regs[0])
            model_length = int(header_regs[1])
            if model_id == SUNSPEC_END_MODEL_ID:
                break
            if model_id <= 0 or model_length <= 0 or model_length > 4096:
                _LOGGER.error(
                    "Invalid SunSpec model header at %s: id=%s length=%s",
                    header_address,
                    model_id,
                    model_length,
                )
                return False

            model_entry = {
                "id": model_id,
                "length": model_length,
                "id_address": header_address,
                "l_address": header_address + 1,
                "data_address": header_address + 2,
            }
            model_headers.append(model_entry)
            if model_id not in models_by_id:
                models_by_id[model_id] = []
            models_by_id[model_id].append(model_entry)

            header_address = model_entry["data_address"] + model_length

        if not model_headers:
            return False

        self._sunspec_models_by_id = models_by_id
        self._sunspec_model_headers = model_headers
        self.data["sunspec_model_count"] = len(model_headers)
        return True

    async def init_data(self):
        await self.connect()
        try:
            result = await self.read_device_info_data(prefix='i_', unit_id=self._inverter_unit_id)
        except Exception as e:
            _LOGGER.error(f"Error reading inverter info {self._host}:{self._port} unit id: {self._inverter_unit_id}", exc_info=True)
            raise Exception(f"Error reading inverter info unit id: {self._inverter_unit_id}")
        if result == False:
            _LOGGER.error(f"Empty inverter info {self._host}:{self._port} unit id: {self._inverter_unit_id}")
            raise Exception(f"Empty inverter info unit id: {self._inverter_unit_id}")

        try:
            if await self.read_mppt_data():
                self.mppt_configured = True
        except Exception as e:
            _LOGGER.warning(f"Error while checking mppt data {e}")

        if len(self._meter_unit_ids)>5:
            _LOGGER.error(f"Too many meters configured, max 5")
            return
        #elif len(self._meter_unit_ids)>0:
        #    self.meter_configured = True

        for i in range(len(self._meter_unit_ids)):
            unit_id = self._meter_unit_ids[i]
            try:
                result = await self.read_device_info_data(prefix=f'm{i+1}_', unit_id=unit_id)
                if result:
                    if not self.meter_configured:
                        self.meter_configured = True
                else:
                    _LOGGER.error(f"Failed reading meter info {self._host}:{self._port} unit id: {unit_id}")
            except Exception as e:
                _LOGGER.error(f"Error reading meter info {self._host}:{self._port} unit id: {unit_id}", exc_info=True)

        if await self.read_inverter_nameplate_data() == False:
            _LOGGER.error(f"Error reading nameplate data", exc_info=True)
        elif self.mppt_configured:
            # Re-evaluate MPPT channels after storage detection from nameplate data.
            await self.read_mppt_data()

        _LOGGER.debug(f"Init done. data: {self.data}")

        return True

    def get_json_storage_info(self):
        self.data['s_manufacturer'] = None
        self.data['s_model'] = 'Battery Storage'
        self.data['s_serial'] = None

        url = f"http://{self._host}/solar_api/v1/GetStorageRealtimeData.cgi"

        try:
            response = requests.get(url)

            if response.status_code == 200:
                data = response.json()
            else:
                _LOGGER.error(f"Error storage json data {response.status_code}")
                return

            try:
                bodydata = data['Body']['Data']
            except Exception as e:
                _LOGGER.error(f"Error no body data in json data: {data}")
                return
    
            for c in bodydata.keys():
                try:
                    details = bodydata[c]['Controller']['Details']
                except Exception as e:
                    _LOGGER.error(f"Error no details in json bodydata: {bodydata}")
                    return

                self.data['s_manufacturer'] = details['Manufacturer']
                self.data['s_model'] = details['Model']
                self.data['s_serial'] = str(details['Serial']).strip()
                break

        except Exception as e:
            _LOGGER.error(f"Error storage json data {url} {e}", exc_info=True)

    async def read_device_info_data(self, prefix, unit_id):
        regs = await self.get_registers(unit_id=unit_id, address=COMMON_ADDRESS, count=65)
        if regs is None:
            return False

        manufacturer = self.get_string_from_registers(regs[0:16])
        model = self.get_string_from_registers(regs[16:32])
        options = self.get_string_from_registers(regs[32:40])
        sw_version = self.get_string_from_registers(regs[40:48])
        serial =  self.get_string_from_registers(regs[48:64])
        modbus_id = self._client.convert_from_registers(regs[64:65], data_type = self._client.DATATYPE.UINT16)

        self.data[prefix + 'manufacturer'] = manufacturer
        self.data[prefix + 'model'] = model
        self.data[prefix + 'options'] = options
        self.data[prefix + 'sw_version'] = sw_version
        self.data[prefix + 'serial'] = serial
        self.data[prefix + 'unit_id'] = modbus_id

        return True

    async def read_inverter_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=INVERTER_ADDRESS, count=50)
        if regs is None:
            return False

        PPVphAB = self._client.convert_from_registers(regs[5:6], data_type = self._client.DATATYPE.UINT16)
        PPVphBC = self._client.convert_from_registers(regs[6:7], data_type = self._client.DATATYPE.UINT16)
        PPVphCA = self._client.convert_from_registers(regs[7:8], data_type = self._client.DATATYPE.UINT16)
        PhVphA = self._client.convert_from_registers(regs[8:9], data_type = self._client.DATATYPE.UINT16)
        PhVphB = self._client.convert_from_registers(regs[9:10], data_type = self._client.DATATYPE.UINT16)
        PhVphC = self._client.convert_from_registers(regs[10:11], data_type = self._client.DATATYPE.UINT16)
        V_SF = self._client.convert_from_registers(regs[11:12], data_type = self._client.DATATYPE.INT16)

        W = self._client.convert_from_registers(regs[12:13], data_type = self._client.DATATYPE.INT16)
        W_SF = self._client.convert_from_registers(regs[13:14], data_type = self._client.DATATYPE.INT16)
        Hz = self._client.convert_from_registers(regs[14:15], data_type = self._client.DATATYPE.INT16)
        Hz_SF = self._client.convert_from_registers(regs[15:16], data_type = self._client.DATATYPE.INT16)

        WH = self._client.convert_from_registers(regs[22:24], data_type = self._client.DATATYPE.UINT32)
        WH_SF = self._client.convert_from_registers(regs[24:25], data_type = self._client.DATATYPE.INT16)

        TmpCab = self._client.convert_from_registers(regs[31:32], data_type = self._client.DATATYPE.INT16)
        Tmp_SF = self._client.convert_from_registers(regs[35:36], data_type = self._client.DATATYPE.INT16)
        #St = self._client.convert_from_registers(regs[36:37], data_type = self._client.DATATYPE.UINT16)
        StVnd = self._client.convert_from_registers(regs[37:38], data_type = self._client.DATATYPE.UINT16)
        #EvtVnd1 = self._client.convert_from_registers(regs[42:44], data_type = self._client.DATATYPE.UINT32)
        EvtVnd2 = self._client.convert_from_registers(regs[44:46], data_type = self._client.DATATYPE.UINT32)

        self.data['PPVphAB'] = self.calculate_value(PPVphAB, V_SF)
        self.data['PPVphBC'] = self.calculate_value(PPVphBC, V_SF)
        self.data['PPVphCA'] = self.calculate_value(PPVphCA, V_SF)
        self.data['PhVphA'] = self.calculate_value(PhVphA, V_SF)
        self.data['PhVphB'] = self.calculate_value(PhVphB, V_SF)
        self.data['PhVphC'] = self.calculate_value(PhVphC, V_SF)
        self.data['tempcab'] = self.calculate_value(TmpCab, Tmp_SF)
        self.data["acpower"] = self.calculate_value(W, W_SF, 2, -50000, 50000)
        self.data["line_frequency"] = self.calculate_value(Hz, Hz_SF, 2, 0, 100)
        self.data["acenergy"] = self.calculate_value(WH, WH_SF) 
        #self.data["status"] = INVERTER_STATUS[St]
        self.data["statusvendor"] = self._map_value(FRONIUS_INVERTER_STATUS, StVnd, "inverter status")
        self.data["statusvendor_id"] = StVnd
        #self.data["events1"] = self.bitmask_to_string(EvtVnd1,INVERTER_EVENTS,default='None',bits=32)  
        self.data["events2"] = self.bitmask_to_string(EvtVnd2,INVERTER_EVENTS,default='None',bits=32)  

        return True

    async def read_inverter_nameplate_data(self):
        """start reading storage data"""
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=NAMEPLATE_ADDRESS, count=120)
        if regs is None:
            return False

        # DERTyp: Type of DER device. Default value is 4 to indicate PV device.
        DERTyp = self._client.convert_from_registers(regs[0:1], data_type = self._client.DATATYPE.UINT16)
        # WHRtg: Nominal energy rating of storage device.
        WHRtg = self._client.convert_from_registers(regs[17:18], data_type = self._client.DATATYPE.UINT16)
        WHRtg_SF = self._client.convert_from_registers(regs[18:19], data_type = self._client.DATATYPE.INT16)
        # MaxChaRte: Maximum rate of energy transfer into the storage device.
        MaxChaRte = self._client.convert_from_registers(regs[21:22], data_type = self._client.DATATYPE.UINT16)
        MaxChaRte_SF = self._client.convert_from_registers(regs[22:23], data_type = self._client.DATATYPE.INT16)
        # MaxDisChaRte: Maximum rate of energy transfer out of the storage device.
        MaxDisChaRte = self._client.convert_from_registers(regs[23:24], data_type = self._client.DATATYPE.UINT16)
        MaxDisChaRte_SF = self._client.convert_from_registers(regs[24:25], data_type = self._client.DATATYPE.INT16)

        has_storage_ratings = any(
            self.is_numeric(value) and 0 < value < 65535
            for value in [WHRtg, MaxChaRte, MaxDisChaRte]
        )
        if DERTyp == 82 or has_storage_ratings:
            self.storage_configured = True
        self.data['DERTyp'] = DERTyp
        self.data['WHRtg'] = self.calculate_value(WHRtg, WHRtg_SF, 0)
        self.data['MaxChaRte'] = self.calculate_value(MaxChaRte, MaxChaRte_SF, 0)
        self.data['MaxDisChaRte'] = self.calculate_value(MaxDisChaRte, MaxDisChaRte_SF, 0)
    
        if self.is_numeric(self.data['MaxChaRte']):
            self.max_charge_rate_w = self.data['MaxChaRte']
        if self.is_numeric(self.data['MaxDisChaRte']):
            self.max_discharge_rate_w = self.data['MaxDisChaRte']

        return True

    async def read_inverter_status_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40183, count=44)
        if regs is None:
            return False

        PVConn = self._client.convert_from_registers(regs[0:1], data_type = self._client.DATATYPE.UINT16)
        StorConn = self._client.convert_from_registers(regs[1:2], data_type = self._client.DATATYPE.UINT16)
        ECPConn = self._client.convert_from_registers(regs[2:3], data_type = self._client.DATATYPE.UINT16)

        StActCtl = self._client.convert_from_registers(regs[33:35], data_type = self._client.DATATYPE.UINT32)
        
        Ris = self._client.convert_from_registers(regs[42:43], data_type = self._client.DATATYPE.UINT16)
        Ris_SF = self._client.convert_from_registers(regs[43:44], data_type = self._client.DATATYPE.UINT16)

        self.data['pv_connection'] = self._map_value(CONNECTION_STATUS_CONDENSED, PVConn, 'pv connection')
        self.data['storage_connection'] = self._map_value(CONNECTION_STATUS_CONDENSED, StorConn, 'storage connection')
        self.data['ecp_connection'] = self._map_value(ECP_CONNECTION_STATUS, ECPConn, 'electrical connection')
        self.data['inverter_controls'] = self.bitmask_to_string(StActCtl, INVERTER_CONTROLS, 'Normal')
        # Adjust the scaling factor because isolation resistance is provided
        # in Ohm and stored in Mega Ohm.
        self.data['isolation_resistance'] = self.calculate_value(Ris, Ris_SF-6)

        return True

    async def read_inverter_model_settings_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40151, count=30)
        if regs is None:
            return False

        WMax = self._client.convert_from_registers(regs[0:1], data_type = self._client.DATATYPE.UINT16)
        #VRef = self._client.convert_from_registers(regs[1:2], data_type = self._client.DATATYPE.UINT16)
        #VRefOfs = self._client.convert_from_registers(regs[2:3], data_type = self._client.DATATYPE.UINT16)

        WMax_SF = self._client.convert_from_registers(regs[20:21], data_type = self._client.DATATYPE.INT16)
        #VRef_SF = self._client.convert_from_registers(regs[21:22], data_type = self._client.DATATYPE.INT16)
        #VRefOfs_SF = self._client.convert_from_registers(regs[21:22], data_type = self._client.DATATYPE.INT16)

        self.data['max_power'] = self.calculate_value(WMax, WMax_SF,2,0,50000) 
        #self.data['vref'] = self.calculate_value(VRef, VRef_SF) # At PCC 
        #self.data['vrefofs'] = self.calculate_value(VRefOfs, VRefOfs_SF) # At PCC 

        return True

    async def read_inverter_controls_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40229, count=24)
        if regs is None:
            return False

        Conn = self._client.convert_from_registers(regs[2:3], data_type = self._client.DATATYPE.UINT16)
        WMaxLim_Ena = self._client.convert_from_registers(regs[7:8], data_type = self._client.DATATYPE.UINT16)
        OutPFSet_Ena = self._client.convert_from_registers(regs[12:13], data_type = self._client.DATATYPE.UINT16)
        VArPct_Ena = self._client.convert_from_registers(regs[20:21], data_type = self._client.DATATYPE.INT16)
        WMaxLimPct_SF = self._client.convert_from_registers(regs[21:22], data_type = self._client.DATATYPE.INT16)

        self.data['Conn'] = self._map_value(CONTROL_STATUS, Conn, 'connection control')
        self.data['WMaxLim_Ena'] = self._map_value(CONTROL_STATUS, WMaxLim_Ena, 'throttle control')
        self.data['OutPFSet_Ena'] = self._map_value(CONTROL_STATUS, OutPFSet_Ena, 'fixed power factor')
        self.data['VArPct_Ena'] = self._map_value(CONTROL_STATUS, VArPct_Ena, 'VAr control')
        self.data['export_limit_rate_sf'] = WMaxLimPct_SF

        return True

    def protect_lfte(self, key, value):
        ''' ensure lfte values are monotonically increasing to fullfil the properties of SensorStateClass.TOTAL_INCREASING.
            Therfore this function returns the previous, last known good value, in case the modbus read was erroneus:
            * the current value from modbus is None
            * the current value from modbus is smaller than the previous value
            * the current value from modbus is much larger then the previous value
            This avoids wrong spikes in consumption / production on the energy dashboard
        '''

        if key not in self.data:
            _LOGGER.info(f"Initializing {key}={value}")
            return value
        elif self.data[key] is None:
            # None is a invalid value for monotonically increasing data.
            # hopefully never happens
            _LOGGER.info(f"Found initial {key}=None. Now using new value {value}")
            return value
        elif value is None:
            _LOGGER.warn(f"Received implausible {key}={value}. Using previous plausible value {self.data[key]}")
            return self.data[key]
        elif value < self.data[key]:
            _LOGGER.warn(f"Received implausible (too small) {key}={value} < previous plausible value {self.data[key]}")
            return self.data[key]
        elif value > self.data[key] + 100000:
            # we allow steps of 100 kWh. Usually, at a typicall rate every 10 seconds the steps should be far below.
            # However, when data transfer is not working for minutes or even an hour it could become relevant.
            # Also, wrong values are often by orders of magnitude to large, which should still be avoided by this check.

            _LOGGER.warn(f"Received implausible (too large) {key}={value} >> previous plausible value {self.data[key]}")
            return self.data[key]
        else:
            return value

    async def read_mppt_data(self):
        if not await self._scan_sunspec_models():
            return False

        mppt_model = self._get_sunspec_model(160)
        if mppt_model is None:
            return False

        mppt_model_length = int(mppt_model["length"])
        if mppt_model_length < 20 or mppt_model_length > 200:
            return False

        mppt_read_address = int(mppt_model["l_address"])
        self._update_storage_base_address(
            mppt_model_length,
            mppt_data_address=mppt_model["data_address"],
        )
        self.data['mppt_model_length'] = mppt_model_length
        self.data['mppt_register_address'] = mppt_read_address
        self.data['mppt_model_id_address'] = int(mppt_model["id_address"])

        storage_model = self._get_sunspec_model(124)
        if storage_model is not None:
            storage_model_id = int(storage_model["id"])
            storage_model_length = int(storage_model["length"])
            self.data['storage_model_id'] = storage_model_id
            self.data['storage_model_length'] = storage_model_length
            self.data['storage_model_address'] = int(storage_model["data_address"])
            if storage_model_length == 24:
                self._storage_address = int(storage_model["data_address"])
                self.storage_configured = True

        model_register_count = mppt_model_length + 1
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=mppt_read_address, count=model_register_count)
        if regs is None:
            return False

        model_limit = len(regs)
        if model_limit < 8:
            return False

        DCA_SF = self._client.convert_from_registers(regs[1:2], data_type=self._client.DATATYPE.INT16)
        DCV_SF = self._client.convert_from_registers(regs[2:3], data_type=self._client.DATATYPE.INT16)
        DCW_SF = self._client.convert_from_registers(regs[3:4], data_type=self._client.DATATYPE.INT16)
        DCWH_SF = self._client.convert_from_registers(regs[4:5], data_type=self._client.DATATYPE.INT16)
        reported_module_count = self._client.convert_from_registers(regs[7:8], data_type=self._client.DATATYPE.UINT16)
        if not self.is_numeric(reported_module_count) or int(reported_module_count) <= 0:
            return False

        max_modules_by_length = (model_limit - 2) // 20
        module_count = min(int(reported_module_count), int(max_modules_by_length))
        if module_count <= 0:
            return False

        self.mppt_module_count = module_count
        self.data['mppt_module_count'] = module_count

        def read_u16(index: int):
            if index + 1 > model_limit:
                return None
            return self._client.convert_from_registers(regs[index:index + 1], data_type=self._client.DATATYPE.UINT16)

        def read_u32(index: int):
            if index + 2 > model_limit:
                return None
            return self._client.convert_from_registers(regs[index:index + 2], data_type=self._client.DATATYPE.UINT32)

        module_power = {}
        module_lfte = {}
        module_tms = {}
        module_labels = {}
        module_current = {}
        module_voltage = {}

        for module_id in range(1, module_count + 1):
            label_idx = 20 * (module_id - 1) + 10
            current_idx = 20 * module_id - 2
            voltage_idx = 20 * module_id - 1
            power_idx = 20 * module_id
            lfte_idx = power_idx + 1
            tms_idx = lfte_idx + 2

            label = None
            if label_idx + 8 <= model_limit:
                try:
                    label = self.get_string_from_registers(regs[label_idx:label_idx + 8])
                except Exception:
                    label = None
            module_labels[module_id] = label

            raw_current = self._sanitize_mppt_u16(read_u16(current_idx))
            raw_voltage = self._sanitize_mppt_u16(read_u16(voltage_idx))
            raw_power = self._sanitize_mppt_u16(read_u16(power_idx))
            raw_lfte = self._sanitize_mppt_u32(read_u32(lfte_idx))
            raw_tms = self._sanitize_mppt_u32(read_u32(tms_idx))

            module_current[module_id] = self.calculate_value(raw_current, DCA_SF, 2, 0, 100) if raw_current is not None else None
            module_voltage[module_id] = self.calculate_value(raw_voltage, DCV_SF, 2, 0, 1500) if raw_voltage is not None else None
            module_power[module_id] = self.calculate_value(raw_power, DCW_SF, 2, 0, 15000) if raw_power is not None else None
            if raw_lfte is None:
                module_lfte[module_id] = None

            elif raw_lfte == 0 and raw_current is None and raw_voltage is None and raw_power is None:
                module_lfte[module_id] = None
            else:
                module_lfte[module_id] = self.calculate_value(raw_lfte, DCWH_SF)
            module_tms[module_id] = raw_tms

            self.data[f'module{module_id}_label'] = label
            self.data[f'module{module_id}_power'] = module_power[module_id]
            self.data[f'module{module_id}_lfte'] = module_lfte[module_id]
            self.data[f'module{module_id}_tms'] = module_tms[module_id]

            module_idx = module_id - 1
            self.data[f'mppt_module_{module_idx}_label'] = label
            self.data[f'mppt_module_{module_idx}_dc_current'] = module_current[module_id]
            self.data[f'mppt_module_{module_idx}_dc_voltage'] = module_voltage[module_id]
            self.data[f'mppt_module_{module_idx}_dc_power'] = module_power[module_id]
            self.data[f'mppt_module_{module_idx}_lifetime_energy'] = self.protect_lfte(
                f'mppt_module_{module_idx}_lifetime_energy',
                module_lfte[module_id],
            )
            self.data[f'mppt_module_{module_idx}_timestamp'] = module_tms[module_id]

        storage_charge_module = None
        storage_discharge_module = None
        for module_id, label in module_labels.items():
            if not isinstance(label, str):
                continue
            normalized = label.replace(" ", "").upper()
            if "STDISCHA" in normalized:
                storage_discharge_module = module_id
            elif normalized.startswith("STCHA"):
                storage_charge_module = module_id

        # If labels are unavailable, use the last two channels as storage channels.
        if self.storage_configured and not (storage_charge_module and storage_discharge_module) and module_count >= 4:
            storage_charge_module = module_count - 1
            storage_discharge_module = module_count

        if self.storage_configured and storage_charge_module and storage_discharge_module:
            self.data['storage_charge_module'] = storage_charge_module
            self.data['storage_discharge_module'] = storage_discharge_module

            storage_charge_power = module_power.get(storage_charge_module)
            storage_discharge_power = module_power.get(storage_discharge_module)
            self.data['storage_charge_power'] = storage_charge_power
            self.data['storage_discharge_power'] = storage_discharge_power
            self.data['storage_charge_lfte'] = self.protect_lfte('storage_charge_lfte', module_lfte.get(storage_charge_module))
            self.data['storage_discharge_lfte'] = self.protect_lfte('storage_discharge_lfte', module_lfte.get(storage_discharge_module))

            if self.is_numeric(storage_charge_power) and self.is_numeric(storage_discharge_power):
                self.data['storage_power'] = round(storage_discharge_power - storage_charge_power, 2)
            else:
                self.data['storage_power'] = None
        elif self.storage_configured:
            self.data['storage_charge_power'] = None
            self.data['storage_discharge_power'] = None
            self.data['storage_charge_lfte'] = None
            self.data['storage_discharge_lfte'] = None
            self.data['storage_power'] = None

        pv_modules = []
        for module_id, label in module_labels.items():
            if isinstance(label, str) and "MPPT" in label.upper():
                pv_modules.append(module_id)
        if not pv_modules:
            if storage_charge_module and storage_discharge_module:
                pv_modules = [module_id for module_id in range(1, module_count + 1) if module_id not in [storage_charge_module, storage_discharge_module]]
            else:
                pv_modules = list(range(1, module_count + 1))

        pv_values = [module_power.get(module_id) for module_id in pv_modules if self.is_numeric(module_power.get(module_id))]
        self.data['pv_power'] = round(sum(pv_values), 2) if pv_values else None

        return True

    async def read_inverter_storage_data(self):
        """start reading storage data"""
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=self._storage_address, count=24)
        if regs is None:
            return False
        
        # WChaMax: Reference Value for maximum Charge and Discharge.
        max_charge = self._client.convert_from_registers(regs[0:1], data_type = self._client.DATATYPE.UINT16)
        # WChaGra: Setpoint for maximum charging rate. Default is MaxChaRte.
        WChaGra = self._client.convert_from_registers(regs[1:2], data_type = self._client.DATATYPE.UINT16)
        # WDisChaGra: Setpoint for maximum discharge rate. Default is MaxDisChaRte.
        WDisChaGra = self._client.convert_from_registers(regs[2:3], data_type = self._client.DATATYPE.UINT16)
        # StorCtl_Mod: Active hold/discharge/charge storage control mode.
        storage_control_mode = self._client.convert_from_registers(regs[3:4], data_type = self._client.DATATYPE.UINT16)
        # VAChaMax: not supported
        # MinRsvPct: Setpoint for minimum reserve for storage as a percentage of the nominal maximum storage.
        minimum_reserve = self._client.convert_from_registers(regs[5:6], data_type = self._client.DATATYPE.UINT16)
        # ChaState: Currently available energy as a percent of the capacity rating.
        charge_state = self._client.convert_from_registers(regs[6:7], data_type = self._client.DATATYPE.UINT16)
        # StorAval: not supported 
        # InBatV: not supported
        # ChaSt:  Charge status of storage device.
        charge_status = self._client.convert_from_registers(regs[9:10], data_type = self._client.DATATYPE.UINT16)
        # OutWRte: Defines maximum Discharge rate. If not used than the default is 100 and WChaMax defines max. Discharge rate.
        discharge_power = self._client.convert_from_registers(regs[10:11], data_type = self._client.DATATYPE.INT16)
        # InWRte: Defines maximum Charge rate. If not used than the default is 100 and WChaMax defines max. Charge rate.
        charge_power = self._client.convert_from_registers(regs[11:12], data_type = self._client.DATATYPE.INT16)
        # InOutWRte_WinTms: not supported
        # InOutWRte_RvrtTms: Timeout period for charge/discharge rate.
        #InOutWRte_RvrtTms = self._client.convert_from_registers(regs[13:14], data_type = self._client.DATATYPE.INT16)
        # InOutWRte_RmpTms: not supported
        # ChaGriSet
        charge_grid_set = self._client.convert_from_registers(regs[15:16], data_type = self._client.DATATYPE.UINT16)
        # WChaMax_SF: Scale factor for maximum charge. 0
        #max_charge_sf = self._client.convert_from_registers(regs[16:17], data_type = self._client.DATATYPE.INT16)
        # WChaDisChaGra_SF: Scale factor for maximum charge and discharge rate. 0
        # VAChaMax_SF: not supported
        # MinRsvPct_SF: Scale factor for minimum reserve percentage. -2
        # ChaState_SF: Scale factor for available energy percent. -2
        #charge_state_sf = self._client.convert_from_registers(regs[20:21], data_type = self._client.DATATYPE.INT16)
        # StorAval_SF: not supported
        # InBatV_SF: not supported
        # InOutWRte_SF: Scale factor for percent charge/discharge rate. -2

        if self.is_numeric(max_charge) and max_charge > 0:
            self.storage_configured = True

        self.data['grid_charging'] = self._map_value(CHARGE_GRID_STATUS, charge_grid_set, 'grid charging')
        #self.data['power'] = power
        self.data['charge_status'] = self._map_value(CHARGE_STATUS, charge_status, 'charge status')
        self.data['minimum_reserve'] =  self.calculate_value(minimum_reserve, -2, 2, 0, 100)
        self.data['discharging_power'] = self.calculate_value(discharge_power, -2, 2, -100, 100)
        self.data['charging_power'] = self.calculate_value(charge_power, -2, 2, -100, 100)
        self.data['soc'] = self.calculate_value(charge_state, -2, 2, 0, 100)
        self.data['max_charge'] = self.calculate_value(max_charge, 0, 0)
        self.data['WChaGra'] = self.calculate_value(WChaGra, 0, 0)
        self.data['WDisChaGra'] = self.calculate_value(WDisChaGra, 0, 0)

        mapped_control_mode = self._map_value(STORAGE_CONTROL_MODE, storage_control_mode, 'storage control mode')
        control_mode = self.data.get('control_mode')
        if control_mode is None or control_mode != mapped_control_mode:
            if discharge_power >= 0:
                self.data['discharge_limit'] = discharge_power / 100.0 
                self.data['grid_charge_power'] = 0
            else: 
                self.data['grid_charge_power'] = (discharge_power * -1) / 100.0 
                self.data['discharge_limit'] = 0
            if charge_power >= 0:
                self.data['charge_limit'] = charge_power / 100 
                self.data['grid_discharge_power'] = 0
            else: 
                self.data['grid_discharge_power'] = (charge_power * -1) / 100.0 
                self.data['charge_limit'] = 0

            self.data['control_mode'] = mapped_control_mode

        # set extended storage control mode at startup
        ext_control_mode = self.data.get('ext_control_mode')
        if ext_control_mode is None:
            if storage_control_mode == 0:
                ext_control_mode = 0
            elif storage_control_mode in [1,3] and charge_power == 0:
                ext_control_mode = 7
            elif storage_control_mode == 1:
                ext_control_mode = 1
            elif storage_control_mode in [2,3] and discharge_power < 0:
                ext_control_mode = 4
            elif storage_control_mode in [2,3] and charge_power < 0:
                ext_control_mode = 5
            elif storage_control_mode in [2,3] and discharge_power == 0:
                ext_control_mode = 6
            elif storage_control_mode == 2:
                ext_control_mode = 2
            elif storage_control_mode == 3:
                ext_control_mode = 3
            if not ext_control_mode is None:
                self.data['ext_control_mode'] = self._map_value(STORAGE_EXT_CONTROL_MODE, ext_control_mode, 'extended storage mode')
                self.storage_extended_control_mode = ext_control_mode

        if ext_control_mode == 7:
            soc = self.data.get('soc')
            if storage_control_mode == 2 and soc == 100:
                _LOGGER.error(f'Calibration hit 100%, start discharge')
                await self.change_settings(1, 0, 100, 0)
            elif storage_control_mode == 3 and soc <= 5: 
                _LOGGER.error(f'Calibration hit 5%, return to auto mode')
                await self.set_auto_mode()
                await self.set_minimum_reserve(30)
                self.data['ext_control_mode'] = self._map_value(STORAGE_EXT_CONTROL_MODE, 0, 'extended storage mode')
                self.storage_extended_control_mode = 0

        return True

    async def read_meter_data(self, meter_prefix, unit_id):
        """start reading meter data"""
        regs = await self.get_registers(unit_id=unit_id, address=METER_ADDRESS, count=103)
        if regs is None:
            return False

        PhVphA = self._client.convert_from_registers(regs[6:7], data_type = self._client.DATATYPE.INT16)
        PhVphB = self._client.convert_from_registers(regs[7:8], data_type = self._client.DATATYPE.INT16)
        PhVphC = self._client.convert_from_registers(regs[8:9], data_type = self._client.DATATYPE.INT16)
        PPV = self._client.convert_from_registers(regs[9:10], data_type = self._client.DATATYPE.INT16)
        V_SF = self._client.convert_from_registers(regs[13:14], data_type = self._client.DATATYPE.INT16)

        Hz = self._client.convert_from_registers(regs[14:15], data_type = self._client.DATATYPE.INT16)
        Hz_SF = self._client.convert_from_registers(regs[15:16], data_type = self._client.DATATYPE.INT16)
        W = self._client.convert_from_registers(regs[16:17], data_type = self._client.DATATYPE.INT16)
        W_SF = self._client.convert_from_registers(regs[20:21], data_type = self._client.DATATYPE.INT16)

        TotWhExp = self._client.convert_from_registers(regs[36:38], data_type = self._client.DATATYPE.UINT32)
        TotWhImp = self._client.convert_from_registers(regs[44:46], data_type = self._client.DATATYPE.UINT32)
        TotWh_SF = self._client.convert_from_registers(regs[52:53], data_type = self._client.DATATYPE.INT16)

        acpower = self.calculate_value(W, W_SF, 2, -50000, 50000)
        m_frequency = self.calculate_value(Hz, Hz_SF, 2, 0, 100)

        self.data[meter_prefix + "PhVphA"] = self.calculate_value(PhVphA, V_SF,1,0,1000)
        self.data[meter_prefix + "PhVphB"] = self.calculate_value(PhVphB, V_SF,1,0,1000)
        self.data[meter_prefix + "PhVphC"] = self.calculate_value(PhVphC, V_SF,1,0,1000)
        self.data[meter_prefix + "PPV"] = self.calculate_value(PPV, V_SF,1,0,1000)
        self.data[meter_prefix + "exported"] = self.protect_lfte(meter_prefix + 'exported', self.calculate_value(TotWhExp, TotWh_SF))
        self.data[meter_prefix + "imported"] = self.protect_lfte(meter_prefix + 'imported', self.calculate_value(TotWhImp, TotWh_SF))
        self.data[meter_prefix + "line_frequency"] = m_frequency
        self.data[meter_prefix + "power"] = acpower

        if meter_prefix == 'm1_':
            inverter_acpower = self.data.get('acpower')
            if not acpower is None and not inverter_acpower is None:
                if self.is_numeric(acpower) and self.is_numeric(inverter_acpower):
                    self.data['load'] = round(acpower + inverter_acpower,2)
                elif not self.is_numeric(acpower):
                    _LOGGER.error(f'meter {meter_prefix} acpower not numeric {acpower}')
                elif not self.is_numeric(inverter_acpower):
                    _LOGGER.error(f'inverter acpower not numeric {inverter_acpower}')

            status_str = ""
            i_frequency = self.data["line_frequency"]
            #_LOGGER.debug(f'grid status m: {m_frequency} i: {i_frequency}')
            if not i_frequency is None and self.is_numeric(i_frequency) and not m_frequency is None and self.is_numeric(m_frequency):
                m_online = False
                if m_frequency and m_frequency > self._grid_frequency_lower_bound and m_frequency < self._grid_frequency_upper_bound:
                    m_online = True
                
                if m_online and i_frequency > self._grid_frequency_lower_bound and i_frequency < self._grid_frequency_upper_bound:
                    status_str = GRID_STATUS.get(3)
                elif not m_online and i_frequency > self._inverter_frequency_lower_bound and i_frequency < self._inverter_frequency_upper_bound:
                    status_str = GRID_STATUS.get(1)
                elif i_frequency < 1:
                    if m_online:
                        status_str = GRID_STATUS.get(2)
                    elif m_frequency < 1:
                        status_str = GRID_STATUS.get(0)
            if status_str is None:
                _LOGGER.error(f'Could not establish grid connection status m: {m_frequency} i: {i_frequency}')
                self.data["grid_status"] = None
            else:
                self.data["grid_status"] = status_str

        return True

    async def read_export_limit_data(self):
        """Read export limit control registers"""
        # Read export limit rate register (40232)
        rate_regs = await self.get_registers(unit_id=self._inverter_unit_id, address=EXPORT_LIMIT_RATE_ADDRESS, count=1)
        if rate_regs is not None:
            export_limit_rate_raw = self._client.convert_from_registers(rate_regs[0:1], data_type=self._client.DATATYPE.UINT16)
            self.data['export_limit_rate_raw'] = export_limit_rate_raw
            self.data['export_limit_rate_pct'] = self._export_limit_raw_to_percent(export_limit_rate_raw)
            self.data['export_limit_rate'] = self._export_limit_raw_to_watts(export_limit_rate_raw)
        else:
            self.data['export_limit_rate_raw'] = None
            self.data['export_limit_rate_pct'] = None
            self.data['export_limit_rate'] = None

        if time.monotonic() < self._export_limit_enable_mask_until:
            self.data['export_limit_enable'] = EXPORT_LIMIT_STATUS.get(1, 'Enabled')
            return True

        export_limit_enable_raw = await self._read_export_limit_enable_raw()
        if export_limit_enable_raw is None:
            self.data['export_limit_enable'] = None
        else:
            self.data['export_limit_enable'] = EXPORT_LIMIT_STATUS.get(export_limit_enable_raw, 'Unknown')
            if export_limit_enable_raw == 1:
                self._export_limit_enable_mask_until = 0.0

        return True

    async def set_storage_control_mode(self, mode: int):
        if not mode in [0,1,2,3]:
            _LOGGER.error(f'Attempted to set to unsupported storage control mode. Value: {mode}')
            return
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(3), payload=[mode])

    async def set_minimum_reserve(self, minimum_reserve: float):
        if minimum_reserve < 5:
            _LOGGER.error(f'Attempted to set minimum reserve below 5%. Value: {minimum_reserve}')
            return
        minimum_reserve = round(minimum_reserve * 100)
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(5), payload=[minimum_reserve])

    async def set_discharge_rate_w(self, discharge_rate_w):
        if discharge_rate_w > self.max_discharge_rate_w:
            discharge_rate = 100
        elif discharge_rate_w < self.max_discharge_rate_w * -1:
            discharge_rate = -100
        else:
            discharge_rate = discharge_rate_w / self.max_discharge_rate_w * 100
        await self.set_discharge_rate(discharge_rate)

    async def set_discharge_rate(self, discharge_rate):
        if discharge_rate < 0:
            discharge_rate = int(65536 + (discharge_rate * 100))
        else:
            discharge_rate = int(round(discharge_rate * 100))
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(10), payload=[discharge_rate])

    async def set_charge_rate_w(self, charge_rate_w):
        if charge_rate_w > self.max_charge_rate_w:
            charge_rate = 100
        elif charge_rate_w < self.max_charge_rate_w * -1:
            charge_rate = -100
        else:
            charge_rate = charge_rate_w / self.max_charge_rate_w * 100
        await self.set_charge_rate(charge_rate)

    async def set_grid_charge_power(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode == 4:
            await self.set_discharge_rate_w(value * -1)
            percent = (value / self.max_charge_rate_w) * 100 if self.max_charge_rate_w else 0
            self.data['grid_charge_power'] = percent
        else:
            return

    async def set_grid_discharge_power(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode == 5:
            await self.set_charge_rate_w(value * -1)
            percent = (value / self.max_discharge_rate_w) * 100 if self.max_discharge_rate_w else 0
            self.data['grid_discharge_power'] = percent
        else:
            return
        
    async def set_charge_limit(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode in [1, 3, 6]:
            await self.set_charge_rate_w(value)
            percent = (value / self.max_charge_rate_w) * 100 if self.max_charge_rate_w else 0
            self.data['charge_limit'] = percent
        elif self.storage_extended_control_mode in [4, 5, 7]:
            return
        elif self.storage_extended_control_mode in [0, 2]:
            return

    async def set_discharge_limit(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode in [2, 3, 7]:
            await self.set_discharge_rate_w(value)
            percent = (value / self.max_discharge_rate_w) * 100 if self.max_discharge_rate_w else 0
            self.data['discharge_limit'] = percent
        elif self.storage_extended_control_mode in [1, 4, 5, 6]:
            return
        elif self.storage_extended_control_mode in [0]:
            return

    async def set_charge_rate(self, charge_rate):
        if charge_rate < 0:
            charge_rate =  int(65536 + (charge_rate * 100))
        else:
            charge_rate = int(round(charge_rate * 100))
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(11), payload=[charge_rate])

    async def change_settings(self, mode, charge_limit, discharge_limit, grid_charge_power=0, grid_discharge_power=0, minimum_reserve=None):
        await self.set_storage_control_mode(mode)
        await self.set_charge_rate(charge_limit)
        await self.set_discharge_rate(discharge_limit)
        self.data['charge_limit'] = charge_limit
        if self.storage_extended_control_mode == 4:
            self.data['discharge_limit'] = 0
        else:
            self.data['discharge_limit'] = discharge_limit
        if self.storage_extended_control_mode == 5:
            self.data['charge_limit'] = 0
        else:
            self.data['charge_limit'] = charge_limit
        self.data['grid_charge_power'] = grid_charge_power
        self.data['grid_discharge_power'] = grid_discharge_power
        if not minimum_reserve is None:
            await self.set_minimum_reserve(minimum_reserve)
        
    async def restore_defaults(self):
        await self.change_settings(mode=0, charge_limit=100, discharge_limit=100, minimum_reserve=7)
        _LOGGER.info(f"restored defaults")

    async def set_auto_mode(self):
        await self.change_settings(mode=0, charge_limit=100, discharge_limit=100)
        self.storage_extended_control_mode = 0
        _LOGGER.info(f"Auto mode")

    async def set_charge_mode(self):
        await self.change_settings(mode=1, charge_limit=100, discharge_limit=100)
        self.storage_extended_control_mode = 1
        _LOGGER.info(f"Set charge mode")
  
    async def set_discharge_mode(self):
        await self.change_settings(mode=2, charge_limit=100, discharge_limit=100)
        self.storage_extended_control_mode = 2
        _LOGGER.info(f"Set discharge mode")

    async def set_charge_discharge_mode(self):
        await self.change_settings(mode=3, charge_limit=100, discharge_limit=100)
        self.storage_extended_control_mode = 3
        _LOGGER.info(f"Set charge/discharge mode.")

    async def set_grid_charge_mode(self):
        # Keep previous grid_charge_power if available, otherwise default to 0
        grid_charge_power = self.data.get('grid_charge_power', 0)

        await self.change_settings(
            mode=2,                # Charge
            charge_limit=100,      # allow charging up to 100%
            discharge_limit=0,     # no discharging in this mode
            grid_charge_power=grid_charge_power,
        )
        self.storage_extended_control_mode = 4
        _LOGGER.info(f"Charge from grid enabled, target {grid_charge_power} W")


    async def set_grid_discharge_mode(self):
        # Keep previous grid_discharge_power if available, otherwise default to 0
        grid_discharge_power = self.data.get('grid_discharge_power', 0)

        await self.change_settings(
            mode=1,                # Discharge
            charge_limit=0,        # no charging in this mode
            discharge_limit=100,   # allow discharging up to 100%
            grid_discharge_power=grid_discharge_power,
        )
        self.storage_extended_control_mode = 5
        _LOGGER.info(
            f"Discharge to grid enabled, target {grid_discharge_power} W"
        )

    async def set_block_discharge_mode(self):
        charge_rate = 100
        await self.change_settings(mode=3, charge_limit=charge_rate, discharge_limit=0)
        self.storage_extended_control_mode = 6
        _LOGGER.info(f"blocked discharging")

    async def set_block_charge_mode(self):
        discharge_rate = 100
        await self.change_settings(mode=3, charge_limit=0, discharge_limit=discharge_rate)
        self.storage_extended_control_mode = 7
        _LOGGER.info(f"Block charging at {discharge_rate}")

    async def set_calibrate_mode(self):
        await self.change_settings(mode=2, charge_limit=100, discharge_limit=-100, grid_charge_power=100)
        self.storage_extended_control_mode = 8
        _LOGGER.info(f"Auto mode")


    async def set_export_limit_rate(self, rate):
        """Set export limit rate in watts and write WMaxLimPct raw value."""
        raw_rate = self._export_limit_watts_to_raw(rate)
        if raw_rate is None:
            _LOGGER.error("Cannot set export limit rate, missing max power or scale factor")
            return

        export_limit_enable_raw = await self._read_export_limit_enable_raw()
        was_enabled = export_limit_enable_raw == 1

        if was_enabled:
            self._export_limit_enable_mask_until = time.monotonic() + 0.6
            await self.write_registers(
                unit_id=self._inverter_unit_id,
                address=EXPORT_LIMIT_ENABLE_ADDRESS,
                payload=[0],
            )

        await self.write_registers(unit_id=self._inverter_unit_id, address=EXPORT_LIMIT_RATE_ADDRESS, payload=[int(raw_rate)])

        if was_enabled:
            applied = False
            for _ in range(6):
                rate_regs = await self.get_registers(
                    unit_id=self._inverter_unit_id,
                    address=EXPORT_LIMIT_RATE_ADDRESS,
                    count=1,
                )
                if rate_regs is not None:
                    readback_raw = self._client.convert_from_registers(
                        rate_regs[0:1],
                        data_type=self._client.DATATYPE.UINT16,
                    )
                    if self.is_numeric(readback_raw) and int(readback_raw) == int(raw_rate):
                        applied = True
                        break
                await asyncio.sleep(0.05)

            await self.write_registers(
                unit_id=self._inverter_unit_id,
                address=EXPORT_LIMIT_ENABLE_ADDRESS,
                payload=[1],
            )
            self.data['export_limit_enable'] = EXPORT_LIMIT_STATUS.get(1, 'Enabled')
            enable_after_raw = await self._read_export_limit_enable_raw()
            if enable_after_raw == 1:
                self._export_limit_enable_mask_until = 0.0
        elif export_limit_enable_raw is not None:
            self._export_limit_enable_mask_until = 0.0
            self.data['export_limit_enable'] = EXPORT_LIMIT_STATUS.get(export_limit_enable_raw, 'Unknown')
        else:
            self._export_limit_enable_mask_until = 0.0

        self.data['export_limit_rate_raw'] = raw_rate
        self.data['export_limit_rate_pct'] = self._export_limit_raw_to_percent(raw_rate)
        self.data['export_limit_rate'] = self._export_limit_raw_to_watts(raw_rate)
        _LOGGER.info(
            "Set export limit rate to %s W (raw=%s, enable_before=%s, pulsed_enable=%s, applied=%s)",
            self.data['export_limit_rate'],
            raw_rate,
            export_limit_enable_raw,
            was_enabled,
            applied if was_enabled else None,
        )

    async def set_export_limit_enable(self, enable):
        """Enable/disable export limit (0=Disabled, 1=Enabled)"""
        enable_value = 1 if enable else 0
        await self.write_registers(unit_id=self._inverter_unit_id, address=EXPORT_LIMIT_ENABLE_ADDRESS, payload=[enable_value])
        self.data['export_limit_enable'] = EXPORT_LIMIT_STATUS.get(enable_value, 'Unknown')
        _LOGGER.info(f"Set export limit enable to {enable_value}")

    async def set_conn_status(self, enable):
        """Enable/disable inverter connection (0=Disconnected/Standby, 1=Connected/Normal)"""
        conn_value = 1 if enable else 0
        await self.write_registers(unit_id=self._inverter_unit_id, address=CONN_ADDRESS, payload=[conn_value])
        self.data['Conn'] = CONTROL_STATUS[conn_value]
        _LOGGER.info(f"Set inverter connection status to {conn_value} ({'Connected' if enable else 'Disconnected/Standby'})")
