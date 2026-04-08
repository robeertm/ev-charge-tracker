"""MG / SAIC connector via saic-ismart-client-ng."""
import asyncio
import logging

try:
    from saic_ismart_client_ng import SaicApi
    from saic_ismart_client_ng.model import SaicApiConfiguration
    HAS_SAIC = True
except ImportError:
    HAS_SAIC = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail / Telefon (MG iSMART)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "vin", "label": "VIN (optional)", "type": "text"},
    {"key": "region", "label": "Region", "type": "select",
     "options": [
         {"value": "eu", "label": "Europa"},
         {"value": "cn", "label": "China"},
     ]},
]

REGION_URLS = {
    'eu': 'https://gateway-eu.saic-ismart.com',
    'cn': 'https://gateway-cn.saic-ismart.com',
}


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=60)
    except RuntimeError:
        pass
    return asyncio.run(coro)


class MGConnector(VehicleConnector):

    async def _fetch(self, force=False):
        region = self.credentials.get('region', 'eu')
        base_url = REGION_URLS.get(region, REGION_URLS['eu'])
        config = SaicApiConfiguration(
            username=self.credentials['username'],
            password=self.credentials['password'],
            base_uri=base_url,
        )
        api = SaicApi(config)
        await api.login()
        vehicle_list = await api.vehicle_list()
        if not vehicle_list:
            raise RuntimeError("Kein MG-Fahrzeug gefunden")

        vin = self.credentials.get('vin', '').strip()
        vehicle = None
        for v in vehicle_list:
            if vin and v.vin == vin:
                vehicle = v
                break
        if not vehicle:
            vehicle = vehicle_list[0]

        status = await api.get_vehicle_status(vehicle.vin)
        charging = await api.get_vehicle_charging_status(vehicle.vin) if hasattr(api, 'get_vehicle_charging_status') else None

        soc = None
        range_km = None
        is_charging = False
        if hasattr(status, 'basic_vehicle_status'):
            bvs = status.basic_vehicle_status
            soc = bvs.battery_voltage if hasattr(bvs, 'battery_voltage') else None
            if hasattr(bvs, 'fuel_range_elec'):
                range_km = int(bvs.fuel_range_elec / 10) if bvs.fuel_range_elec else None

        if charging:
            if hasattr(charging, 'charging_status'):
                is_charging = charging.charging_status == 1
            if hasattr(charging, 'real_time_power'):
                pass  # charge_power available

        odometer = None
        if hasattr(status, 'basic_vehicle_status') and hasattr(status.basic_vehicle_status, 'mileage'):
            odometer = int(status.basic_vehicle_status.mileage / 10) if status.basic_vehicle_status.mileage else None

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=odometer,
            is_charging=is_charging,
            estimated_range_km=range_km,
            vehicle_name=vehicle.model_name if hasattr(vehicle, 'model_name') else 'MG',
            vehicle_model=vehicle.model_name if hasattr(vehicle, 'model_name') else 'MG',
            raw_data={'vin': vehicle.vin},
        )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"MG auth failed: {e}")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self, force=False) -> VehicleStatus:
        return _run_async(self._fetch(force))

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return "MG (iSMART)"


if HAS_SAIC:
    register('mg', MGConnector)
