"""Smart #1 / #3 connector via pySmartHashtag."""
import asyncio
import logging

try:
    from pysmarthashtag.api import SmartApi
    from pysmarthashtag.models import SmartAccount
    HAS_SMART = True
except ImportError:
    HAS_SMART = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Hello Smart)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
]


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(coro)


class SmartConnector(VehicleConnector):

    async def _fetch(self, force=False):
        account = SmartAccount(
            username=self.credentials['username'],
            password=self.credentials['password'],
        )
        api = SmartApi(account)
        await api.async_update()

        vehicles = account.vehicles
        if not vehicles:
            raise RuntimeError("Kein Smart-Fahrzeug gefunden")

        vin = list(vehicles.keys())[0]
        vehicle = vehicles[vin]

        soc = None
        range_km = None
        is_charging = False
        odometer = None

        if hasattr(vehicle, 'battery'):
            bat = vehicle.battery
            soc = int(bat.soc) if hasattr(bat, 'soc') and bat.soc is not None else None
            range_km = int(bat.range) if hasattr(bat, 'range') and bat.range is not None else None
            is_charging = bat.charging_status == 'CHARGING' if hasattr(bat, 'charging_status') else False

        if hasattr(vehicle, 'odometer') and vehicle.odometer is not None:
            odometer = int(vehicle.odometer)

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=odometer,
            is_charging=is_charging,
            estimated_range_km=range_km,
            vehicle_name=f"Smart {vehicle.model}" if hasattr(vehicle, 'model') else 'Smart',
            vehicle_model=f"Smart {vehicle.model}" if hasattr(vehicle, 'model') else 'Smart',
            raw_data={'vin': vin},
        )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"Smart auth failed: {e}")
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
        return "Smart (#1 / #3)"


if HAS_SMART:
    register('smart', SmartConnector)
