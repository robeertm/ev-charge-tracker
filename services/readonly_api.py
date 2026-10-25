# -*- coding: utf-8 -*-
"""A narrow, read-only view of the cars for assistants and dashboards.

``GET /api/readonly/vehicle`` with ``Authorization: Bearer <token>`` answers
with what the app already knows about each car: state of charge, range,
charging, plug, target, odometer, when the car reported it, when the app
last asked, and the last logged charge. Nothing here can change anything.

Why its own token and not the web login: a program that only needs to read
the battery level should not hold the key that can also change settings,
delete history or move a car's hardware. The token is a separate secret,
created and revoked on the command line (``python3 -m
services.readonly_token new|off|status``), and stored only as a SHA-256
hash — a copy of the database does not reveal it.

What is deliberately NOT in the answer: GPS coordinates, the VIN, number
plates, any credential, account or tariff data, addresses or place names.
The last charge says only whether it was at home or at a public charger,
and only when the app itself recorded that — otherwise ``null``.

Protection is the token. The address a request comes from is not a usable
signal here: inside a container every request arrives from the bridge
gateway. The failure brake below therefore counts ALL failed attempts,
from wherever they come, and while it is engaged every request is refused
without looking at the token — otherwise the difference between 429 and
200 would let guessing continue at full speed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: AppConfig key holding the SHA-256 (hex) of the read-only token. Absent or
#: empty = the endpoint is switched off and answers 404.
TOKEN_HASH_KEY = 'readonly_api_token_sha256'
#: When the current token was created (ISO, local time). Informational only.
TOKEN_CREATED_KEY = 'readonly_api_token_created_at'

#: Per-vehicle record of the last poll, written by the sync path. The
#: ``vehicle_syncs`` table only gains a row when a tracked value changes, so
#: its newest row says when the values last CHANGED, not when the app last
#: asked — and it has no column for the plug, the target or the car's own
#: timestamp. This one small record holds exactly what the connector
#: answered on the last poll, unchanged.
LAST_POLL_KEY = 'vehicle_{vid}_last_poll'

#: Failure brake: at most this many failed attempts per window, counted
#: across all callers.
MAX_FAILURES = 10
WINDOW_S = 60.0

_failures: deque = deque()
_lock = threading.Lock()


# ── token ─────────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def stored_hash() -> str:
    from models.database import AppConfig
    return (AppConfig.get(TOKEN_HASH_KEY, '') or '').strip()


def token_matches(presented: str, expected_hash: str) -> bool:
    if not presented or not expected_hash:
        return False
    return hmac.compare_digest(hash_token(presented), expected_hash)


def bearer_from_header(value: str | None) -> str:
    """The token from an ``Authorization: Bearer …`` header, or ''."""
    if not value:
        return ''
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return ''
    return parts[1].strip()


# ── failure brake ─────────────────────────────────────────────────────
def _prune(now: float) -> None:
    while _failures and now - _failures[0] >= WINDOW_S:
        _failures.popleft()


def brake_retry_after(now: float | None = None) -> int:
    """Seconds until the next attempt is allowed; 0 = not braked."""
    now = time.monotonic() if now is None else now
    with _lock:
        _prune(now)
        if len(_failures) < MAX_FAILURES:
            return 0
        return max(1, int(WINDOW_S - (now - _failures[0])) + 1)


def note_failure(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _lock:
        _prune(now)
        _failures.append(now)


def reset_brake() -> None:
    """For tests."""
    with _lock:
        _failures.clear()


# ── last-poll record (written by the sync path) ───────────────────────
def _iso_or_none(value):
    """The car's own timestamp as ISO 8601, if it already is one.

    Connectors hand over whatever their vendor sends. A string that parses
    as an ISO date-time is passed through (normalised); anything else —
    epoch numbers, vendor formats — becomes ``None`` rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _load_poll(vid):
    from models.database import AppConfig
    raw = AppConfig.get(LAST_POLL_KEY.format(vid=vid), '') or ''
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _opt_bool(v):
    return None if v is None else bool(v)


def note_poll(vehicle_id, status) -> None:
    """Remember what the connector answered on a successful poll.

    Never raises — this sits inside the sync path.
    """
    if vehicle_id is None or status is None:
        return
    try:
        from models.database import AppConfig
        now = datetime.now().isoformat(timespec='seconds')
        rec = {
            'at': now,
            'ok': True,
            # when these values arrived — kept by a later failed attempt,
            # which only moves 'at'
            'ok_at': now,
            'soc_pct': getattr(status, 'soc_percent', None),
            'range_km': getattr(status, 'estimated_range_km', None),
            'charging': _opt_bool(getattr(status, 'is_charging', None)),
            'plugged_in': _opt_bool(getattr(status, 'is_plugged_in', None)),
            'charge_power_kw': getattr(status, 'charge_power_kw', None),
            'target_soc_pct': getattr(status, 'charge_limit_ac', None),
            'odometer_km': getattr(status, 'odometer_km', None),
            'data_ts': _iso_or_none(getattr(status, 'last_updated', None)),
        }
        AppConfig.set(LAST_POLL_KEY.format(vid=vehicle_id),
                      json.dumps(rec, default=str))
    except Exception as e:
        logger.debug(f"read-only API: last poll not recorded: {e}")
        try:
            from models.database import db
            db.session.rollback()
        except Exception:
            pass


def note_poll_failed(vehicle_id) -> None:
    """A poll of this car failed: keep the last good values, mark the attempt."""
    if vehicle_id is None:
        return
    try:
        from models.database import AppConfig
        rec = _load_poll(vehicle_id)
        rec['at'] = datetime.now().isoformat(timespec='seconds')
        rec['ok'] = False
        AppConfig.set(LAST_POLL_KEY.format(vid=vehicle_id),
                      json.dumps(rec, default=str))
    except Exception as e:
        logger.debug(f"read-only API: failed poll not recorded: {e}")
        try:
            from models.database import db
            db.session.rollback()
        except Exception:
            pass


# ── answer ────────────────────────────────────────────────────────────
def _local_iso(dt):
    """A naive local timestamp the app wrote with ``datetime.now()``,
    labelled with the local offset."""
    if dt is None:
        return None
    try:
        return dt.astimezone().isoformat(timespec='seconds')
    except (ValueError, OSError):
        return dt.isoformat(timespec='seconds')


def _local_iso_str(s):
    if not s:
        return None
    try:
        return _local_iso(datetime.fromisoformat(str(s)))
    except ValueError:
        return None


def _home_operator_names():
    """Every spelling the app uses for "charged at home" as an operator."""
    from models.database import AppConfig
    from services.i18n import SUPPORTED_LANGUAGES, _load_lang
    names = set()
    for code, _label in SUPPORTED_LANGUAGES:
        v = (_load_lang(code) or {}).get('set.op_home_private')
        if v:
            names.add(v.strip().lower())
    lbl = (AppConfig.get('home_label', '') or '').strip().lower()
    if lbl:
        names.add(lbl)
    return names


def _public_operator_names():
    try:
        from app import _DEFAULT_OPERATOR_BRANDS
    except Exception:
        return set()
    return {n.strip().lower() for n in _DEFAULT_OPERATOR_BRANDS}


def _location_kind(charge, home_names, public_names):
    """'home' / 'public' only where the app itself recorded it.

    * linked to a home-meter reading → it was measured at the home wallbox;
    * the operator is the app's "home / private" entry → home;
    * the operator is one of the built-in public charging networks → public.
    Anything else (work, "other", a custom name, nothing) → ``None``.
    """
    if getattr(charge, 'wallbox_charge_id', None):
        return 'home'
    op = (charge.operator or '').strip().lower()
    if not op:
        return None
    if op in home_names:
        return 'home'
    if op in public_names:
        return 'public'
    return None


def _charge_times(c):
    """(start, end) of a logged charge, as precise as it was stored.

    A charge linked to a home-meter reading has measured start and end
    times. Otherwise the charge stores a date plus the start and end hour;
    an end hour before the start hour means past midnight, the same rule
    the CO2 backfill uses.
    """
    if getattr(c, 'wallbox_charge_id', None):
        try:
            from models.database import WallboxCharge
            wc = WallboxCharge.query.get(c.wallbox_charge_id)
            if wc is not None and wc.start_ts and wc.end_ts:
                return (datetime.fromtimestamp(wc.start_ts).astimezone().isoformat(timespec='seconds'),
                        datetime.fromtimestamp(wc.end_ts).astimezone().isoformat(timespec='seconds'))
        except Exception:
            pass
    if c.date is None:
        return None, None
    if c.charge_hour is None:
        return c.date.isoformat(), None
    base = datetime(c.date.year, c.date.month, c.date.day)
    start = base.replace(hour=int(c.charge_hour))
    end = None
    if c.charge_end_hour is not None:
        off = 1 if c.charge_end_hour < c.charge_hour else 0
        end = base.replace(hour=int(c.charge_end_hour)) + timedelta(days=off)
    return _local_iso(start), _local_iso(end)


def _last_charge(vid, home_names, public_names):
    from models.database import Charge
    c = (Charge.query.filter(Charge.vehicle_id == vid)
         .order_by(Charge.date.desc(),
                   Charge.charge_hour.is_(None),
                   Charge.charge_hour.desc(),
                   Charge.id.desc())
         .first())
    if c is None:
        return None
    start, end = _charge_times(c)
    return {
        'start': start,
        'end': end,
        'kwh': c.kwh_loaded,
        'soc_from': c.soc_from,
        'soc_to': c.soc_to,
        'location_kind': _location_kind(c, home_names, public_names),
    }


def _vehicle_entry(v, active_id, home_names, public_names):
    from models.database import VehicleSync
    row = (VehicleSync.query.filter(VehicleSync.vehicle_id == v.id)
           .order_by(VehicleSync.timestamp.desc()).first())
    poll = _load_poll(v.id)
    has_values = any(k in poll for k in ('soc_pct', 'odometer_km'))
    if has_values:
        # The car's own timestamp where the connector gives one; otherwise
        # the moment this successful poll arrived. Never the time of a later
        # FAILED attempt — that one only moves last_sync_ts.
        values_at = poll.get('ok_at') or (poll.get('at') if poll.get('ok') else None)
        vals = {
            'soc_pct': poll.get('soc_pct'),
            'range_km': poll.get('range_km'),
            'charging': poll.get('charging'),
            'plugged_in': poll.get('plugged_in'),
            'charge_power_kw': poll.get('charge_power_kw'),
            'target_soc_pct': poll.get('target_soc_pct'),
            'odometer_km': poll.get('odometer_km'),
            'data_ts': poll.get('data_ts') or _local_iso_str(values_at),
            'data_source': 'poll',
        }
    elif row is not None:
        # No successful poll since the update (or since the vendor started
        # refusing): the newest stored status row is all there is. Its time
        # is when the app stored those values — so an assistant can say how
        # old they are instead of presenting them as current.
        vals = {
            'soc_pct': row.soc_percent,
            'range_km': row.estimated_range_km,
            'charging': _opt_bool(row.is_charging),
            'plugged_in': None,
            'charge_power_kw': row.charge_power_kw,
            'target_soc_pct': None,
            'odometer_km': row.odometer_km,
            'data_ts': _local_iso(row.timestamp),
            'data_source': 'stored',
        }
    else:
        vals = dict.fromkeys(('soc_pct', 'range_km', 'charging', 'plugged_in',
                              'charge_power_kw', 'target_soc_pct',
                              'odometer_km', 'data_ts', 'data_source'))
    if vals['charge_power_kw'] is not None:
        try:
            vals['charge_power_kw'] = float(vals['charge_power_kw'])
        except (TypeError, ValueError):
            vals['charge_power_kw'] = None
    entry = {
        'name': v.name,
        'brand': v.brand,
        'model': v.model,
        'active': v.id == active_id,
        'archived': bool(v.is_archived),
    }
    entry.update(vals)
    entry['last_sync_ts'] = _local_iso_str(poll.get('at'))
    entry['last_sync_ok'] = (bool(poll['ok']) if 'ok' in poll else None)
    entry['last_charge'] = _last_charge(v.id, home_names, public_names)
    return entry


def build_payload(version: str) -> dict:
    from models.database import Vehicle
    vehicles = Vehicle.query.order_by(Vehicle.id.asc()).all()
    # The car the picker shows when nobody has picked one: the first
    # non-archived vehicle (same rule as the picker and the legacy mirror).
    primary = next((v for v in vehicles if not v.is_archived),
                   vehicles[0] if vehicles else None)
    active_id = primary.id if primary is not None else None
    home_names = _home_operator_names()
    public_names = _public_operator_names()
    return {
        'ok': True,
        'version': version,
        'vehicles': [_vehicle_entry(v, active_id, home_names, public_names)
                     for v in vehicles],
    }
