# -*- coding: utf-8 -*-
"""Any car whose data already reaches Home Assistant.

Why this exists: some cars have no cloud API a private owner can use. The
XPENG G6 is one (October 2026: no public XPENG API, the Enode aggregator
does not take private sign-ups), and Kia/Hyundai Europe started refusing
third-party sign-ins on 14 October 2026. What these owners often DO have
is the car in Home Assistant — through the core Tibber integration (state
of charge, target, range, plug and charging state since HA 2026.9), an OBD
dongle such as the WiCAN Pro publishing over MQTT (state of charge,
odometer, pack current, 12 V), or whatever official integration comes
next. This connector reads those entities, so the tracker follows the car
without a vendor API of its own.

Credentials, in the three boxes the form already has:
  * ``username`` — the Home Assistant address, e.g. ``http://10.0.0.5:8123``
    (a container on the NAS may not resolve a ``*.ts.net`` name; the LAN
    address always works);
  * ``password`` — a long-lived access token (HA profile → Security);
  * ``vin``      — the shared start of the car's entity ids, e.g.
    ``xpeng_g6`` for ``sensor.xpeng_g6_soc``, ``sensor.xpeng_g6_odometer``,
    ``device_tracker.xpeng_g6``. Renaming entities in HA to one prefix is
    also what the WiCAN guide recommends.

Which entity means what is worked out from HA's own metadata — device
class and unit first, words in the id second — and the "Test connection"
button lists what it found, so a wrong guess is visible before it is
recorded. Values HA reports as ``unavailable``/``unknown`` are no reading.
Nothing is written to Home Assistant; the token is only used to read
``/api/states``.

Cars without a charging entity (the XPENG G6 over a WiCAN dongle)
----------------------------------------------------------------
WiCAN publishes the profile parameters in capitals — ``SOC``,
``ODOMETER``, ``HV_A`` (pack current), ``HV_V`` (pack voltage), optionally
``SPEED`` and ``SOH`` — and Home Assistant turns them into entities such as
``sensor.<device>_wican_<id>_local_hv_a``. Nobody publishes "is charging",
so it is worked out here from the pack current:

* **Sense.** The sources disagree on the sign of the XPENG pack current
  (221103, ``(B4*256+B5)*0.5-1600`` A). Negative = charging: stevelea/
  xpcardata (docs/XPENG_G6_PIDs.md, WICAN_SETUP.md, its Dart profile) and
  vondraussen/xpeng-wican-evcc (live value -9 while trickle-charging — but
  marked "unverified" there). Positive = charging: FranckH250/
  xpeng-home-assistant-wican (README, no measurement given). The majority
  is the default; the car itself settles it: when the state of charge keeps
  rising while the car stands and the current has the *other* sense, the
  sense is turned round for this car and remembered (``_learn``).
* **Charging is not "current flows into the pack".** Regeneration has the
  same sign as charging. xpcardata separates the two by speed ("speed=0 for
  2 consecutive samples"). With a speed entity: |A| >= 1 in the charging
  sense AND speed 0. Without one: the plug state confirms, or two
  consecutive polls both show the current and the odometer has not moved.
  A real ``charging``/``plug`` entity always wins.
* **Power** is |V x A| only when both are there and plausible. The 2026 LFP
  G6 reportedly does not answer HV_V at all (FranckH250); no voltage is
  invented, the power stays empty (see ``_to_status``).
* **Odometer.** An odometer only goes up: 0 after a positive value, a value
  below the last one, a jump no car can drive in the time that has passed
  (> 250 km/h on average) and the known false value 0x6201 = 25 089 (an
  answer shifted by its own service echo, wican-fw #371) are no reading.

The poll-to-poll memory lives in a small file per car (``STATE_DIR``),
because the connector object is rebuilt for every poll.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import VehicleConnector, VehicleStatus, plausible_percent, to_km
from .registry import register

logger = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:                          # pragma: no cover
    DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data')

STATE_DIR = os.path.join(DATA_DIR, 'homeassistant')

# ── inferring a charge from pack current ──────────────────────────────
CHARGE_MIN_A = 1.0              # |A| below this is idle consumption, not a charge
PACK_V_RANGE = (200.0, 900.0)   # a plausible traction pack (power = V x A)
PACK_A_MAX = 1000.0             # more than this is no reading
STILL_KMH = 0.5                 # "standing"
MIN_POLL_GAP_S = 60             # two polls closer than this are one observation
MAX_PREV_AGE_S = 6 * 3600       # an older previous poll says nothing
LEARN_VOTES = 2                 # observed SoC rises that turn the sense round
SOC_RISE_PCT = 1.0
# ── odometer plausibility ─────────────────────────────────────────────
ODO_MAX_KMH = 250.0
ODO_SLACK_KM = 1.0              # whole-km rounding
ODO_KNOWN_BAD = frozenset({0x6201})     # 25 089: echo "62 01" read as a value
ODO_REANCHOR = 3                # distinct, mutually consistent lower readings

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "Home-Assistant-Adresse", "type": "text",
     "label_key": "cred.ha_url", "help_key": "cred.help_ha_url"},
    {"key": "password", "label": "Langlebiger Zugriffstoken", "type": "password",
     "label_key": "cred.ha_token", "help_key": "cred.help_ha_token"},
    {"key": "vin", "label": "Namensanfang der Entitäten", "type": "text",
     "label_key": "cred.ha_prefix", "help_key": "cred.help_ha_prefix"},
]

DEFAULT_TIMEOUT = 20
_NO_VALUE = {'unavailable', 'unknown', 'none', '', 'null'}

# Words in an entity id, per role. Checked AFTER device class / unit, and
# only on the part after the prefix, so a car called "range_rover" does not
# turn every sensor into a range.
_WORDS = {
    'soc': ('soc', 'state_of_charge', 'battery_level', 'batterie', 'battery',
            'ladestand', 'charge_level', 'akku'),
    'target': ('target', 'ziel', 'limit', 'max_soc', 'charge_limit'),
    'range': ('range', 'reichweite', 'remaining', 'autonomie'),
    'odometer': ('odometer', 'odo', 'mileage', 'kilometerstand', 'km_stand', 'total_distance'),
    'charging': ('charging', 'laden', 'ladevorgang', 'lade_status', 'charge_state'),
    'plug': ('plug', 'stecker', 'connector', 'cable', 'kabel', 'plugged'),
    'power': ('charging_power', 'charge_power', 'ladeleistung', 'power'),
    'aux': ('12v', 'aux', 'starter', 'lv_battery', '12_v'),
    'lat': ('latitude', 'lat'),
    'lon': ('longitude', 'lon', 'lng'),
    'outside': ('outside_temp', 'aussentemp', 'ambient', 'exterior_temp'),
}
# Whole token sequences ("hv_a" must not match "hv_ac" or "hv_ambient").
# WiCAN names its parameters HV_A / HV_V; the rest are what other setups
# call the traction pack's current and voltage.
_PACK_CURRENT_WORDS = ('hv_a', 'hv_current', 'hvcurrent', 'pack_current', 'battery_current',
                       'bms_current', 'batt_current', 'hv_battery_current', 'packstrom',
                       'batteriestrom', 'akkustrom')
_PACK_VOLTAGE_WORDS = ('hv_v', 'hv_voltage', 'hvvoltage', 'pack_voltage', 'battery_voltage',
                       'bms_voltage', 'batt_voltage', 'hv_battery_voltage', 'packspannung',
                       'batteriespannung', 'akkuspannung')
_SPEED_WORDS = ('speed', 'geschwindigkeit', 'velocity', 'vehicle_speed')
_NOT_SPEED = ('max', 'limit', 'avg', 'average', 'fan', 'wind', 'target', 'set')
_SPEED_UNITS = {'km/h': 1.0, 'kph': 1.0, 'kmh': 1.0, 'km/hr': 1.0,
                'mph': 1.609344, 'mi/h': 1.609344, 'm/s': 3.6}
_NOT_RANGE = ('added', 'last', 'fast')         # "range added by the last charge" is no range
_NOT_SOC = _WORDS['target'] + _WORDS['aux'] + ('health', 'soh', 'temp', 'voltage', 'current')


def _t(key, **kw):
    try:
        from services.i18n import t
        return t(key, **kw)
    except Exception:                      # pragma: no cover
        return key


class HomeAssistantError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return v


def _has(text: str, words) -> bool:
    parts = set(re.split(r'[^a-z0-9]+', text))
    return any((w in parts) if '_' not in w else (w in text) for w in words)


def _seq(text: str, words) -> bool:
    """True when any of ``words`` occurs in ``text`` as whole tokens in a row."""
    toks = [t for t in re.split(r'[^a-z0-9]+', text) if t]
    for w in words:
        wt = w.split('_')
        n = len(wt)
        if any(toks[i:i + n] == wt for i in range(len(toks) - n + 1)):
            return True
    return False


def _is_pack_current(rest: str, dc: str, unit: str) -> bool:
    return ((unit.lower() == 'a' or dc == 'current')
            and unit.lower() in ('a', '')
            and _seq(rest, _PACK_CURRENT_WORDS)
            and not _has(rest, _WORDS['aux']))


def _is_pack_voltage(rest: str, dc: str, unit: str, state) -> bool:
    if not ((unit.lower() == 'v' or dc == 'voltage') and unit.lower() in ('v', '')):
        return False
    if not _seq(rest, _PACK_VOLTAGE_WORDS) or _has(rest, _WORDS['aux']):
        return False
    v = _num((state or {}).get('state'))
    return v is None or 60.0 <= v <= 1000.0       # a 12 V value is not the pack


def _is_speed(rest: str, dc: str, unit: str) -> bool:
    return (unit.lower() in _SPEED_UNITS
            and (dc == 'speed' or _has(rest, _SPEED_WORDS))
            and not _has(rest, _NOT_SPEED))


def _kmh(value, unit) -> Optional[float]:
    v = _num(value)
    f = _SPEED_UNITS.get(str(unit or 'km/h').strip().lower())
    return None if v is None or f is None or v < 0 else v * f


def _now() -> float:
    """Wall-clock seconds of a poll — a seam so tests can steer it."""
    return time.time()


def _is_on(state) -> Optional[bool]:
    s = str(state or '').strip().lower()
    if s in _NO_VALUE:
        return None
    if s in ('on', 'true', 'yes', 'charging', 'connected', 'plugged', 'plugged_in',
             'fastcharging', 'fast_charging', 'laden', 'lädt', 'eingesteckt', '1'):
        return True
    if s in ('off', 'false', 'no', 'idle', 'disconnected', 'unplugged', 'not_charging',
             'notcharging', 'complete', 'completed', 'stopped', 'ready', '0'):
        return False
    return None


# ── the community XPENG integration (HA custom component, XPENG G6) ──────
# It publishes ``sensor.<prefix>_charging_state`` as an enum with the words
# below. They are NOT the plug/charging words ``_is_on`` knows: "Connected"
# means cable in, not charging. ``Idle``/``Scheduled`` say nothing about the
# cable, ``Error`` nothing about anything.
_XP_STATE = {            # word -> (charging, plugged)
    'charging': (True, True),
    'connected': (False, True),
    'complete': (False, True),
    'disconnected': (False, False),
    'idle': (False, None),
    'scheduled': (False, None),
}


def _xp_charge_state(state):
    """``(charging, plugged)`` for an XPENG-integration charging-state word, else (None, None)."""
    return _XP_STATE.get(str(state or '').strip().lower(), (None, None))


def _is_xp_charge_state(attrs: dict) -> bool:
    """The enum sensor of the XPENG integration: its ``options`` carry its own words."""
    opts = {str(o).strip().lower() for o in (attrs.get('options') or [])}
    return {'charging', 'disconnected'} <= opts


def _utc_iso(value) -> Optional[str]:
    """An ISO 8601 stamp with an offset as ``YYYY-MM-DDTHH:MM:SS+00:00``, else None."""
    s = str(value or '').strip()
    if not s or s.lower() in _NO_VALUE:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None                     # a stamp without offset is a guess
    return dt.astimezone(timezone.utc).isoformat()


def _km(value, unit) -> Optional[float]:
    v = _num(value)
    if v is None:
        return None
    u = str(unit or 'km').strip().lower()
    if u in ('m', 'meter', 'meters'):
        return v / 1000.0
    if u in ('km', 'mi', 'mile', 'miles', ''):
        return to_km(v, u or 'km')
    return None


# ── what the car did at the previous poll ─────────────────────────────
# The connector object is rebuilt for every poll, so the previous poll is
# kept in a small file per car (like the Renault connector keeps its
# sign-in): keyed by a hash of address + entity prefix — nothing to put
# in a file name that is private — written atomically, mode 0600.

def _mem_path(base: str, prefix: str) -> str:
    key = hashlib.sha256(f'{base}|{prefix}'.encode()).hexdigest()[:32]
    return os.path.join(STATE_DIR, key + '.json')


def _mem_load(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _mem_save(path: str, data: dict) -> None:
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        tmp = path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:                    # pragma: no cover - e.g. Windows
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:                   # a read-only data folder must not break a poll
        logger.warning(f"Home Assistant: could not remember the last poll ({e})")


def _check_odometer(mem: dict, km, stamp, now: float):
    """``(km or None, reason or None)`` — only plausible readings pass.

    An odometer only goes up, so the car's own last accepted reading is the
    judge: no 0 after a positive value, nothing below it, no step a car
    cannot drive in the time since (``ODO_MAX_KMH`` on average, plus one
    rounding km), and never the known false value 0x6201. A reading that
    is lower than the last one but agrees with its own predecessors for
    ``ODO_REANCHOR`` distinct Home Assistant updates is taken as the new
    truth — otherwise one wrongly accepted high value would lock every
    later, correct reading out for good.
    """
    if km is None:
        return None, None
    last = mem.get('odo')
    reason = None
    if round(km) in ODO_KNOWN_BAD:
        reason = 'known_bad'
    elif last and km <= 0 and last['km'] > 0:
        reason = 'zero'
    elif last and km < last['km'] - ODO_SLACK_KM:
        reason = 'backwards'
    elif last and km - last['km'] > ODO_MAX_KMH * max(0.0, now - last['t']) / 3600.0 + ODO_SLACK_KM:
        reason = 'jump'
    if reason is None:
        if km > 0:                          # a lone 0 is no baseline to judge by
            mem['odo'] = {'km': km, 't': now}
        mem.pop('odo_rej', None)
        return km, None
    if reason == 'backwards':
        rej = mem.get('odo_rej')
        if (rej and stamp and stamp != rej.get('stamp')
                and rej['km'] - ODO_SLACK_KM <= km
                <= rej['km'] + ODO_MAX_KMH * max(0.0, now - rej['t']) / 3600.0 + ODO_SLACK_KM):
            rej = {'km': km, 't': now, 'stamp': stamp, 'n': rej.get('n', 1) + 1}
        else:
            rej = {'km': km, 't': now, 'stamp': stamp, 'n': 1}
        mem['odo_rej'] = rej
        if rej['n'] >= ODO_REANCHOR:
            logger.warning(f"Home Assistant: odometer now reads {km:.0f} km, below the earlier "
                           f"{last['km']:.0f} km, on {rej['n']} separate updates — taking it as the real one")
            mem['odo'] = {'km': km, 't': now}
            mem.pop('odo_rej', None)
            return km, None
    logger.warning(f"Home Assistant: odometer reading {km:.0f} km ignored ({reason}"
                   f"{'' if not last else ', last accepted %.0f km' % last['km']})")
    return None, reason


def _mem_dirty(mem: dict, before: str) -> bool:
    return json.dumps(mem, sort_keys=True) != before


def _sense(current, sign) -> int:
    """+1 = current flows in the charging sense, -1 = the other way, 0 = idle."""
    if current is None:
        return 0
    c = current * sign
    return 1 if c >= CHARGE_MIN_A else -1 if c <= -CHARGE_MIN_A else 0


def _observe(mem: dict, now: float, *, soc, odo, cur, speed, plug, real_charging) -> dict:
    """Charging from pack current, with the previous poll as witness.

    Updates ``mem`` (previous poll, learned sense) and returns what was
    decided: ``{'charging': True/False/None, 'mode': ..., 'sign': ...}``
    (``None`` = leave it to the real entity).
    """
    sign = 1 if mem.get('sign', -1) > 0 else -1          # sign of the current WHILE charging
    prev = mem.get('prev')
    if not isinstance(prev, dict) or not 0 <= now - prev.get('t', -1e12) <= MAX_PREV_AGE_S:
        prev = None
    spaced = prev is not None and now - prev['t'] >= MIN_POLL_GAP_S
    still = None if speed is None else speed <= STILL_KMH

    # Learn the sense from the car: it stands, the state of charge climbs,
    # and the current says "not charging" — twice. Then the assumed sense
    # is wrong for this car.
    if spaced and not real_charging and cur is not None:
        pcur, psoc = prev.get('cur'), prev.get('soc')
        both_still = ((still is True and prev.get('still') is True)
                      or (speed is None and odo is not None and odo == prev.get('odo')))
        if both_still and pcur is not None and soc is not None and psoc is not None:
            if _sense(cur, sign) == -1 and _sense(pcur, sign) == -1 and soc - psoc >= SOC_RISE_PCT:
                mem['votes'] = mem.get('votes', 0) + 1
                if mem['votes'] >= LEARN_VOTES:
                    sign = -sign
                    mem.update(sign=sign, sign_learned=True, votes=0)
                    logger.warning("Home Assistant: the state of charge rose twice while the car "
                                   "stood, with the pack current in the other sense — a current "
                                   f"{'above' if sign > 0 else 'below'} zero now means charging "
                                   "for this car (remembered)")
            elif _sense(cur, sign) == 1:
                mem['votes'] = 0

    out = {'charging': None, 'mode': 'entity' if real_charging else None, 'sign': sign,
           'learned': bool(mem.get('sign_learned'))}
    if not real_charging:
        s_now = _sense(cur, sign)
        if plug is False:
            out.update(charging=False, mode='unplugged')
        elif s_now != 1:
            out.update(charging=False, mode='no_charge_current' if cur is not None else 'no_current')
        elif still is True:
            out.update(charging=True, mode='current+standstill')
        elif still is False:
            out.update(charging=False, mode='driving')           # regeneration
        elif plug is True:
            out.update(charging=True, mode='current+plug')
        elif (spaced and odo is not None and odo == prev.get('odo')
              and _sense(prev.get('cur'), sign) == 1):
            out.update(charging=True, mode='current+two_polls')
        else:
            out.update(charging=False, mode='waiting_for_second_poll')

    if prev is None or spaced:
        mem['prev'] = {'t': now, 'odo': odo, 'soc': soc, 'cur': cur, 'still': still}
    return out


class HomeAssistantConnector(VehicleConnector):

    # ── plumbing ──────────────────────────────────────────────────────
    def _base(self) -> str:
        url = (self.credentials.get('username') or '').strip().rstrip('/')
        if url and not re.match(r'^https?://', url):
            url = 'http://' + url
        if url.endswith('/api'):
            url = url[:-4]
        return url

    def _token(self) -> str:
        return (self.credentials.get('password') or '').strip()

    def _prefix(self) -> str:
        p = (self.credentials.get('vin') or '').strip().lower()
        p = p.split('.', 1)[-1]                # "sensor.xpeng_g6" → "xpeng_g6"
        return p

    def _get(self, path: str):
        base, token = self._base(), self._token()
        if not base or not token:
            raise HomeAssistantError('no_key', _t('ha.err_need_url_token'))
        req = urllib.request.Request(base + path, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
            'User-Agent': 'ev-charge-tracker'})
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise HomeAssistantError('token', _t('ha.err_token'))
            raise HomeAssistantError('http', _t('ha.err_http', status=e.code))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, 'reason', e)
            raise HomeAssistantError('network', _t('ha.err_network', url=base,
                                                   reason=str(reason)[:120]))
        except ValueError:
            raise HomeAssistantError('http', _t('ha.err_http', status=200))

    def _states(self) -> list:
        prefix = self._prefix()
        if not prefix:
            raise HomeAssistantError('no_prefix', _t('ha.err_need_prefix'))
        data = self._get('/api/states')
        if not isinstance(data, list):
            raise HomeAssistantError('http', _t('ha.err_http', status=200))
        out = []
        for st in data:
            eid = str((st or {}).get('entity_id') or '')
            domain, _, obj = eid.partition('.')
            if domain in ('sensor', 'binary_sensor', 'device_tracker', 'switch') and obj.startswith(prefix):
                out.append(st)
        return out

    # ── which entity is what ──────────────────────────────────────────
    def map_entities(self, states: list) -> dict:
        """``{role: state}`` for the roles we can fill. Metadata first."""
        prefix = self._prefix()
        roles: dict = {}

        def take(role, st):
            roles.setdefault(role, st)

        # Pass 1: device classes and units say it outright.
        for st in states:
            eid = st['entity_id']
            domain, _, obj = eid.partition('.')
            rest = obj[len(prefix):].strip('_') or obj
            attrs = st.get('attributes') or {}
            dc = str(attrs.get('device_class') or '').lower()
            unit = str(attrs.get('unit_of_measurement') or '').strip()
            if domain == 'device_tracker':
                if _num(attrs.get('latitude')) is not None and _num(attrs.get('longitude')) is not None:
                    take('location', st)
                continue
            if domain == 'switch':          # the XPENG integration's compact layout: the charging switch
                if rest == 'charging':
                    take('charging', st)
                continue
            if domain == 'binary_sensor':
                if dc == 'battery_charging':
                    take('charging', st)
                elif dc == 'plug':
                    take('plug', st)
                elif rest == 'credentials_valid':
                    take('credentials_valid', st)
                continue
            if _is_xp_charge_state(attrs):      # enum with the integration's own words
                take('charging_state', st)
                continue
            if rest == 'last_data_update' and dc == 'timestamp':
                take('data_time', st)           # when the CAR last reported, not when HA looked
                continue
            if rest == 'polling_status':
                take('polling_status', st)
                continue
            if dc == 'battery' and unit == '%' and not _has(rest, _NOT_SOC):
                take('soc', st)
            elif dc in ('distance',) and unit.lower() in ('km', 'mi', 'm') \
                    and _has(rest, _WORDS['odometer']):
                take('odometer', st)
            elif dc in ('distance',) and unit.lower() in ('km', 'mi', 'm') \
                    and _has(rest, _WORDS['range']) and not _has(rest, _NOT_RANGE):
                take('range', st)
            elif dc == 'power' and _has(rest, ('charging', 'charge', 'lade', 'ladeleistung')):
                take('power', st)
            elif _is_pack_current(rest, dc, unit):
                take('pack_current', st)
            elif _is_pack_voltage(rest, dc, unit, st):
                take('pack_voltage', st)
            elif _is_speed(rest, dc, unit):
                take('speed', st)

        # Pass 2: words, for entities without (useful) metadata — MQTT
        # sensors from an OBD dongle typically carry only a unit.
        for st in states:
            eid = st['entity_id']
            domain, _, obj = eid.partition('.')
            if domain not in ('sensor', 'binary_sensor'):
                continue
            rest = obj[len(prefix):].strip('_') or obj
            attrs = st.get('attributes') or {}
            if _is_xp_charge_state(attrs) or rest in ('last_data_update', 'polling_status',
                                                      'credentials_valid'):
                continue
            unit = str(attrs.get('unit_of_measurement') or '').strip().lower()
            if unit == '%' and _has(rest, _WORDS['target']):
                take('target', st)
            elif unit == '%' and _has(rest, _WORDS['soc']) and not _has(rest, _NOT_SOC):
                take('soc', st)
            elif unit in ('km', 'mi', 'm') and _has(rest, _WORDS['odometer']):
                take('odometer', st)
            elif unit in ('km', 'mi', 'm') and _has(rest, _WORDS['range']) \
                    and not _has(rest, _NOT_RANGE):
                take('range', st)
            elif unit in ('w', 'kw') and _has(rest, _WORDS['power']):
                take('power', st)
            elif unit == 'v' and _has(rest, _WORDS['aux']):
                take('aux_v', st)
            elif _is_pack_current(rest, '', unit):
                take('pack_current', st)
            elif _is_pack_voltage(rest, '', unit, st):
                take('pack_voltage', st)
            elif _is_speed(rest, '', unit):
                take('speed', st)
            elif unit in ('°c', '°f') and _has(rest, _WORDS['outside']):
                take('outside', st)
            elif _has(rest, _WORDS['plug']):
                take('plug', st)
            elif _has(rest, _WORDS['charging']) and unit in ('', None):
                take('charging', st)
            elif _has(rest, _WORDS['lat']) and _num(st.get('state')) is not None:
                take('lat', st)
            elif _has(rest, _WORDS['lon']) and _num(st.get('state')) is not None:
                take('lon', st)
        return roles

    def describe(self, roles: dict) -> str:
        names = [('soc', 'ha.role_soc'), ('odometer', 'ha.role_odometer'),
                 ('range', 'ha.role_range'), ('charging', 'ha.role_charging'),
                 ('plug', 'ha.role_plug'), ('location', 'ha.role_location'),
                 ('pack_current', 'ha.role_pack_current'),
                 ('pack_voltage', 'ha.role_pack_voltage'), ('speed', 'ha.role_speed')]
        parts = []
        for role, key in names:
            st = roles.get(role)
            if role == 'charging' and not st:
                st = roles.get('charging_state')
            if role == 'location' and not st and roles.get('lat') and roles.get('lon'):
                st = roles['lat']
            parts.append(f"{_t(key)}: {st['entity_id'] if st else '—'}")
        return ' · '.join(parts)

    # ── the interface ─────────────────────────────────────────────────
    def authenticate(self) -> bool:
        try:
            self._get('/api/')
            return True
        except HomeAssistantError as e:
            logger.error(f"Home Assistant sign-in failed ({e.kind})")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def verify_credentials(self) -> str:
        """Raise with the reason, or return what was found — the test
        button shows it, so a wrong entity is noticed before it counts."""
        self._get('/api/')
        roles = self.map_entities(self._states())
        if not roles.get('soc') and not roles.get('odometer'):
            raise HomeAssistantError('nothing', _t('ha.err_nothing_found', prefix=self._prefix()))
        return self.describe(roles)

    def get_status(self, force=False) -> VehicleStatus:
        """``force`` cannot do anything here: HA holds what it last got."""
        states = self._states()
        roles = self.map_entities(states)
        if not roles.get('soc') and not roles.get('odometer'):
            raise HomeAssistantError('nothing', _t('ha.err_nothing_found', prefix=self._prefix()))
        return self._to_status(roles)

    # ── mapping ───────────────────────────────────────────────────────
    @staticmethod
    def _val(st):
        if not st:
            return None
        s = st.get('state')
        return None if str(s).strip().lower() in _NO_VALUE else s

    @staticmethod
    def _unit(st):
        return ((st or {}).get('attributes') or {}).get('unit_of_measurement')

    def _to_status(self, roles: dict) -> VehicleStatus:
        v, u = self._val, self._unit
        now = _now()
        path = _mem_path(self._base(), self._prefix())
        mem = _mem_load(path)
        before = json.dumps(mem, sort_keys=True)

        soc = plausible_percent(_num(v(roles.get('soc'))))
        target = plausible_percent(_num(v(roles.get('target'))))
        odo_read = _km(v(roles.get('odometer')), u(roles.get('odometer')))
        odo, odo_rejected = _check_odometer(
            mem, odo_read, (roles.get('odometer') or {}).get('last_updated'), now)
        rng = _km(v(roles.get('range')), u(roles.get('range')))

        cur = _num(v(roles.get('pack_current')))
        if cur is not None and abs(cur) > PACK_A_MAX:
            cur = None
        volt = _num(v(roles.get('pack_voltage')))
        if volt is not None and not PACK_V_RANGE[0] <= volt <= PACK_V_RANGE[1]:
            volt = None
        speed = _kmh(v(roles.get('speed')), u(roles.get('speed')))

        charging = _is_on(v(roles.get('charging')))
        plugged = _is_on(v(roles.get('plug')))
        # the XPENG integration's own state word fills what the switch / plug entity
        # leave open (the plug entity is "unknown" while a schedule waits).
        cs_charging, cs_plugged = _xp_charge_state(v(roles.get('charging_state')))
        if charging is None:
            charging = cs_charging
        if plugged is None:
            plugged = cs_plugged
        power = _num(v(roles.get('power')))
        if power is not None and str(u(roles.get('power')) or '').lower() == 'w':
            power = power / 1000.0
        if charging is None and power is not None:
            charging = power > 0.2
        # No charging entity and no power entity: work it out from the pack
        # current (see the module note). A real entity always wins.
        inferred = _observe(mem, now, soc=soc, odo=odo, cur=cur, speed=speed, plug=plugged,
                            real_charging=charging is not None)
        if charging is None and inferred['charging'] is not None:
            charging = inferred['charging']
        if charging and plugged is None:
            plugged = True

        # Charge power: a real power entity first; else |V x A| when both
        # are there. Without the pack voltage (the 2026 LFP G6 reportedly
        # does not answer it) it stays EMPTY: a nominal voltage would be a
        # guess presented as a reading, and AC power is not pack V x A
        # anyway. Never shown for a car that is not charging — a -1.5 A
        # idle draw is not a charge power.
        if power is None and charging and cur is not None and volt is not None:
            power = abs(volt * cur) / 1000.0

        if _mem_dirty(mem, before):
            _mem_save(path, mem)

        lat = lon = None
        loc = roles.get('location')
        if loc:
            a = loc.get('attributes') or {}
            lat, lon = _num(a.get('latitude')), _num(a.get('longitude'))
        elif roles.get('lat') and roles.get('lon'):
            lat, lon = _num(v(roles['lat'])), _num(v(roles['lon']))
        if (lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180)
                or (lat == 0 and lon == 0)):
            lat = lon = None

        used = [st for st in roles.values() if st]
        stamps = [str(st.get('last_updated') or st.get('last_changed') or '') for st in used]
        # Freshness: the integration says when the car last reported. HA's own
        # last_updated only says when HA wrote the state, which a frozen car
        # (sign-in lost) keeps refreshing. Without that entity, HA's stamp stays.
        data_iso = _utc_iso(v(roles.get('data_time')))
        last_updated = data_iso if roles.get('data_time') else (max(stamps) if any(stamps) else None)
        # The GPS fix time: the tracker's own age of the fix, counted back from
        # the moment HA wrote that state.
        fix_ts = None
        if loc and lat is not None:
            fa = _num((loc.get('attributes') or {}).get('fix_age_s'))
            written = _utc_iso(loc.get('last_updated'))
            if fa is not None and fa >= 0 and written:
                from datetime import datetime, timedelta
                fix_ts = (datetime.fromisoformat(written) - timedelta(seconds=fa)).isoformat()
        creds = _is_on(v(roles.get('credentials_valid')))
        units_seen = {str(u(roles.get(r)) or '').lower() for r in ('odometer', 'range') if roles.get(r)}

        outside = _num(v(roles.get('outside')))
        if outside is not None and str(u(roles.get('outside')) or '') == '°F':
            outside = round((outside - 32) * 5 / 9, 1)

        status = VehicleStatus(
            soc_percent=int(round(soc)) if soc is not None else None,
            odometer_km=int(odo) if odo is not None else None,
            is_charging=bool(charging),
            is_plugged_in=bool(plugged),
            charge_power_kw=round(power, 2) if power is not None and power >= 0 else None,
            estimated_range_km=int(round(rng)) if rng is not None else None,
            charge_limit_ac=int(target) if target is not None else None,
            location_lat=lat,
            location_lon=lon,
            last_updated=last_updated,
            vehicle_name=self._prefix(),
            distance_unit=('mi' if units_seen & {'mi', 'mile', 'miles'} else
                           'km' if units_seen & {'km'} else None),
            raw_data={
                'source': 'homeassistant',
                'entities': {role: st['entity_id'] for role, st in roles.items() if st},
                'states': {role: v(st) for role, st in roles.items() if st},
                'aux_battery_v': _num(v(roles.get('aux_v'))),
                'pack_current_a': cur,
                'pack_voltage_v': volt,
                'speed_kmh': speed,
                'odometer_rejected': odo_rejected,
                # What decided "charging" and which sense of the pack
                # current counts as charging for this car (learned or
                # the default): visible, so a wrong call can be traced.
                'charge_inference': {
                    'mode': inferred['mode'],
                    'charging_current_sign': 'positive' if inferred['sign'] > 0 else 'negative',
                    'sign_learned': inferred['learned'],
                },
                'outside_temperature_c': outside,
                'lock_status_known': False,
                # XPENG integration: is the car's account still signed in, and what
                # does the integration say about its own polling.
                'credentials_valid': creds,
                'polling_status': v(roles.get('polling_status')),
                'charge_state_word': v(roles.get('charging_state')),
                'data_time': data_iso,
            },
        )
        if fix_ts:
            status.location_last_updated_at = fix_ts
        return status

    # ── UI metadata ───────────────────────────────────────────────────
    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return 'Home Assistant'


register('homeassistant', HomeAssistantConnector)
