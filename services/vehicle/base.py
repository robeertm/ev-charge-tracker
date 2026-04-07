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
    charge_power_kw: Optional[float] = None
    estimated_range_km: Optional[int] = None
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
    def get_status(self) -> VehicleStatus:
        """Fetch current vehicle state (SoC, odometer, charging, etc.)."""

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
