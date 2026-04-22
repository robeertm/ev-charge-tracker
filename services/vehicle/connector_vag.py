"""VW Group connectors (VW, Skoda, Seat, Cupra, Audi) via CarConnectivity.

Newer carconnectivity releases (>= 0.11) don't re-export `CarConnectivity`
at the top of the package, it lives in the `carconnectivity.carconnectivity`
submodule. Old releases had it on the top level. Try both so upgrades and
downgrades don't break.
"""
import json
import os
import tempfile
from datetime import datetime, date as _date

try:
    try:
        from carconnectivity.carconnectivity import CarConnectivity
    except ImportError:
        from carconnectivity import CarConnectivity  # type: ignore
    HAS_CARCONNECTIVITY = True
except ImportError:
    CarConnectivity = None  # type: ignore
    HAS_CARCONNECTIVITY = False

from .base import VehicleConnector, VehicleStatus
from .registry import register


def _dump_vag_vehicle(vehicle, max_depth=2):
    """Best-effort JSON-safe dump of a CarConnectivity Vehicle object.

    The object graph is deeply nested (drives[0].level.value, etc), so we
    walk two levels and capture ``value``/``unit`` pairs from any child
    that has them. Anything non-primitive gets stringified with a cap to
    keep the blob size bounded.
    """
    def _serialize(v, depth):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (datetime, _date)):
            return v.isoformat()
        if depth <= 0:
            return str(v)[:200]
        if isinstance(v, (list, tuple)):
            return [_serialize(x, depth - 1) for x in v[:20]]
        if isinstance(v, dict):
            return {str(k): _serialize(val, depth - 1) for k, val in list(v.items())[:50]}
        # Object-like: dump public attributes
        out = {}
        for key in sorted(dir(v)):
            if key.startswith('_'):
                continue
            try:
                child = getattr(v, key)
            except Exception as e:
                out[key] = f'<error: {type(e).__name__}>'
                continue
            if callable(child):
                continue
            out[key] = _serialize(child, depth - 1)
            # Cap attribute count to keep blobs sane
            if len(out) >= 50:
                break
        return out

    try:
        return _serialize(vehicle, max_depth)
    except Exception as e:
        return {'_error': f'{type(e).__name__}: {e}',
                'vin': getattr(vehicle, 'vin', None)}

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
            self._cc = CarConnectivity(
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
        except Exception as exc:
            self._last_error = str(exc)
            self._cc = None
            return False

    def test_connection(self) -> bool:
        """Raise on failure so the caller sees the real error message.

        VW-Group's identity server frequently asks the user to re-accept
        new terms/consent at https://identity.vwgroup.io after a password
        change or T&C update. The library raises with that URL in the
        message. Swallowing it (previous behavior) turned every failure
        into a generic "check password" flash — unhelpful because the
        password is almost always fine, just the consent is stale.
        """
        self._find_vehicle()
        return True

    def get_status(self, force=False) -> VehicleStatus:
        vehicle = self._find_vehicle()

        soc = None
        estimated_range = None
        charge_power = None
        is_charging = False

        # Electric drive info.
        # carconnectivity >= 0.11 changed ``vehicle.drives`` from a
        # subscriptable list to a ``Drives`` container object whose real
        # payload lives in ``.drives: Dict[str, GenericDrive]``. Older
        # code did ``vehicle.drives[0]`` which now raises
        # ``'Drives' object is not subscriptable``. Pull the dict, pick
        # the explicit ELECTRIC drive if present (hybrids expose both),
        # otherwise fall back to the first entry.
        drives_container = getattr(vehicle, 'drives', None)
        drives_dict = getattr(drives_container, 'drives', None) if drives_container is not None else None
        ed = None
        if drives_dict:
            for d in drives_dict.values():
                t = getattr(getattr(d, 'type', None), 'value', None)
                if t is not None and str(t).upper().endswith('ELECTRIC'):
                    ed = d
                    break
            if ed is None:
                ed = next(iter(drives_dict.values()), None)
        if ed is not None:
            level = getattr(ed, 'level', None)
            if level is not None and getattr(level, 'value', None) is not None:
                soc = int(level.value)
            rng = getattr(ed, 'range', None)
            if rng is not None and getattr(rng, 'value', None) is not None:
                estimated_range = int(rng.value)

        # Charging info. ``ch.state.value`` is the carconnectivity
        # EnumAttribute's inner enum member, not a plain string. A naive
        # ``'CHARGING' in str(ch.state.value).upper()`` matches every
        # state because ``str(ChargingState.OFF)`` renders as
        # ``'ChargingState.OFF'`` — and ``'CHARGING'`` is a substring
        # of ``'CHARGINGSTATE'``. Extract the enum's underlying value
        # (or the raw string if it's already a string), then compare
        # exactly. Fall back to ``charge_power > 0`` when the state is
        # missing or unknown: power flowing is the ground truth.
        if hasattr(vehicle, 'charging') and vehicle.charging:
            ch = vehicle.charging
            if hasattr(ch, 'power') and ch.power and hasattr(ch.power, 'value'):
                charge_power = float(ch.power.value) if ch.power.value is not None else None
            state_attr = getattr(ch, 'state', None)
            raw = getattr(state_attr, 'value', None) if state_attr is not None else None
            # EnumAttribute → enum member → underlying string
            inner = getattr(raw, 'value', None)
            state_str = str(inner if inner is not None else (raw or '')).strip().lower()
            if state_str == 'charging':
                is_charging = True
            elif not state_str and charge_power and charge_power > 0.1:
                # State unknown but power flowing — trust power.
                is_charging = True

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
            raw_data=_dump_vag_vehicle(vehicle),
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
