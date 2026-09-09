"""Porsche connector via pyporscheconnectapi."""
import asyncio
import logging

# Upstream moved: there is no `pyporscheconnectapi.client` module any
# more and `connection` no longer exports `PorscheConnect`. Both imports
# raised ImportError against every published version, so HAS_PORSCHE was
# permanently False — the brand could never register and the wizard tile
# stayed on "Paket nötig" no matter how often the package was installed.
# Verified against pyporscheconnectapi 0.2.8.
try:
    from pyporscheconnectapi.account import PorscheConnectAccount
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


def _num(value):
    """Unwrap whatever shape a measurement arrives in, to int or None.

    Upstream hands back a plain number for some fields and a
    ``{'value': …, 'unit': …}`` dict or small object for others.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get('value')
    inner = getattr(value, 'value', value)
    try:
        return int(round(float(inner)))
    except (TypeError, ValueError):
        return None


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
        account = PorscheConnectAccount(
            username=self.credentials['username'],
            password=self.credentials['password'],
        )
        wanted = (self.credentials.get('vin') or '').strip()
        vehicle = None
        if wanted:
            vehicle = await account.get_vehicle(wanted)
        if vehicle is None:
            vehicles = await account.get_vehicles()
            if not vehicles:
                raise RuntimeError("Kein Porsche-Fahrzeug gefunden")
            vehicle = vehicles[0]

        # The vehicle object is empty until an overview is pulled into it;
        # the stored one is the cached snapshot and does not wake the car.
        await vehicle.get_stored_overview()

        soc = _num(getattr(vehicle, 'main_battery_level', None))
        range_km = _num(getattr(vehicle, 'remaining_range', None))
        if range_km is None:
            range_km = _num(getattr(vehicle, 'electric_range', None))
        odometer = _num(getattr(vehicle, 'mileage', None))
        is_charging = bool(getattr(vehicle, 'direct_charge_on', False)) or \
            'CHARGING' in str(getattr(vehicle, 'charging_state', '') or '').upper()

        model = getattr(vehicle, 'model_name', None) or 'Porsche'
        v_vin = getattr(vehicle, 'vin', '') or wanted

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=odometer,
            is_charging=is_charging,
            estimated_range_km=range_km,
            vehicle_name=str(model),
            vehicle_model=str(model),
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
