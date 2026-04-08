"""Polestar connector via pypolestar."""
import asyncio
import logging

try:
    from pypolestar import PolestarApi
    HAS_POLESTAR = True
except ImportError:
    HAS_POLESTAR = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Polestar ID)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "vin", "label": "VIN (optional, bei mehreren Fahrzeugen)", "type": "text"},
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


class PolestarConnector(VehicleConnector):

    async def _fetch(self, force=False):
        api = PolestarApi(
            username=self.credentials['username'],
            password=self.credentials['password'],
        )
        await api.async_init()

        vin = self.credentials.get('vin', '').strip()
        if not vin:
            vins = list(api.vehicles.keys()) if api.vehicles else []
            if not vins:
                raise RuntimeError("Kein Polestar-Fahrzeug gefunden")
            vin = vins[0]

        data = api.get_battery_data(vin)
        odo_data = api.get_odometer_data(vin)

        return VehicleStatus(
            soc_percent=int(data.battery_charge_level_percentage) if data and data.battery_charge_level_percentage is not None else None,
            odometer_km=int(odo_data.odometer_meters / 1000) if odo_data and odo_data.odometer_meters else None,
            is_charging=data.charging_status == 'CHARGING' if data and data.charging_status else False,
            is_plugged_in=data.charger_connection_status == 'CHARGER_CONNECTION_STATUS_CONNECTED' if data and data.charger_connection_status else False,
            estimated_range_km=int(data.estimated_charging_time_minutes_to_target_distance) if data and hasattr(data, 'estimated_distance_to_empty_km') and data.estimated_distance_to_empty_km else None,
            charge_limit_ac=int(data.charging_current_limit_amps) if data and hasattr(data, 'charging_current_limit_amps') and data.charging_current_limit_amps else None,
            last_updated=str(data.event_updated_timestamp) if data and hasattr(data, 'event_updated_timestamp') else None,
            vehicle_model=f"Polestar",
            raw_data={'vin': vin},
        )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"Polestar auth failed: {e}")
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
        return "Polestar"


if HAS_POLESTAR:
    register('polestar', PolestarConnector)
