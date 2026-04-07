"""VW Group connectors (VW, Skoda, Seat, Cupra, Audi) via CarConnectivity."""
import json
import os
import tempfile

try:
    import carconnectivity
    HAS_CARCONNECTIVITY = True
except ImportError:
    HAS_CARCONNECTIVITY = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail / Benutzername", "type": "text"},
    {"key": "password", "label": "Passwort", "type": "password"},
    {"key": "vin", "label": "FIN / VIN (optional, bei mehreren Fahrzeugen)", "type": "text"},
]


class VAGConnector(VehicleConnector):
    """Base connector for all VAG brands using CarConnectivity."""

    CONNECTOR_TYPE = None  # override in subclass
    BRAND_PARAM = None     # for seatcupra: "seat" or "cupra"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self._cc = None
        self._tokenstore = os.path.join(
            tempfile.gettempdir(), f'ev_tracker_cc_{self.CONNECTOR_TYPE}_tokens.json'
        )

    def _build_config(self) -> dict:
        connector_config = {
            "username": self.credentials["username"],
            "password": self.credentials["password"],
        }
        if self.BRAND_PARAM:
            connector_config["brand"] = self.BRAND_PARAM
        return {
            "carConnectivity": {
                "connectors": [{
                    "type": self.CONNECTOR_TYPE,
                    "config": connector_config,
                }]
            }
        }

    def _get_cc(self):
        if self._cc is None:
            config = self._build_config()
            self._cc = carconnectivity.CarConnectivity(
                config=config,
                tokenstore_file=self._tokenstore,
            )
            self._cc.startup()
        return self._cc

    def _find_vehicle(self):
        cc = self._get_cc()
        cc.fetch_all()
        garage = cc.get_garage()
        vin = self.credentials.get('vin', '').strip()
        if vin:
            return garage.get_vehicle(vin)
        vehicles = list(garage.list_vehicles())
        if not vehicles:
            raise RuntimeError("Kein Fahrzeug im Account gefunden")
        return vehicles[0]

    def authenticate(self) -> bool:
        try:
            self._get_cc()
            return True
        except Exception:
            self._cc = None
            return False

    def test_connection(self) -> bool:
        try:
            self._find_vehicle()
            return True
        except Exception:
            self._cc = None
            return False

    def get_status(self) -> VehicleStatus:
        vehicle = self._find_vehicle()

        soc = None
        estimated_range = None
        charge_power = None
        is_charging = False

        # Electric drive info
        if hasattr(vehicle, 'drives') and vehicle.drives:
            ed = vehicle.drives[0]
            if hasattr(ed, 'level') and ed.level and hasattr(ed.level, 'value'):
                soc = int(ed.level.value) if ed.level.value is not None else None
            if hasattr(ed, 'range') and ed.range and hasattr(ed.range, 'value'):
                estimated_range = int(ed.range.value) if ed.range.value is not None else None

        # Charging info
        if hasattr(vehicle, 'charging') and vehicle.charging:
            ch = vehicle.charging
            if hasattr(ch, 'state') and ch.state and hasattr(ch.state, 'value'):
                state_val = str(ch.state.value).upper()
                is_charging = 'CHARGING' in state_val
            if hasattr(ch, 'power') and ch.power and hasattr(ch.power, 'value'):
                charge_power = float(ch.power.value) if ch.power.value is not None else None

        # Odometer
        odometer = None
        if hasattr(vehicle, 'odometer') and vehicle.odometer and hasattr(vehicle.odometer, 'value'):
            odometer = int(vehicle.odometer.value) if vehicle.odometer.value is not None else None

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=odometer,
            is_charging=is_charging,
            charge_power_kw=charge_power,
            estimated_range_km=estimated_range,
            raw_data={'vin': getattr(vehicle, 'vin', None)},
        )

    def shutdown(self):
        if self._cc:
            try:
                self._cc.shutdown()
            except Exception:
                pass
            self._cc = None

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS


class VWConnector(VAGConnector):
    CONNECTOR_TYPE = "volkswagen"

    @staticmethod
    def brand_name() -> str:
        return "Volkswagen (WeConnect)"


class SkodaConnector(VAGConnector):
    CONNECTOR_TYPE = "skoda"

    @staticmethod
    def brand_name() -> str:
        return "Skoda (MySkoda)"


class SeatConnector(VAGConnector):
    CONNECTOR_TYPE = "seatcupra"
    BRAND_PARAM = "seat"

    @staticmethod
    def brand_name() -> str:
        return "Seat (MyCar)"


class CupraConnector(VAGConnector):
    CONNECTOR_TYPE = "seatcupra"
    BRAND_PARAM = "cupra"

    @staticmethod
    def brand_name() -> str:
        return "Cupra (MyCupra)"


class AudiConnector(VAGConnector):
    CONNECTOR_TYPE = "audi"

    @staticmethod
    def brand_name() -> str:
        return "Audi (myAudi)"


# Register if dependency is installed
if HAS_CARCONNECTIVITY:
    register('vw', VWConnector)
    register('skoda', SkodaConnector)
    register('seat', SeatConnector)
    register('cupra', CupraConnector)
    register('audi', AudiConnector)
