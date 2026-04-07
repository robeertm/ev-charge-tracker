"""Kia UVO / Hyundai Bluelink connector via hyundai-kia-connect-api.

Since 2025, Kia/Hyundai EU requires a refresh_token instead of a password
(reCAPTCHA blocks automated login). The token is obtained once via a
browser-based OAuth flow and then passed as the 'password' parameter.
"""
import logging

try:
    from hyundai_kia_connect_api import VehicleManager, Vehicle
    HAS_HYUNDAI_KIA = True
except ImportError:
    HAS_HYUNDAI_KIA = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

REGIONS = {
    'EU': 1,   # Europe
    'US': 2,   # USA
    'CA': 3,   # Canada
    'KR': 4,   # Korea
}

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Kia Connect / Bluelink Account)", "type": "text"},
    {"key": "password", "label": "Refresh-Token", "type": "password",
     "help": "Kein Passwort! Token über Browser-Login holen (siehe Anleitung unten)."},
    {"key": "pin", "label": "PIN (4-stellig aus Kia Connect App)", "type": "password"},
    {"key": "region", "label": "Region", "type": "select",
     "options": [{"value": "EU", "label": "Europa"},
                 {"value": "US", "label": "USA"},
                 {"value": "CA", "label": "Kanada"},
                 {"value": "KR", "label": "Korea"}]},
]


class _HyundaiKiaBase(VehicleConnector):
    """Shared logic for Kia and Hyundai (token-based auth)."""

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
                password=self.credentials['password'],  # This is the refresh_token
                pin=self.credentials.get('pin', ''),
            )
        return self._manager

    def _ensure_auth(self):
        """Refresh the access token using the stored refresh_token."""
        mgr = self._get_manager()
        return mgr.check_and_refresh_token()

    def _get_vehicle(self) -> 'Vehicle':
        if self._vehicle is None:
            mgr = self._get_manager()
            if not mgr.vehicles:
                mgr.update_all_vehicles_with_cached_state()
            if not mgr.vehicles:
                raise RuntimeError("Kein Fahrzeug im Account gefunden")
            self._vehicle = list(mgr.vehicles.values())[0]
        return self._vehicle

    def authenticate(self) -> bool:
        try:
            self._ensure_auth()
            self._get_vehicle()
            return True
        except Exception as e:
            logger.error(f"Auth failed: {e}")
            self._manager = None
            self._vehicle = None
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self) -> VehicleStatus:
        self._ensure_auth()
        mgr = self._get_manager()
        vehicle = self._get_vehicle()
        mgr.update_vehicle_with_cached_state(vehicle.id)

        return VehicleStatus(
            soc_percent=vehicle.ev_battery_percentage,
            odometer_km=int(vehicle.odometer) if vehicle.odometer else None,
            is_charging=vehicle.ev_battery_is_charging or False,
            charge_power_kw=getattr(vehicle, 'ev_charging_power', None),
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
