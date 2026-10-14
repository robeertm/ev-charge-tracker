# -*- coding: utf-8 -*-
"""Hyundai, Kia and Genesis via the official Pleos personal Vehicle Data API.

Why this exists: see ``pleos_api``. In short, since 14 October 2026 the
manufacturer no longer lets third-party programs sign in the way the app
does (code 9035), and Pleos is the official door it offers instead.

**Why separate brand keys** (``kia_pleos``, ``hyundai_pleos``,
``genesis_pleos``) instead of changing what ``kia`` means: an install whose
old access still works (US, Canada, Korea, or Europe if the block is ever
lifted) keeps it untouched, and switching is a deliberate act that proves
the new key before it overwrites anything — the same reasoning as for the
official Škoda API (``connector_skoda_public``).

**What Pleos gives us**: state of charge, charging and plug state, AC/DC
targets, time to full, range, odometer, position, doors/lock, bonnet and
boot, climate, outside temperature. That covers everything the app's own
charge detection and trip derivation need — both are built on sequences
of position, odometer and SoC.

**What it does not give us, stated plainly**: no 12 V battery, no state
of health, no charging power, no 30-day consumption, no remote commands
and no wake-up — it serves what the car last reported. The Kia/Hyundai
trip-log backfill (``trip_log_fetch``) has no counterpart; trips are
derived from parking events, as for the official Škoda API.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import KM_PER_MI, VehicleConnector, VehicleStatus, plausible_percent
from .pleos_api import KEY_MANAGEMENT_URL, PleosError, pick_vehicle, snapshot
from .registry import register

logger = logging.getLogger(__name__)


def _fields(context: str) -> list:
    return [
        {"key": "username", "label": "Client ID (Pleos)", "type": "text",
         "label_key": "cred.client_id", "context": "Pleos",
         "help_key": "cred.help_pleos_key", "help_url": KEY_MANAGEMENT_URL},
        {"key": "password", "label": "Client Secret (Pleos)", "type": "password",
         "label_key": "cred.client_secret", "context": "Pleos"},
        {"key": "vin", "label": "VIN (optional, bei mehreren Fahrzeugen)", "type": "text",
         "label_key": "cred.vin", "optional": True, "help_key": "cred.help_vin_multi"},
    ]


# Charging states that mean energy is flowing into the car.
_CHARGING_STATES = {'charging', 'fastcharging', 'wirelesscharging'}
# remainTime is 0..65535 minutes; the top value is the "no value" marker,
# the same kind of sentinel as the 12 V reading's 255 (see base.py).
_NO_TIME = 65535


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return v


def _dist_km(value, unit) -> Optional[float]:
    """A Pleos distance as km. Units: km, miles, meter, feet, invalid."""
    v = _num(value)
    if v is None:
        return None
    u = str(unit or 'km').strip().lower()
    if u in ('km', 'kilometer', 'kilometers'):
        return v
    if u in ('mile', 'miles', 'mi'):
        return v * KM_PER_MI
    if u in ('meter', 'meters', 'm'):
        return v / 1000.0
    if u in ('feet', 'foot', 'ft'):
        return v * 0.0003048
    return None                              # 'invalid' or something new


def _celsius(obj) -> Optional[float]:
    if not isinstance(obj, dict):
        return None
    v = _num(obj.get('value'))
    if v is None:
        return None
    u = str(obj.get('unit') or 'C').upper()
    if u == 'F':
        return round((v - 32) * 5 / 9, 1)
    if u == 'C':
        return v
    return None


def _rows(value) -> list:
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _ev_range(powertrains: dict):
    """(km, unit as reported) from distanceToEmpties, preferring the EV entry."""
    best = None
    for entry in _rows((powertrains or {}).get('distanceToEmpties')):
        kind = str(entry.get('type') or '').upper()
        km = _dist_km(entry.get('value'), entry.get('unit'))
        if km is None:
            continue
        if kind == 'EV':
            return km, entry.get('unit')
        if best is None and kind in ('PHEV', 'HEV', 'FCEV'):
            best = (km, entry.get('unit'))
    return best if best else (None, None)


def _doors(status: dict):
    """Per-door open flags and the lock state, as (open map, locked, known).

    ``doors`` is a list of rows (front, rear), each ``{left, right}`` with
    ``open`` / ``closed`` / ``locked`` / ``invalid``. "closed" is a shut but
    UNLOCKED door. A car counts as locked only when every door that
    answered says ``locked``; no usable answer means "don't know", which
    keeps the safe default instead of raising a false open-padlock alarm.
    """
    rows = _rows((status or {}).get('doors'))
    names = (('front_left', 'front_right'), ('back_left', 'back_right'))
    opened = {}
    states = []
    for i, row in enumerate(rows[:2]):
        for side, key in zip(('left', 'right'), names[i]):
            s = str(row.get(side) or '').lower()
            if s in ('open', 'closed', 'locked'):
                states.append(s)
                opened[key] = (s == 'open')
    if not states:
        return opened, True, False
    return opened, all(s == 'locked' for s in states), True


def _newest(*stamps) -> Optional[str]:
    vals = [s for s in stamps if isinstance(s, str) and s]
    return max(vals) if vals else None      # ISO-8601 UTC sorts as text


class _PleosBase(VehicleConnector):
    BRAND = ''           # Pleos 'Brand' header
    LABEL = ''

    def _cid(self) -> str:
        return (self.credentials.get('username') or '').strip()

    def _secret(self) -> str:
        return (self.credentials.get('password') or '').strip()

    def _vin(self) -> str:
        return (self.credentials.get('vin') or '').strip().upper()

    def _car(self, fresh: bool = False) -> dict:
        return pick_vehicle(self.BRAND, self._cid(), self._secret(), self._vin(), fresh=fresh)

    # ── the interface ─────────────────────────────────────────────────
    def authenticate(self) -> bool:
        try:
            self._car()
            return True
        except PleosError as e:
            logger.error(f"Pleos {self.BRAND} sign-in failed ({e.kind})")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def verify_credentials(self) -> None:
        # The API's own reason, translated: "key invalid", "pick a VIN",
        # "rate limited" each need a different action from the owner.
        self._car(fresh=True)

    def get_status(self, force=False) -> VehicleStatus:
        """Read the car. ``force`` cannot wake it — Pleos has no such call,
        it serves what the car last reported — so it changes nothing."""
        car = self._car()
        snap = snapshot(self.BRAND, self._cid(), self._secret(), car['vin'])
        return self._to_status(snap, car)

    # ── mapping ───────────────────────────────────────────────────────
    def _to_status(self, snap: dict, car: dict) -> VehicleStatus:
        bat = snap.get('batteries') or {}
        charge = bat.get('charge') or {}
        pt = snap.get('powertrains') or {}
        drv = snap.get('driving') or {}
        loc_block = snap.get('locations') or {}
        loc = loc_block.get('location') or {}
        st = snap.get('status') or {}

        soc = plausible_percent(_num(charge.get('stateOfCharge')))
        if soc is None:
            for b in _rows(pt.get('batteries')):
                if str(b.get('type') or '').lower() == 'main':
                    soc = plausible_percent(_num(b.get('remainRatio')))
                    break
        soc = int(round(soc)) if soc is not None else None

        state = str(charge.get('status') or '')
        charging = (charge.get('charging') is True) or state.lower() in _CHARGING_STATES
        plugin = str(charge.get('plugin') or '').lower()
        plugged = plugin == 'connected' or charging

        remain = _num(charge.get('remainTime'))
        remain = int(remain) if remain is not None and 0 <= remain < _NO_TIME else None

        targets = charge.get('targetStateOfCharge') or {}
        lim_ac = plausible_percent(_num(targets.get('standard')))
        lim_dc = plausible_percent(_num(targets.get('quick')))

        rng_km, rng_unit = _ev_range(pt)

        odo = drv.get('odometer') or {}
        odo_km = _dist_km(odo.get('value'), odo.get('unit'))
        # Truncate like every other connector: a rounded odometer would
        # read as "moved 1 km" once (see base.to_km).
        odo_km = int(odo_km) if odo_km is not None else None

        lat = _num(loc.get('latitude'))
        lon = _num(loc.get('longitude'))
        coord_type = loc.get('type')
        # Only WGS84 (type 0) is a position we can draw. Bessel and the
        # shifted Korean grid would land the pin metres to kilometres off;
        # 0/0 is the Gulf of Guinea, not a parking spot.
        if (coord_type not in (0, '0', None) or lat is None or lon is None
                or not (-90 <= lat <= 90 and -180 <= lon <= 180)
                or (lat == 0 and lon == 0)):
            lat = lon = None

        opened, locked, lock_known = _doors(st)
        climate = st.get('climateControl') or {}
        warnings = [str(w) for w in (st.get('warnings') or []) if isinstance(w, str)]

        name = car.get('nickname') or car.get('sellname') or self.LABEL
        model = car.get('sellname') or car.get('name') or self.LABEL
        unit_seen = (str(odo.get('unit') or rng_unit or '').lower() or None)

        return VehicleStatus(
            soc_percent=soc,
            odometer_km=odo_km,
            is_charging=charging,
            is_plugged_in=plugged,
            is_locked=locked,
            estimated_range_km=int(round(rng_km)) if rng_km is not None else None,
            charge_limit_ac=int(lim_ac) if lim_ac is not None else None,
            charge_limit_dc=int(lim_dc) if lim_dc is not None else None,
            est_charge_duration_min=remain if charging or plugged else None,
            climate_temp=_celsius(climate.get('temperature')),
            climate_on=str(climate.get('status') or '').lower() == 'on',
            location_lat=lat,
            location_lon=lon,
            last_updated=_newest(bat.get('timestamp'), drv.get('timestamp'),
                                 loc_block.get('timestamp'), pt.get('timestamp'),
                                 st.get('timestamp')),
            vehicle_name=str(name),
            vehicle_model=str(model),
            front_left_door_open=opened.get('front_left', False),
            front_right_door_open=opened.get('front_right', False),
            back_left_door_open=opened.get('back_left', False),
            back_right_door_open=opened.get('back_right', False),
            trunk_open=str(st.get('trunk') or '').lower() == 'open',
            hood_open=str(st.get('hood') or '').lower() == 'open',
            tire_warn_all=any('tire' in w.lower() for w in warnings),
            steering_wheel_heater=str(st.get('steeringWheelHeat') or '').lower() == 'on',
            defrost=str((st.get('windshield') or {}).get('defrost') or '').lower() == 'on',
            # Values above are km already; this only tells the sync which
            # unit the cloud used (see unit_history.note_reported_unit).
            distance_unit=('mi' if unit_seen in ('mile', 'miles', 'mi') else
                           'km' if unit_seen == 'km' else None),
            raw_data={
                'source': 'pleos',
                'brand': self.BRAND,
                'vin': car.get('vin'),
                'vehicle_type': car.get('type'),
                'model_code': car.get('name'),
                'charging_state': state,
                'plugin': plugin,
                'battery_capacity_wh': _num(charge.get('batteryCapacity')),
                'outside_temperature_c': _celsius(st.get('outsideTemperature')),
                'ignition': (drv.get('ignition') or {}).get('status'),
                'sleep_mode': drv.get('sleepMode'),
                'in_park': (drv.get('transmission') or {}).get('parkingPosition'),
                'lock_status_known': lock_known,
                'warnings': warnings,
                # Parts this car does not report, or that failed this round.
                # Kept so a missing value can be explained rather than look
                # like a bug in this app.
                'api_errors': dict(snap.get('_errors') or {}),
                'pleos': {k: v for k, v in snap.items() if not k.startswith('_')},
            },
        )

    # ── UI metadata ───────────────────────────────────────────────────
    @staticmethod
    def key_management_url() -> str:
        return KEY_MANAGEMENT_URL


class KiaPleosConnector(_PleosBase):
    BRAND = 'kia'
    LABEL = 'Kia'

    @staticmethod
    def credential_fields() -> list:
        return _fields('Kia')

    @staticmethod
    def brand_name() -> str:
        return 'Kia (Pleos)'


class HyundaiPleosConnector(_PleosBase):
    BRAND = 'hyundai'
    LABEL = 'Hyundai'

    @staticmethod
    def credential_fields() -> list:
        return _fields('Hyundai')

    @staticmethod
    def brand_name() -> str:
        return 'Hyundai (Pleos)'


class GenesisPleosConnector(_PleosBase):
    BRAND = 'genesis'
    LABEL = 'Genesis'

    @staticmethod
    def credential_fields() -> list:
        return _fields('Genesis')

    @staticmethod
    def brand_name() -> str:
        return 'Genesis (Pleos)'


# Which Pleos key replaces which old brand key (the switch-over card).
REPLACES = {'kia': 'kia_pleos', 'hyundai': 'hyundai_pleos'}

# No optional dependency: plain HTTPS through the standard library, so
# these brands are available on every install.
register('kia_pleos', KiaPleosConnector)
register('hyundai_pleos', HyundaiPleosConnector)
register('genesis_pleos', GenesisPleosConnector)
