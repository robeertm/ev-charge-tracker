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
