"""Abstract base for vehicle API connectors."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
