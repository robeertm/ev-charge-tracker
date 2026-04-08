"""Porsche connector via pyporscheconnectapi."""
import asyncio
import logging

try:
    from pyporscheconnectapi.client import PorscheConnectApi
    from pyporscheconnectapi.connection import PorscheConnect
    HAS_PORSCHE = True
except ImportError:
    HAS_PORSCHE = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Porsche ID)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "vin", "label": "VIN (optional)", "type": "text"},
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


class PorscheConnector(VehicleConnector):

    async def _fetch(self, force=False):
        conn = PorscheConnect(
            email=self.credentials['username'],
            password=self.credentials['password'],
        )
        api = PorscheConnectApi(conn)
        vehicles = await conn.getVehicles()
        if not vehicles:
            raise RuntimeError("Kein Porsche-Fahrzeug gefunden")

        vin = self.credentials.get('vin', '').strip()
        vehicle = None
        for v in vehicles:
            if vin and v.get('vin') == vin:
                vehicle = v
                break
        if not vehicle:
            vehicle = vehicles[0]

        v_vin = vehicle.get('vin', '')
        stored = await api.getStoredOverview(v_vin)
        emob = stored.get('batteryLevel', {})

        soc = emob.get('value')
        range_km = stored.get('remainingRanges', {}).get('electricalRange', {}).get('distance', {}).get('value')
        is_charging = emob.get('chargingState') == 'CHARGING' if emob.get('chargingState') else False
        odometer = stored.get('mileage', {}).get('value')

        await conn.close()

        return VehicleStatus(
            soc_percent=int(soc) if soc is not None else None,
            odometer_km=int(odometer) if odometer is not None else None,
            is_charging=is_charging,
            estimated_range_km=int(range_km) if range_km is not None else None,
            vehicle_name=vehicle.get('modelDescription', 'Porsche'),
            vehicle_model=vehicle.get('modelDescription', 'Porsche'),
            raw_data={'vin': v_vin},
        )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"Porsche auth failed: {e}")
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
        return "Porsche (Connect)"


if HAS_PORSCHE:
    register('porsche', PorscheConnector)
