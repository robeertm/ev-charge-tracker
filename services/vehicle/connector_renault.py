"""Renault / Dacia connector via renault-api."""
import asyncio
import logging

try:
    from renault_api.renault_client import RenaultClient
    from aiohttp import ClientSession
    HAS_RENAULT = True
except ImportError:
    HAS_RENAULT = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (MyRenault / MyDacia)", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "locale", "label": "Region", "type": "select",
     "options": [
         {"value": "de_DE", "label": "Deutschland"},
         {"value": "at_AT", "label": "Oesterreich"},
         {"value": "fr_FR", "label": "Frankreich"},
         {"value": "it_IT", "label": "Italien"},
         {"value": "es_ES", "label": "Spanien"},
         {"value": "nl_NL", "label": "Niederlande"},
         {"value": "en_GB", "label": "UK"},
     ]},
]


def _run_async(coro):
    """Run async code from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(coro)


class _RenaultBase(VehicleConnector):
    BRAND = None  # 'renault' or 'dacia'

    async def _fetch(self, force=False):
        locale = self.credentials.get('locale', 'de_DE')
        async with ClientSession() as session:
            client = RenaultClient(session=session, locale=locale)
            await client.session.login(self.credentials['username'], self.credentials['password'])
            person = await client.get_person()
            accounts = person.accounts
            # Find Kamereon account
            account = None
            for a in accounts:
                if a.account_type == 'MYRENAULT':
                    account = await client.get_api_account(a.account_id)
                    break
            if not account:
                for a in accounts:
                    account = await client.get_api_account(a.account_id)
                    break
            if not account:
                raise RuntimeError("Kein Renault/Dacia-Account gefunden")

            vehicles = await account.get_vehicles()
            if not vehicles.vehicleLinks:
                raise RuntimeError("Kein Fahrzeug im Account gefunden")
            vin = vehicles.vehicleLinks[0].vin
            vehicle = await account.get_api_vehicle(vin)

            battery = await vehicle.get_battery_status()
            cockpit = None
            location = None
            try:
                cockpit = await vehicle.get_cockpit()
            except Exception:
                pass
            try:
                location = await vehicle.get_location()
            except Exception:
                pass

            soc = battery.batteryLevel if battery else None
            is_charging = battery.chargingStatus is not None and battery.chargingStatus > 0 if battery else False
            is_plugged = battery.plugStatus is not None and battery.plugStatus > 0 if battery else False
            range_km = battery.batteryAutonomy if battery else None
            odometer = int(cockpit.totalMileage) if cockpit and cockpit.totalMileage else None

            return VehicleStatus(
                soc_percent=soc,
                odometer_km=odometer,
                is_charging=is_charging,
                is_plugged_in=is_plugged,
                estimated_range_km=range_km,
                location_lat=location.gpsLatitude if location else None,
                location_lon=location.gpsLongitude if location else None,
                last_updated=str(battery.timestamp) if battery and battery.timestamp else None,
                vehicle_name=vehicles.vehicleLinks[0].vehicleDetails.model if vehicles.vehicleLinks[0].vehicleDetails else None,
                vehicle_model=vehicles.vehicleLinks[0].vehicleDetails.model if vehicles.vehicleLinks[0].vehicleDetails else None,
                raw_data={'vin': vin},
            )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"Renault auth failed: {e}")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self, force=False) -> VehicleStatus:
        return _run_async(self._fetch(force))

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS


class RenaultConnector(_RenaultBase):
    BRAND = 'renault'

    @staticmethod
    def brand_name() -> str:
        return "Renault (MyRenault)"


class DaciaConnector(_RenaultBase):
    BRAND = 'dacia'

    @staticmethod
    def brand_name() -> str:
        return "Dacia (MyDacia)"


if HAS_RENAULT:
    register('renault', RenaultConnector)
    register('dacia', DaciaConnector)
