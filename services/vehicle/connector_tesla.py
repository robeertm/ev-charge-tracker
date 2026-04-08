"""Tesla connector via teslapy."""
import logging

try:
    import teslapy
    HAS_TESLAPY = True
except ImportError:
    HAS_TESLAPY = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (Tesla Account)", "type": "text"},
    {"key": "password", "label": "Refresh-Token",  "type": "password",
     "help": "Tesla nutzt OAuth — Refresh-Token aus Auth-Flow oder teslapy cache.json."},
]

_teslas = {}


class TeslaConnector(VehicleConnector):

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._vehicle = None

    def _get_tesla(self):
        key = self.credentials.get('username', '')
        if key not in _teslas:
            _teslas[key] = teslapy.Tesla(key)
            if self.credentials.get('password'):
                _teslas[key].token = {'refresh_token': self.credentials['password']}
                _teslas[key].fetch_token()
        return _teslas[key]

    def _get_vehicle(self):
        if self._vehicle is None:
            t = self._get_tesla()
            vehicles = t.vehicle_list()
            if not vehicles:
                raise RuntimeError("Kein Fahrzeug im Tesla-Account gefunden")
            self._vehicle = vehicles[0]
        return self._vehicle

    def authenticate(self) -> bool:
        try:
            self._get_vehicle()
            return True
        except Exception as e:
            logger.error(f"Tesla auth failed: {e}")
            _teslas.pop(self.credentials.get('username', ''), None)
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def get_status(self, force=False) -> VehicleStatus:
        vehicle = self._get_vehicle()
        if force:
            vehicle.sync_wake_up()
        data = vehicle.get_vehicle_data()
        cs = data.get('charge_state', {})
        vs = data.get('vehicle_state', {})
        ds = data.get('drive_state', {})
        cls_ = data.get('climate_state', {})

        return VehicleStatus(
            soc_percent=cs.get('battery_level'),
            odometer_km=int(vs.get('odometer', 0) * 1.60934) if vs.get('odometer') else None,
            is_charging=cs.get('charging_state') == 'Charging',
            is_plugged_in=cs.get('charge_port_door_open', False) and cs.get('charge_port_latch') == 'Engaged',
            is_locked=vs.get('locked', True),
            charge_power_kw=cs.get('charger_power'),
            estimated_range_km=int(cs.get('battery_range', 0) * 1.60934) if cs.get('battery_range') else None,
            charge_limit_ac=cs.get('charge_limit_soc'),
            charge_limit_dc=cs.get('charge_limit_soc'),
            climate_temp=cls_.get('inside_temp'),
            climate_on=cls_.get('is_climate_on', False),
            location_lat=ds.get('latitude'),
            location_lon=ds.get('longitude'),
            last_updated=str(data.get('vehicle_state', {}).get('timestamp', '')),
            vehicle_name=data.get('display_name'),
            vehicle_model=f"Tesla {data.get('vehicle_config', {}).get('car_type', '')}",
            front_left_door_open=bool(vs.get('df', 0)),
            front_right_door_open=bool(vs.get('pf', 0)),
            back_left_door_open=bool(vs.get('dr', 0)),
            back_right_door_open=bool(vs.get('pr', 0)),
            trunk_open=bool(vs.get('rt', 0)),
            hood_open=bool(vs.get('ft', 0)),
            raw_data={'vin': data.get('vin')},
        )

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return "Tesla"


if HAS_TESLAPY:
    register('tesla', TeslaConnector)
