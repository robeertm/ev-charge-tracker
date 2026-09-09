"""Smart #1 / #3 connector via pySmartHashtag."""
import asyncio
import logging

# Upstream moved: `SmartApi` was dropped and `SmartAccount` left
# `pysmarthashtag.models` for `pysmarthashtag.account`. Both imports here
# raised ImportError against every published version of the package, so
# HAS_SMART was permanently False — the brand could never register, the
# wizard tile said "Paket nötig" for good, and the install button
# installed a package that changed nothing. Verified against
# pySmartHashtag 0.12.2.
try:
    from pysmarthashtag.account import SmartAccount
    HAS_SMART = True
except ImportError:
    HAS_SMART = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Hello Smart)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "vin", "label": "VIN (optional, bei mehreren Fahrzeugen)", "type": "text"},
]


def _num(value):
    """Unwrap a pysmarthashtag ``ValueWithUnit`` (or a plain number) to int.

    Nearly every measurement upstream is a ``ValueWithUnit`` namedtuple,
    not a bare float — ``int(vehicle.odometer)`` raised TypeError.
    """
    if value is None:
        return None
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


class SmartConnector(VehicleConnector):

    async def _fetch(self, force=False):
        account = SmartAccount(
            username=self.credentials['username'],
            password=self.credentials['password'],
        )
        # get_vehicles() fills account.vehicles in place and returns None;
        # get_vehicle_information(vin) is what actually pulls the state.
        await account.get_vehicles()
        vehicles = account.vehicles or {}
        if not vehicles:
            raise RuntimeError("Kein Smart-Fahrzeug gefunden")

        wanted = (self.credentials.get('vin') or '').strip()
        vin = wanted if wanted in vehicles else list(vehicles.keys())[0]
        await account.get_vehicle_information(vin)
        vehicle = account.vehicles[vin]

        soc = None
        range_km = None
        is_charging = False
        odometer = None

        bat = getattr(vehicle, 'battery', None)
        if bat is not None:
            soc = _num(getattr(bat, 'remaining_battery_percent', None))
            range_km = _num(getattr(bat, 'remaining_range', None))
            status = getattr(bat, 'charging_status', None)
            # Upstream reports an enum on some firmwares and a plain
            # string on others; compare on the name either way.
            is_charging = 'CHARGING' in str(getattr(status, 'name', status) or '').upper()

        odometer = _num(getattr(vehicle, 'odometer', None))

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
