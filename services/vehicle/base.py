"""Abstract base for vehicle API connectors."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# A 12 V reading is a percentage, so 0..100 is the whole of its range.
# Anything outside is not a measurement: the ECU answers 255 (0xFF, "no
# value") for a while after work on the 12 V battery. One Kia install logged
# 255 % for nine syncs across 18 hours after a battery service, and that one
# sentinel is wrong twice over — the history plot draws a 255 % spike, and
# ``sync_service.is_12v_low`` reads the newest stored value, so a sentinel
# reports a healthy battery and lets the force-refresh guard wake the car on
# a flat one. Dropping the field (not the row) keeps every other value of
# that sync; the untouched payload stays in ``VehicleSync.raw_json``.
def plausible_percent(value):
    """``value`` if it is a usable percentage, else ``None``.

    Deliberately not a clamp: 255 does not mean "full", it means "unknown",
    and a clamp to 100 would invent a reading that the car never reported.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 100:
        return None
    return value


# v3.0.155: the app stores kilometres, full stop. A manufacturer cloud that
# answers in miles (every US Kia/Hyundai, Porsche or VW accounts set to
# imperial) used to land in ``odometer_km`` as it came — 30 000 miles
# became "30 000 km", and every trip, km/h and kWh/100 km computed from it
# was off by 1.609 without anything looking wrong. The connectors now
# convert at the door; showing miles is the page's job (services/units).
KM_PER_MI = 1.609344
_MILE_NAMES = {'mi', 'mile', 'miles', 'mph', 'imperial'}
_KM_NAMES = {'km', 'kilometer', 'kilometers', 'kilometre', 'kilometres', 'km/h',
             'kmh', 'kph', 'metric'}


def unit_name(unit):
    """'mi', 'km' or None from whatever a library calls its unit — a
    string ('mi', 'MILES', 'km'), an enum (Length.MI, value 'mi'), or a
    small object carrying ``unit``/``value``."""
    if unit is None:
        return None
    inner = getattr(unit, 'value', None)
    if isinstance(inner, str):
        unit = inner
    name = getattr(unit, 'name', None) if not isinstance(unit, str) else None
    raw = str(unit if isinstance(unit, str) else (name or unit)).strip().lower()
    raw = raw.rsplit('.', 1)[-1]          # 'Length.MI' -> 'mi'
    if raw in _MILE_NAMES:
        return 'mi'
    if raw in _KM_NAMES:
        return 'km'
    return None


def to_km(value, unit, as_int=False):
    """A distance (or speed) as km, given the unit it arrived in. An unknown
    unit is taken as km — that is what every connector assumed until now,
    so nothing that worked changes."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if unit_name(unit) != 'mi':
        # km stays exactly as before — int() truncates, as every connector
        # did; a rounded odometer would read as "moved 1 km" once.
        return int(v) if as_int else v
    v *= KM_PER_MI
    return int(round(v)) if as_int else v


@dataclass
class VehicleStatus:
    soc_percent: Optional[int] = None
    odometer_km: Optional[int] = None
    is_charging: bool = False
    is_plugged_in: bool = False
    is_locked: bool = True
    charge_power_kw: Optional[float] = None
    estimated_range_km: Optional[int] = None
    battery_12v_percent: Optional[int] = None
    battery_soh_percent: Optional[int] = None
    charge_limit_ac: Optional[int] = None
    charge_limit_dc: Optional[int] = None
    est_charge_duration_min: Optional[int] = None
    est_fast_charge_duration_min: Optional[int] = None
    climate_temp: Optional[float] = None
    climate_on: bool = False
    total_power_consumed_kwh: Optional[float] = None
    total_power_regenerated_kwh: Optional[float] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None
    last_updated: Optional[str] = None
    vehicle_name: Optional[str] = None
    vehicle_model: Optional[str] = None
    # Doors & openings
    front_left_door_open: bool = False
    front_right_door_open: bool = False
    back_left_door_open: bool = False
    back_right_door_open: bool = False
    trunk_open: bool = False
    hood_open: bool = False
    # Tire pressure warnings
    tire_warn_all: bool = False
    tire_warn_fl: bool = False
    tire_warn_fr: bool = False
    tire_warn_rl: bool = False
    tire_warn_rr: bool = False
    # Extras
    steering_wheel_heater: bool = False
    rear_window_heater: bool = False
    defrost: bool = False
    consumption_30d_wh_per_km: Optional[int] = None
    est_portable_charge_min: Optional[int] = None
    registration_date: Optional[str] = None
    # v3.0.155: the unit the cloud reported distances in ('km'/'mi'/None).
    # The values above are already km; this only tells the sync that a
    # car's earlier history may have been stored in miles.
    distance_unit: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw_data: dict = field(default_factory=dict)


class VehicleConnector(ABC):
    """Interface that every brand connector must implement."""

    def __init__(self, credentials: dict):
        self.credentials = credentials

    @abstractmethod
    def authenticate(self) -> bool:
        """Login to the vehicle cloud API. Returns True on success."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Quick connectivity/credential check."""

    @abstractmethod
    def get_status(self, force=False) -> VehicleStatus:
        """Fetch current vehicle state. force=True wakes the car for fresh data."""

    @staticmethod
    @abstractmethod
    def credential_fields() -> list:
        """Return list of dicts describing required credential fields.

        Each dict: {"key": str, "label": str, "type": "text"|"password"|"select",
                     "options": [...] (only for select)}
        """

    @staticmethod
    @abstractmethod
    def brand_name() -> str:
        """Human-readable brand name for the UI."""

    def verify_credentials(self) -> None:
        """Check the stored credentials and RAISE with a reason if they fail.

        The "Testen" button in the vehicle list used to call
        ``connector._ensure_auth()`` — a *private* method that only two of
        the ten connector modules happen to have. For every other brand
        the button answered with an AttributeError instead of a verdict,
        and it had done so for as long as those brands existed. The XPENG
        connector even carried a comment saying its ``_ensure_auth`` was
        named that way "so the Testen button works", which documents the
        problem rather than fixing it: a private name had become an
        accidental interface that every new brand had to guess.

        So this is the public one. The default is good enough for any
        connector whose ``test_connection`` already does the work;
        connectors that can explain *why* a sign-in failed override it and
        raise their own, better message.
        """
        if not self.test_connection():
            raise RuntimeError('Die hinterlegten Zugangsdaten wurden abgelehnt.')

    # ── optional: remote control ──────────────────────────────────────
    # Not abstract on purpose. A connector that says nothing supports
    # nothing, so adding a brand can never accidentally expose commands
    # it has not implemented.
    def remote_commands(self) -> list:
        """Command names from REMOTE_COMMANDS this connector can send."""
        return []

    def send_remote(self, command: str, params: dict) -> dict:
        """Send one command. Returns ``{'accepted': bool}``.

        Raises RemoteNotSupported when the brand cannot do it. Callers
        must have checked the vehicle's opt-in BEFORE getting here.
        """
        raise RemoteNotSupported(command)


# ── Remote control ────────────────────────────────────────────────────
#
# One vocabulary for every brand, so the UI renders buttons from what a
# connector says it can do instead of carrying a per-brand list of its
# own — the drift that put a Škoda tile on the VW connector started
# exactly that way.
REMOTE_COMMANDS = (
    'charging_start', 'charging_stop', 'charging_limit',
    'ac_start', 'ac_stop',
    'ventilation_start', 'ventilation_stop',
    'lock', 'unlock',
)

# Commands that change the car's physical security, as opposed to its
# comfort or its charging. These need a deliberate confirmation of their
# own: turning remote control on so you can pre-heat the car in winter is
# not the same decision as being one stray click away from unlocking it
# in a car park.
SENSITIVE_COMMANDS = frozenset({'unlock'})


class RemoteNotSupported(RuntimeError):
    """This brand cannot do that — a clear answer, not a stack trace."""


def remote_commands_of(connector) -> list:
    """What this connector supports, filtered to the shared vocabulary.

    Filtering here rather than trusting the connector means a typo in one
    brand cannot put a button in the UI that no route can service.
    """
    try:
        names = list(connector.remote_commands() or [])
    except Exception:
        return []
    return [n for n in names if n in REMOTE_COMMANDS]
