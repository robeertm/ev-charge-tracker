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
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from .base import VehicleConnector, VehicleStatus, plausible_percent, to_km
from .registry import register

logger = logging.getLogger(__name__)

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
            if domain in ('sensor', 'binary_sensor', 'device_tracker') and obj.startswith(prefix):
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
            if domain == 'binary_sensor':
                if dc == 'battery_charging':
                    take('charging', st)
                elif dc == 'plug':
                    take('plug', st)
                continue
            if dc == 'battery' and unit == '%' and not _has(rest, _NOT_SOC):
                take('soc', st)
            elif dc in ('distance',) and unit.lower() in ('km', 'mi', 'm') \
                    and _has(rest, _WORDS['odometer']):
                take('odometer', st)
            elif dc in ('distance',) and unit.lower() in ('km', 'mi', 'm') \
                    and _has(rest, _WORDS['range']):
                take('range', st)
            elif dc == 'power' and _has(rest, ('charging', 'charge', 'lade', 'ladeleistung')):
                take('power', st)

        # Pass 2: words, for entities without (useful) metadata — MQTT
        # sensors from an OBD dongle typically carry only a unit.
        for st in states:
            eid = st['entity_id']
            domain, _, obj = eid.partition('.')
            if domain == 'device_tracker':
                continue
            rest = obj[len(prefix):].strip('_') or obj
            attrs = st.get('attributes') or {}
            unit = str(attrs.get('unit_of_measurement') or '').strip().lower()
            if unit == '%' and _has(rest, _WORDS['target']):
                take('target', st)
            elif unit == '%' and _has(rest, _WORDS['soc']) and not _has(rest, _NOT_SOC):
                take('soc', st)
            elif unit in ('km', 'mi', 'm') and _has(rest, _WORDS['odometer']):
                take('odometer', st)
            elif unit in ('km', 'mi', 'm') and _has(rest, _WORDS['range']):
                take('range', st)
            elif unit in ('w', 'kw') and _has(rest, _WORDS['power']):
                take('power', st)
            elif unit == 'v' and _has(rest, _WORDS['aux']):
                take('aux_v', st)
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
                 ('plug', 'ha.role_plug'), ('location', 'ha.role_location')]
        parts = []
        for role, key in names:
            st = roles.get(role)
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
        soc = plausible_percent(_num(v(roles.get('soc'))))
        target = plausible_percent(_num(v(roles.get('target'))))
        odo = _km(v(roles.get('odometer')), u(roles.get('odometer')))
        rng = _km(v(roles.get('range')), u(roles.get('range')))

        charging = _is_on(v(roles.get('charging')))
        plugged = _is_on(v(roles.get('plug')))
        power = _num(v(roles.get('power')))
        if power is not None and str(u(roles.get('power')) or '').lower() == 'w':
            power = power / 1000.0
        if charging is None and power is not None:
            charging = power > 0.2
        if charging and plugged is None:
            plugged = True

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
        units_seen = {str(u(roles.get(r)) or '').lower() for r in ('odometer', 'range') if roles.get(r)}

        outside = _num(v(roles.get('outside')))
        if outside is not None and str(u(roles.get('outside')) or '') == '°F':
            outside = round((outside - 32) * 5 / 9, 1)

        return VehicleStatus(
            soc_percent=int(round(soc)) if soc is not None else None,
            odometer_km=int(odo) if odo is not None else None,
            is_charging=bool(charging),
            is_plugged_in=bool(plugged),
            charge_power_kw=round(power, 2) if power is not None and power >= 0 else None,
            estimated_range_km=int(round(rng)) if rng is not None else None,
            charge_limit_ac=int(target) if target is not None else None,
            location_lat=lat,
            location_lon=lon,
            last_updated=max(stamps) if any(stamps) else None,
            vehicle_name=self._prefix(),
            distance_unit=('mi' if units_seen & {'mi', 'mile', 'miles'} else
                           'km' if units_seen & {'km'} else None),
            raw_data={
                'source': 'homeassistant',
                'entities': {role: st['entity_id'] for role, st in roles.items() if st},
                'states': {role: v(st) for role, st in roles.items() if st},
                'aux_battery_v': _num(v(roles.get('aux_v'))),
                'outside_temperature_c': outside,
                'lock_status_known': False,
            },
        )

    # ── UI metadata ───────────────────────────────────────────────────
    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return 'Home Assistant'


register('homeassistant', HomeAssistantConnector)
