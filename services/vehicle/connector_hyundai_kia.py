"""Kia UVO / Hyundai Bluelink connector via hyundai-kia-connect-api.

Since 2025/2026, **both** Kia EU and Hyundai EU require a refresh_token
instead of a password — reCAPTCHA blocks direct automated login. The
user obtains the token once via a browser-based OAuth flow
(`token_fetch.py`) and stores it in the password field.

The two brands use *different* OAuth flows under the hood (Kia uses the
oneid flow on kia.com, Hyundai uses the CTB flow on
ctbapi.hyundai-europe.com with a real client_secret and different
authorize query params). See `token_fetch.py` for the per-brand config.
At the API-call level though, both end up calling
`hyundai_kia_connect_api.VehicleManager` with the refresh_token in the
password parameter.
"""
import logging
from datetime import datetime, date as _date

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

_REGION_FIELD = {
    "key": "region", "label": "Region", "type": "select",
    "options": [{"value": "EU", "label": "Europa"},
                {"value": "US", "label": "USA"},
                {"value": "CA", "label": "Kanada"},
                {"value": "KR", "label": "Korea"}],
}

# Kia: refresh-token flow (password login blocked by reCAPTCHA since 2025)
KIA_CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Kia Connect Account)", "type": "text"},
    {"key": "password", "label": "Refresh-Token", "type": "password",
     "help": "Kein Passwort! Token über Browser-Login holen (siehe Anleitung unten)."},
    {"key": "pin", "label": "PIN (4-stellig aus Kia Connect App)", "type": "password"},
    _REGION_FIELD,
]

# Hyundai: same refresh-token flow as Kia (different OAuth URLs under
# the hood — see token_fetch.py BRAND_CONFIG['hyundai']).
HYUNDAI_CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Bluelink Account)", "type": "text"},
    {"key": "password", "label": "Refresh-Token", "type": "password",
     "help": "Kein Passwort! Token über Browser-Login holen (siehe Anleitung unten)."},
    {"key": "pin", "label": "PIN (4-stellig aus Bluelink App)", "type": "password"},
    _REGION_FIELD,
]


_managers = {}  # Cache VehicleManager instances across requests


def _dump_vehicle(vehicle):
    """Introspect a Vehicle object and return a dict of all JSON-safe
    public attributes. Used to populate VehicleStatus.raw_data so the
    /vehicle/raw viewer can surface every field the SDK exposes — not
    just the subset our normalized VehicleStatus cherry-picks.

    Safety: we only emit primitive types (plus datetime → ISO string).
    Anything else is stringified with a length cap to prevent runaway
    memory use from deeply nested objects.
    """
    out = {}
    for key in sorted(dir(vehicle)):
        if key.startswith('_'):
            continue
        try:
            val = getattr(vehicle, key)
        except Exception as e:
            out[key] = f'<error: {type(e).__name__}: {e}>'
            continue
        if callable(val):
            continue
        if val is None or isinstance(val, (str, int, float, bool)):
            out[key] = val
        elif isinstance(val, (datetime, _date)):
            out[key] = val.isoformat()
        elif isinstance(val, (list, tuple, dict)):
            try:
                import json as _j
                # Round-trip through json with default=str so the stored
                # value is **truly** JSON-safe. A bare `dumps(val,
                # default=str)` check is misleading: default=str silently
                # stringifies any unknown object (e.g. DailyDrivingStats
                # from hyundai_kia_connect_api), so the check passes —
                # but if we then keep the original val, a later naive
                # `json.dumps(raw_data)` without default would crash.
                out[key] = _j.loads(_j.dumps(val, default=str))
            except (TypeError, ValueError):
                out[key] = str(val)[:500]
        else:
            out[key] = str(val)[:500]
    return out


class _HyundaiKiaBase(VehicleConnector):
    """Shared logic for Kia and Hyundai (token-based auth)."""

    BRAND_ID = None  # override in subclass

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._vehicle = None

    @property
    def _cache_key(self):
        return f"{self.BRAND_ID}:{self.credentials.get('username', '')}"

    def _get_manager(self) -> 'VehicleManager':
        key = self._cache_key
        if key not in _managers:
            region = REGIONS.get(self.credentials.get('region', 'EU'), 1)
            _managers[key] = VehicleManager(
                region=region,
                brand=self.BRAND_ID,
                username=self.credentials['username'],
                password=self.credentials['password'],  # This is the refresh_token
                pin=self.credentials.get('pin', ''),
            )
        return _managers[key]

    def _ensure_auth(self):
        """Refresh the access token using the stored refresh_token."""
        mgr = self._get_manager()
        try:
            return mgr.check_and_refresh_token()
        except Exception as e:
            # If token refresh fails, clear cache and retry once
            logger.warning(f"Token refresh failed, retrying: {e}")
            _managers.pop(self._cache_key, None)
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
            _managers.pop(self._cache_key, None)
            self._vehicle = None
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self, force=False) -> VehicleStatus:
        self._ensure_auth()
        mgr = self._get_manager()
        vehicle = self._get_vehicle()
        if force:
            # Save cached values before force refresh (some may be missing after wake)
            cached_odometer = vehicle.odometer
            cached_range = vehicle.ev_driving_range
            cached_12v = vehicle.car_battery_percentage
            mgr.force_refresh_vehicle_state(vehicle.id)
            # Restore missing values from cache
            if vehicle.odometer is None and cached_odometer is not None:
                vehicle.odometer = cached_odometer
            if vehicle.ev_driving_range is None and cached_range is not None:
                vehicle.ev_driving_range = cached_range
            if vehicle.car_battery_percentage is None and cached_12v is not None:
                vehicle.car_battery_percentage = cached_12v
        else:
            mgr.update_vehicle_with_cached_state(vehicle.id)

        return VehicleStatus(
            soc_percent=vehicle.ev_battery_percentage,
            odometer_km=int(vehicle.odometer) if vehicle.odometer else None,
            is_charging=vehicle.ev_battery_is_charging or False,
            is_plugged_in=bool(vehicle.ev_battery_is_plugged_in),
            is_locked=vehicle.is_locked if vehicle.is_locked is not None else True,
            charge_power_kw=vehicle.ev_charging_power,
            estimated_range_km=int(vehicle.ev_driving_range) if vehicle.ev_driving_range else None,
            battery_12v_percent=vehicle.car_battery_percentage,
            battery_soh_percent=vehicle.ev_battery_soh_percentage,
            charge_limit_ac=vehicle.ev_charge_limits_ac,
            charge_limit_dc=vehicle.ev_charge_limits_dc,
            est_charge_duration_min=vehicle.ev_estimated_current_charge_duration,
            est_fast_charge_duration_min=vehicle.ev_estimated_fast_charge_duration,
            climate_temp=vehicle.air_temperature,
            climate_on=vehicle.air_control_is_on or False,
            total_power_consumed_kwh=vehicle.total_power_consumed,
            total_power_regenerated_kwh=vehicle.total_power_regenerated,
            location_lat=vehicle.location_latitude,
            location_lon=vehicle.location_longitude,
            last_updated=str(vehicle.last_updated_at) if vehicle.last_updated_at else None,
            vehicle_name=vehicle.name,
            vehicle_model=vehicle.model,
            front_left_door_open=bool(vehicle.front_left_door_is_open),
            front_right_door_open=bool(vehicle.front_right_door_is_open),
            back_left_door_open=bool(vehicle.back_left_door_is_open),
            back_right_door_open=bool(vehicle.back_right_door_is_open),
            trunk_open=bool(vehicle.trunk_is_open) if vehicle.trunk_is_open is not None else False,
            hood_open=bool(vehicle.hood_is_open) if vehicle.hood_is_open is not None else False,
            tire_warn_all=bool(vehicle.tire_pressure_all_warning_is_on),
            tire_warn_fl=bool(vehicle.tire_pressure_front_left_warning_is_on),
            tire_warn_fr=bool(vehicle.tire_pressure_front_right_warning_is_on),
            tire_warn_rl=bool(vehicle.tire_pressure_rear_left_warning_is_on),
            tire_warn_rr=bool(vehicle.tire_pressure_rear_right_warning_is_on),
            steering_wheel_heater=bool(vehicle.steering_wheel_heater_is_on),
            rear_window_heater=bool(vehicle.back_window_heater_is_on),
            defrost=bool(vehicle.defrost_is_on) if vehicle.defrost_is_on is not None else False,
            consumption_30d_wh_per_km=vehicle.power_consumption_30d,
            est_portable_charge_min=vehicle.ev_estimated_portable_charge_duration,
            registration_date=str(vehicle.registration_date) if vehicle.registration_date else None,
            raw_data=_dump_vehicle(vehicle),
        )

class KiaConnector(_HyundaiKiaBase):
    BRAND_ID = 1

    @staticmethod
    def brand_name() -> str:
        return "Kia (UVO)"

    @staticmethod
    def credential_fields() -> list:
        return KIA_CREDENTIAL_FIELDS


class HyundaiConnector(_HyundaiKiaBase):
    BRAND_ID = 2

    @staticmethod
    def brand_name() -> str:
        return "Hyundai (Bluelink)"

    @staticmethod
    def credential_fields() -> list:
        return HYUNDAI_CREDENTIAL_FIELDS


# Register if dependency is installed
if HAS_HYUNDAI_KIA:
    register('kia', KiaConnector)
    register('hyundai', HyundaiConnector)
