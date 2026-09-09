# -*- coding: utf-8 -*-
"""Škoda via the official MyŠkoda Public API.

Škoda announced that the unofficial app API — the one
``carconnectivity-connector-skoda`` and the ``myskoda`` library speak, and
the one ``connector_vag.SkodaConnector`` has used until now — stops
serving third-party clients in October 2026. This connector talks to the
official replacement instead.

**Why a separate brand key** (``skoda_api``) rather than changing what
``skoda`` means: an install that already has a Škoda stored keeps working
untouched until it is deliberately switched over. Rewriting the brand of
every existing Škoda in the database to make a name free would put a
migration in the path of people who did not ask for one, on a day when
their old access still works fine. The key is internal — the UI shows the
label from ``catalog.py``, which is simply "Škoda".

**What the official API gives us**, all from one ``GET``: state of charge,
range, odometer, charging state and power, target SoC, parking position,
lock state, doors/trunk/bonnet. That covers everything this app's own
charge detection and trip derivation need, because both are built on
sequences of position, odometer and SoC rather than on vendor history.

**What it does not give us, and this is a real loss to state plainly:**
there is no trips endpoint and no charging-history endpoint. The
``skoda_trip_fetch`` / ``skoda_charging_fetch`` backfills read those from
the old private API and have no counterpart here. Trips recorded from now
on are the ones this app derives itself from parking events; historical
trips and charges already imported stay in the database and are not
touched.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import VehicleConnector, VehicleStatus
from .registry import register
from .skoda_public_api import (KEY_MANAGEMENT_URL, SkodaApiError, budget_state,
                               get_vehicle)

logger = logging.getLogger(__name__)

# Only the parts we actually map. Asking explicitly also makes the API
# tell us when a part is unsupported by this particular car, instead of
# leaving it silently absent (see skoda_public_api.get_vehicle).
PARTS = ['info', 'status', 'odometer', 'parkingPosition', 'charging', 'fuelStatus']

CREDENTIAL_FIELDS = [
    {"key": "password", "label": "API-Schlüssel (MyŠkoda-App)", "type": "password"},
    {"key": "vin", "label": "Fahrgestellnummer (VIN)", "type": "text"},
]

# States in which the cable is physically connected. CONNECT_CABLE is the
# opposite — it is the car asking for the cable, not reporting one.
_PLUGGED_STATES = {'CHARGING', 'CONSERVING', 'READY_FOR_CHARGING',
                   'CHARGING_INTERRUPTED'}


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    v = _num(value)
    return None if v is None else int(round(v))


def _locked(overall: dict):
    """Locked / unlocked / don't know — as three states, not two.

    The API says ``locked`` may be YES, NO or **UNKNOWN**, and offers
    ``reliableLockStatus`` (LOCKED / UNLOCKED / UNKNOWN) alongside it.
    Testing ``== 'YES'`` collapses UNKNOWN into "unlocked", which puts an
    open-padlock warning on a car nobody can actually vouch for. Failing
    towards a false alarm about a vehicle's security is the wrong
    direction, so an unknown answer keeps the safe default and is
    recorded as unknown rather than reported as a state.

    Returns ``(is_locked, known)``.
    """
    reliable = str(overall.get('reliableLockStatus') or '').upper()
    if reliable == 'LOCKED':
        return True, True
    if reliable == 'UNLOCKED':
        return False, True
    plain = str(overall.get('locked') or '').upper()
    if plain == 'YES':
        return True, True
    if plain == 'NO':
        return False, True
    return True, False


class SkodaPublicConnector(VehicleConnector):

    # ── plumbing ──────────────────────────────────────────────────────
    def _key(self) -> str:
        return (self.credentials.get('password') or '').strip()

    def _vin(self) -> str:
        return (self.credentials.get('vin') or '').strip().upper()

    def _fetch(self, spend_reserve: bool = False) -> dict:
        return get_vehicle(self._key(), self._vin(), parts=PARTS,
                           spend_reserve=spend_reserve)

    # ── the interface ─────────────────────────────────────────────────
    def authenticate(self) -> bool:
        try:
            self._fetch(spend_reserve=True)
            return True
        except SkodaApiError as e:
            logger.error(f"Skoda public API auth failed ({e.kind}): {e}")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def verify_credentials(self) -> None:
        # Let the API's own reason through — "key expired", "not valid for
        # this vehicle", "quota exhausted" each need a different action
        # from the user, and "rejected" tells them none of it.
        self._fetch(spend_reserve=True)

    def get_status(self, force=False) -> VehicleStatus:
        """Read the vehicle.

        ``force`` does NOT mean "wake the car" here — the official API has
        no wake-up call, it serves what the vehicle last reported. What it
        does mean is "a person is waiting", which is allowed to spend the
        slice of the hourly quota that background polling leaves alone.
        """
        data = self._fetch(spend_reserve=bool(force))
        return self._to_status(data)

    # ── mapping ───────────────────────────────────────────────────────
    def _to_status(self, data: dict) -> VehicleStatus:
        v = (data or {}).get('vehicle') or {}
        charging = v.get('charging') or {}
        ch_status = charging.get('status') or {}
        battery = ch_status.get('battery') or {}
        settings = charging.get('settings') or {}
        odo = v.get('odometer') or {}
        park = v.get('parkingPosition') or {}
        gps = park.get('gpsCoordinates') or {}
        status = v.get('status') or {}
        overall = status.get('overall') or {}
        detail = status.get('detail') or {}
        fuel = v.get('fuelStatus') or {}
        primary = fuel.get('primaryEngineRange') or {}

        state = str(ch_status.get('state') or '').upper()
        _is_locked, _lock_known = _locked(overall)

        # SoC: the charging block is the precise one; the engine-range
        # block is the fallback for a car that reports no charging data.
        soc = _int(battery.get('stateOfChargeInPercent'))
        if soc is None:
            soc = _int(primary.get('currentSoCInPercent'))

        # Range arrives in metres here and kilometres there.
        rng_m = _num(battery.get('remainingCruisingRangeInMeters'))
        rng = int(round(rng_m / 1000.0)) if rng_m is not None else \
            _int(primary.get('remainingRangeInKm'))

        lat = _num(gps.get('latitude'))
        lon = _num(gps.get('longitude'))
        # A car in motion reports no position. Passing 0/0 would drop a
        # pin in the Gulf of Guinea and silently corrupt the trip log.
        if str(park.get('state') or '').upper() == 'IN_MOTION':
            lat = lon = None

        name = v.get('name') or 'Škoda'

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=_int(odo.get('mileageInKm')),
            is_charging=(state == 'CHARGING'),
            is_plugged_in=(state in _PLUGGED_STATES),
            is_locked=_is_locked,
            charge_power_kw=_num(ch_status.get('chargePowerInKw')),
            estimated_range_km=rng,
            charge_limit_ac=_int(settings.get('targetStateOfChargeInPercent')),
            est_charge_duration_min=_int(
                ch_status.get('remainingTimeToFullyChargedInMinutes')),
            location_lat=lat,
            location_lon=lon,
            last_updated=(charging.get('carCapturedTimestamp')
                          or odo.get('carCapturedTimestamp')
                          or status.get('carCapturedTimestamp')),
            vehicle_name=str(name),
            vehicle_model=str(name),
            trunk_open=str(detail.get('trunk') or '').upper() == 'OPEN',
            hood_open=str(detail.get('bonnet') or '').upper() == 'OPEN',
            raw_data={
                'vin': v.get('vin') or self._vin(),
                'charging_state': state,
                'charge_type': ch_status.get('chargeType'),
                'parking_state': park.get('state'),
                'lock_status_known': _lock_known,
                'formatted_address': park.get('formattedAddress'),
                'key_expires_at': data.get('_key_expires_at'),
                'rate_limit': budget_state(self._vin()),
                # Parts the car does not support or could not deliver.
                # Kept so a missing value can be explained instead of
                # looking like a bug in this app.
                'api_errors': data.get('errors') or [],
            },
        )

    # ── remote control ────────────────────────────────────────────────
    def remote_commands(self) -> list:
        # No lock/unlock: the official API has no such endpoint. Saying
        # so by omission is the point — the UI draws what this returns.
        return ['charging_start', 'charging_stop', 'charging_limit',
                'ac_start', 'ac_stop', 'ventilation_start', 'ventilation_stop']

    def send_remote(self, command: str, params: dict) -> dict:
        from .base import RemoteNotSupported
        if command not in self.remote_commands():
            raise RemoteNotSupported(command)
        from .skoda_public_api import send_command
        return send_command(self._key(), self._vin(), command,
                            enabled=True, params=params or {})

    # ── UI metadata ───────────────────────────────────────────────────
    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return "Škoda (offizielle API)"

    @staticmethod
    def key_management_url() -> str:
        return KEY_MANAGEMENT_URL


# No optional dependency to guard: the client speaks HTTP through the
# standard library, so this brand is available on every install.
register('skoda_api', SkodaPublicConnector)
