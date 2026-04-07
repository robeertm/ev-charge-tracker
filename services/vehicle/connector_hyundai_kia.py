"""Kia UVO / Hyundai Bluelink connector via hyundai-kia-connect-api."""
try:
    from hyundai_kia_connect_api import VehicleManager, Vehicle
    HAS_HYUNDAI_KIA = True
except ImportError:
    HAS_HYUNDAI_KIA = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

REGIONS = {
    'EU': 1,   # Europe
    'US': 2,   # USA
    'CA': 3,   # Canada
    'KR': 4,   # Korea
}

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail / Benutzername", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "pin", "label": "PIN", "type": "password"},
    {"key": "region", "label": "Region", "type": "select",
     "options": [{"value": "EU", "label": "Europa"},
                 {"value": "US", "label": "USA"},
                 {"value": "CA", "label": "Kanada"},
                 {"value": "KR", "label": "Korea"}]},
]


class _HyundaiKiaBase(VehicleConnector):
    """Shared logic for Kia and Hyundai."""

    BRAND_ID = None  # override in subclass

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._manager = None
        self._vehicle = None

    def _get_manager(self) -> 'VehicleManager':
        if self._manager is None:
            region = REGIONS.get(self.credentials.get('region', 'EU'), 1)
            self._manager = VehicleManager(
                region=region,
                brand=self.BRAND_ID,
                username=self.credentials['username'],
                password=self.credentials['password'],
                pin=self.credentials.get('pin', ''),
            )
        return self._manager

    def _get_vehicle(self) -> 'Vehicle':
        if self._vehicle is None:
            mgr = self._get_manager()
            mgr.check_and_refresh_token()
            if not mgr.vehicles:
                raise RuntimeError("Kein Fahrzeug im Account gefunden")
            self._vehicle = list(mgr.vehicles.values())[0]
        return self._vehicle

    def authenticate(self) -> bool:
        try:
            self._get_vehicle()
            return True
        except Exception:
            self._manager = None
            self._vehicle = None
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self) -> VehicleStatus:
        mgr = self._get_manager()
        mgr.check_and_refresh_token()
        vehicle = self._get_vehicle()
        mgr.update_vehicle_with_cached_state(vehicle.id)

        return VehicleStatus(
            soc_percent=vehicle.ev_battery_percentage,
            odometer_km=int(vehicle.odometer) if vehicle.odometer else None,
            is_charging=vehicle.ev_battery_is_charging or False,
            charge_power_kw=None,  # not available in this API
            estimated_range_km=int(vehicle.ev_driving_range) if vehicle.ev_driving_range else None,
            raw_data={
                'vin': vehicle.VIN,
                'name': vehicle.name,
                'ev_battery_percentage': vehicle.ev_battery_percentage,
                'odometer': vehicle.odometer,
                'ev_battery_is_charging': vehicle.ev_battery_is_charging,
                'ev_driving_range': vehicle.ev_driving_range,
            },
        )

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS


class KiaConnector(_HyundaiKiaBase):
    BRAND_ID = 1

    @staticmethod
    def brand_name() -> str:
        return "Kia (UVO)"


class HyundaiConnector(_HyundaiKiaBase):
    BRAND_ID = 2

    @staticmethod
    def brand_name() -> str:
        return "Hyundai (Bluelink)"


# Register if dependency is installed
if HAS_HYUNDAI_KIA:
    register('kia', KiaConnector)
    register('hyundai', HyundaiConnector)
