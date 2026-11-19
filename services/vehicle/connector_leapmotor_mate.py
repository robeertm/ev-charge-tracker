# -*- coding: utf-8 -*-
"""Leapmotor cars, read directly from a Leapmotor Mate install (experimental).

Leapmotor has no public API a private owner can use. The open-source
project *Leapmotor Mate* (AGPL-3.0, a self-hosted web app, usually a
container on port 4000) signs in to the manufacturer's cloud itself and
keeps every reading in its own database. Owners who run Mate without Home
Assistant and without an MQTT broker still have one thing: Mate's web
server on their network. This connector reads from there, and nowhere
else:

* ``GET /healthz`` — is this a Mate, has it been set up, is its poller
  alive. Public in Mate, used by the "Test connection" button.
* ``GET /api/export/charges.csv`` — Mate's finished charges (its own
  detection: start/end time, SoC, energy, AC/DC, peak power, place). The
  same file Mate offers under Settings as "Export charges".
* ``GET /api/export/trips.csv`` — its finished trips, for the latest
  odometer and the SoC at the end of the last drive.
* ``GET /api/charging-chart`` — the chart of the charge in progress. Mate
  answers with an empty panel while no charge runs, so a non-empty answer
  means "charging now"; the live SoC and power are taken from the chart's
  data when they can be read, and left empty when they cannot.

None of these makes Mate call the manufacturer's cloud — they read what
Mate has already stored, so polling them never wakes the car and costs
Leapmotor nothing. Nothing is ever written to Mate.

Credentials, in the boxes the form already has:
  * ``username`` — Mate's address, e.g. ``http://192.168.1.20:4000``.
    Mate without a password answers only to home-network names (an IP
    address, a single-label name, ``.local``/``.lan`` …), which is what a
    container on the same NAS uses anyway.
  * ``password`` — Mate's own login password, only if one is set in Mate
    (Settings → Access, or ``MATE_AUTH_PASSWORD``). Optional.

Charges are filed by ``services.leapmotor_mate_import`` with a marker
``leapmotor-mate:<id>`` so the same charge is never filed twice. A car on
this connector does not run the tracker's own snapshot charge detection
(``records_own_charges``): Mate measured the charge from the car's own
signals, a guess from two snapshots could only add a duplicate.

Why the B10 needs guards
------------------------
Mate is maintained for several models and its CSV is a view of its own
database, not a stable API. So every value is checked before it is used:
a SoC outside 0..100 is no reading (the sentinel problem, see
``base.plausible_percent``), an odometer of 0 or one that runs backwards is
ignored, a "charge" whose SoC fell or that added no energy is not a charge
(V2L — the B10 can power devices from its battery, and that energy flows
the other way), and a column Mate renamed is a missing value, never a
crash. Energy is taken in the order the owner can trust most: the
charger's own figure typed into Mate, then the wallbox meter Mate read,
then the battery side the car reported (with a note saying so).
"""
from __future__ import annotations

import csv
import hashlib
import http.cookies
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from .base import VehicleConnector, VehicleStatus, plausible_percent
from .registry import register

logger = logging.getLogger(__name__)

BRAND_KEY = 'leapmotor_mate'
DEFAULT_TIMEOUT = 25
COOKIE_NAME = 'mate_session'
MAX_CSV_BYTES = 40 * 1024 * 1024      # a decade of trips is a few MB; anything bigger is not Mate
ODO_MAX_KM = 2_000_000
CHARGE_MAX_KW = 400.0                 # no B10/C10/T03 charges faster; a bigger peak is a unit slip

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "Leapmotor-Mate-Adresse", "type": "text",
     "label_key": "cred.mate_url", "help_key": "cred.help_mate_url"},
    {"key": "password", "label": "Mate-Passwort", "type": "password",
     "label_key": "cred.mate_password", "help_key": "cred.help_mate_password",
     "optional": True},
]


def _t(key, **kw):
    try:
        from services.i18n import t
        return t(key, **kw)
    except Exception:                      # pragma: no cover
        return key


class MateError(RuntimeError):
    """A failure with a reason the owner can act on (shown by the test button)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# ── value parsing ─────────────────────────────────────────────────────
def _num(value) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().lstrip("'")
    if not s or s.lower() in ('none', 'null', 'nan', 'n/a', '-'):
        return None
    try:
        v = float(s.replace(',', '.')) if s.count(',') == 1 and '.' not in s else float(s)
    except ValueError:
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return v


def _text(value) -> str:
    """A text cell as Mate meant it: its export puts an apostrophe in front
    of anything a spreadsheet would run as a formula."""
    s = str(value or '').strip()
    if len(s) > 1 and s[0] == "'" and s[1] in '=+-@':
        s = s[1:]
    return s


def _epoch(value) -> Optional[int]:
    """Unix seconds from Mate's ISO time. Mate writes local time WITH its
    offset, so the instant is exact whatever zone either side runs in; a
    naive value is read as the tracker's local time."""
    s = _text(value)
    if not s:
        return None
    s = s.replace(' ', 'T', 1) if 'T' not in s else s
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    try:
        return int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _soc(value) -> Optional[float]:
    v = _num(value)
    if v is None:
        return None
    return v if plausible_percent(v) is not None else None


def _odo(value) -> Optional[float]:
    v = _num(value)
    if v is None or v <= 0 or v > ODO_MAX_KM:
        return None
    return v


def _read_csv(text: str) -> list:
    text = text.lstrip('﻿')
    if not text.strip():
        return []
    try:
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    except csv.Error as e:
        raise MateError('format', _t('mate.err_format', reason=str(e)[:80]))


def parse_charges(rows: list) -> tuple:
    """``(sessions, skipped)`` from Mate's charges export.

    A session: ``{'id', 'start', 'end', 'soc_from', 'soc_to', 'kwh',
    'kwh_source', 'type', 'peak_kw', 'lat', 'lon', 'place', 'duration'}``.
    ``skipped`` counts rows that are not a charge the tracker can file
    (still open, no times, SoC fell, nothing added)."""
    out, skipped = [], 0
    for r in rows:
        start, end = _epoch(r.get('started_at')), _epoch(r.get('ended_at'))
        if not start or not end or end < start:
            skipped += 1
            continue
        soc_from, soc_to = _soc(r.get('start_soc')), _soc(r.get('end_soc'))
        battery_kwh = _num(r.get('energy_added_kwh'))
        meter_kwh = _num(r.get('ac_energy_kwh'))
        charger_kwh = _num(r.get('gross_kwh'))
        kwh, source = None, None
        for value, name in ((charger_kwh, 'charger'), (meter_kwh, 'wallbox'),
                            (battery_kwh, 'battery')):
            if value is not None and value > 0:
                kwh, source = value, name
                break
        # V2L and other discharges: energy left the pack. Never a charge.
        if soc_from is not None and soc_to is not None and soc_to < soc_from:
            skipped += 1
            continue
        if kwh is None and (soc_from is None or soc_to is None or soc_to <= soc_from):
            skipped += 1
            continue
        peak = _num(r.get('max_power_kw'))
        if peak is not None and (peak <= 0 or peak > CHARGE_MAX_KW):
            peak = None
        ctype = _text(r.get('charge_type')).upper()
        if ctype not in ('AC', 'DC'):
            ctype = 'DC' if (peak or 0) >= 22.5 else 'AC'
        lat, lon = _num(r.get('latitude')), _num(r.get('longitude'))
        if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6) \
                or abs(lat) > 90 or abs(lon) > 180:
            lat = lon = None
        try:
            rid = int(_num(r.get('id')) or 0) or None
        except (TypeError, ValueError):
            rid = None
        out.append({
            'id': rid, 'start': start, 'end': end, 'duration': end - start,
            'soc_from': soc_from, 'soc_to': soc_to,
            'kwh': round(kwh, 3) if kwh is not None else None, 'kwh_source': source,
            'type': ctype, 'peak_kw': round(peak, 1) if peak is not None else None,
            'lat': lat, 'lon': lon, 'place': _text(r.get('place'))[:200] or None,
            # Newer Mate versions write the odometer at the charge; older ones
            # do not, and attach_odometer() takes it from the drive before.
            'odometer': int(_odo(r.get('odometer_km'))) if _odo(r.get('odometer_km')) else None,
        })
    out.sort(key=lambda s: s['start'])
    return out, skipped


TRIP_MAX_KM = 3000.0                  # one drive; a bigger figure is a unit slip
TRIP_MAX_MINUTES = 24 * 60


def parse_trips(rows: list) -> tuple:
    """``(trips, skipped)`` from Mate's trips export.

    A trip: ``{'id', 'start', 'end', 'minutes', 'km', 'avg_kmh', 'soc_from',
    'soc_to', 'odo_from', 'odo_to', 'place_from', 'place_to'}``. Mate's
    export carries no coordinates, only the place names as text
    (``start_place``/``end_place``, each cut to 120 characters; a missing
    or renamed column is ``None``). ``skipped`` counts rows that are no
    usable drive (still open, no times, no distance)."""
    out, skipped = [], 0
    for r in rows:
        start, end = _epoch(r.get('started_at')), _epoch(r.get('ended_at'))
        if not start or not end or end < start or end - start > TRIP_MAX_MINUTES * 60:
            skipped += 1
            continue
        odo_from, odo_to = _odo(r.get('start_odometer_km')), _odo(r.get('end_odometer_km'))
        km = _num(r.get('distance_km'))
        if (km is None or km <= 0) and odo_from is not None and odo_to is not None \
                and odo_to > odo_from:
            km = odo_to - odo_from
        if km is None or km <= 0 or km > TRIP_MAX_KM:
            skipped += 1
            continue
        minutes = int(round((end - start) / 60.0))
        try:
            rid = int(_num(r.get('id')) or 0) or None
        except (TypeError, ValueError):
            rid = None
        out.append({
            'id': rid, 'start': start, 'end': end, 'minutes': minutes,
            'km': round(km, 1),
            'avg_kmh': round(km / (minutes / 60.0), 1) if minutes >= 1 else None,
            'soc_from': _soc(r.get('start_soc')), 'soc_to': _soc(r.get('end_soc')),
            'odo_from': odo_from, 'odo_to': odo_to,
            'place_from': _text(r.get('start_place'))[:120] or None,
            'place_to': _text(r.get('end_place'))[:120] or None,
        })
    out.sort(key=lambda t: t['start'])
    return out, skipped


def latest_reading(charges: list, trip_rows: list) -> dict:
    """The newest SoC and odometer Mate has, and when the car reported them.

    The SoC is the one at the end of whatever finished last — a drive or a
    charge. The odometer only goes up, so it is the largest plausible trip
    end reading."""
    best_t, best_soc, odo, odo_t = None, None, None, None
    for r in trip_rows:
        end = _epoch(r.get('ended_at'))
        soc = _soc(r.get('end_soc'))
        o = _odo(r.get('end_odometer_km'))
        if end and soc is not None and (best_t is None or end > best_t):
            best_t, best_soc = end, soc
        if o is not None and (odo is None or o > odo):
            odo, odo_t = o, end
    for c in charges:
        if c['soc_to'] is not None and (best_t is None or c['end'] > best_t):
            best_t, best_soc = c['end'], c['soc_to']
        o = c.get('odometer')
        if o is not None and (odo is None or o > odo):
            odo, odo_t = float(o), c['end']
    return {'soc': best_soc, 'at': best_t, 'odometer': odo, 'odometer_at': odo_t}


_SERIES_RE = r"\b%s\s*:\s*\{[^{}]*?data:\s*col\((\[[^\]]*\])\)"


def parse_live_chart(html: str) -> dict:
    """What the charge-in-progress panel says: ``charging`` plus, when the
    chart's data can be read, the latest ``soc`` and ``power_kw``.

    Mate renders nothing but whitespace and a template comment while no
    charge runs, so "has a card in it" is the signal; the numbers are a
    bonus that a future template change may take away without breaking
    anything."""
    body = re.sub(r'<!--.*?-->', '', html or '', flags=re.S).strip()
    if not body or 'class=' not in body:
        return {'charging': False, 'soc': None, 'power_kw': None}

    def last(name):
        m = re.search(_SERIES_RE % name, body, flags=re.S)
        if not m:
            return None
        try:
            values = json.loads(m.group(1))
        except ValueError:
            return None
        for v in reversed(values if isinstance(values, list) else []):
            n = _num(v)
            if n is not None:
                return n
        return None

    soc = last('soc')
    power = last('power')
    if power is not None and (power < 0 or power > CHARGE_MAX_KW):
        power = None
    return {'charging': True,
            'soc': soc if soc is not None and plausible_percent(soc) is not None else None,
            'power_kw': power}


# ── HTTP ──────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D401
        return None


_SESSIONS: dict = {}          # (base, sha256(password)) → cookie value
_SESSIONS_LOCK = threading.Lock()


def _session_key(base: str, password: str) -> tuple:
    return base, hashlib.sha256(password.encode('utf-8')).hexdigest()


class LeapmotorMateConnector(VehicleConnector):
    """Reads one Leapmotor from a Mate install. See the module note."""

    records_own_charges = True

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.charge_sessions: list = []
        self.skipped_charges = 0
        self.trip_sessions: list = []
        self.trip_rows_read = 0

    # ── plumbing ──────────────────────────────────────────────────────
    def _base(self) -> str:
        url = (self.credentials.get('username') or '').strip().rstrip('/')
        if not url:
            raise MateError('no_url', _t('mate.err_need_url'))
        if not re.match(r'^https?://', url, flags=re.I):
            url = 'http://' + url
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise MateError('no_url', _t('mate.err_need_url'))
        # Only the origin: a pasted page address ("…:4000/settings") still works.
        return f'{parts.scheme.lower()}://{parts.netloc}'

    def _password(self) -> str:
        return (self.credentials.get('password') or '').strip()

    def _request(self, path: str, *, data: Optional[bytes] = None, cookie: str = '',
                 accept: str = '*/*'):
        """``(status, headers, body_bytes)``. Redirects are returned, not followed."""
        base = self._base()
        headers = {'User-Agent': 'ev-charge-tracker', 'Accept': accept}
        if cookie:
            headers['Cookie'] = f'{COOKIE_NAME}={cookie}'
        if data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method='POST' if data is not None else 'GET')
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=DEFAULT_TIMEOUT) as resp:
                return resp.status, resp.headers, resp.read(MAX_CSV_BYTES + 1)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(64 * 1024)
            except Exception:              # pragma: no cover
                body = b''
            return e.code, e.headers, body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, 'reason', e)
            raise MateError('network', _t('mate.err_network', url=base,
                                          reason=str(reason)[:120]))

    def _refused(self, status: int, body: bytes):
        text = (body or b'').decode('utf-8', 'replace')
        if status == 403 and ('password' in text.lower() or 'MATE_ALLOWED_HOSTS' in text
                              or 'Home Assistant' in text):
            raise MateError('host', _t('mate.err_host_refused'))
        if status == 403:
            raise MateError('http', _t('mate.err_http', status=status))
        if status == 429:
            raise MateError('throttled', _t('mate.err_throttled'))
        raise MateError('http', _t('mate.err_http', status=status))

    def _login(self) -> str:
        password = self._password()
        if not password:
            raise MateError('auth', _t('mate.err_password_needed'))
        status, headers, body = self._request(
            '/login', data=urllib.parse.urlencode({'password': password}).encode())
        if status == 429:
            raise MateError('throttled', _t('mate.err_throttled'))
        if status == 401:
            raise MateError('auth', _t('mate.err_password_wrong'))
        if status in (403,):
            self._refused(status, body)
        jar = http.cookies.SimpleCookie()
        for raw in headers.get_all('Set-Cookie') or []:
            try:
                jar.load(raw)
            except http.cookies.CookieError:      # pragma: no cover
                continue
        morsel = jar.get(COOKIE_NAME)
        if morsel is None or not morsel.value:
            # 303 to "/" without a cookie means Mate has no password at all.
            if status in (302, 303, 307) and not password:
                return ''
            raise MateError('auth', _t('mate.err_password_wrong'))
        with _SESSIONS_LOCK:
            _SESSIONS[_session_key(self._base(), password)] = morsel.value
        return morsel.value

    def _cookie(self) -> str:
        password = self._password()
        if not password:
            return ''
        with _SESSIONS_LOCK:
            cached = _SESSIONS.get(_session_key(self._base(), password))
        return cached or self._login()

    def _get(self, path: str, accept: str = '*/*') -> bytes:
        cookie = self._cookie()
        status, _h, body = self._request(path, cookie=cookie, accept=accept)
        if status == 401:
            if not self._password():
                raise MateError('auth', _t('mate.err_password_needed'))
            with _SESSIONS_LOCK:                    # the session expired or the password changed
                _SESSIONS.pop(_session_key(self._base(), self._password()), None)
            status, _h, body = self._request(path, cookie=self._login(), accept=accept)
            if status == 401:
                raise MateError('auth', _t('mate.err_password_wrong'))
        if status in (302, 303, 307):
            # A page redirected to /login or /setup instead of answering.
            raise MateError('auth', _t('mate.err_password_needed'))
        if status != 200:
            self._refused(status, body)
        if len(body) > MAX_CSV_BYTES:
            raise MateError('format', _t('mate.err_format', reason='too large'))
        return body

    # ── what Mate says ────────────────────────────────────────────────
    def health(self) -> dict:
        status, _h, body = self._request('/healthz', accept='application/json')
        try:
            data = json.loads((body or b'').decode('utf-8', 'replace'))
        except ValueError:
            data = None
        if not isinstance(data, dict) or 'status' not in data:
            if status in (403, 429):
                self._refused(status, body)
            raise MateError('not_mate', _t('mate.err_not_mate', url=self._base()))
        state = str(data.get('status'))
        if state == 'awaiting_setup':
            raise MateError('setup', _t('mate.err_awaiting_setup'))
        if state == 'demo':
            raise MateError('demo', _t('mate.err_demo'))
        return data

    def fetch(self) -> dict:
        charges_csv = self._get('/api/export/charges.csv', accept='text/csv')
        trips_csv = self._get('/api/export/trips.csv', accept='text/csv')
        charge_rows = _read_csv(charges_csv.decode('utf-8', 'replace'))
        trip_rows = _read_csv(trips_csv.decode('utf-8', 'replace'))
        try:
            live = parse_live_chart(self._get('/api/charging-chart', accept='text/html')
                                    .decode('utf-8', 'replace'))
        except MateError as e:
            if e.kind in ('auth', 'host', 'network'):
                raise
            live = {'charging': False, 'soc': None, 'power_kw': None}
        sessions, skipped = parse_charges(charge_rows)
        trips, trips_skipped = parse_trips(trip_rows)
        return {'sessions': sessions, 'skipped': skipped, 'trip_rows': trip_rows,
                'trips': trips, 'trips_skipped': trips_skipped,
                'charge_rows': len(charge_rows), 'live': live}

    # ── the interface ─────────────────────────────────────────────────
    def authenticate(self) -> bool:
        try:
            self.health()
            self._get('/api/export/charges.csv', accept='text/csv')
            return True
        except MateError as e:
            logger.error(f"Leapmotor Mate check failed ({e.kind})")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def verify_credentials(self) -> str:
        """Raise with the reason, or say what Mate holds."""
        health = self.health()
        data = self.fetch()
        reading = latest_reading(data['sessions'], data['trip_rows'])
        if reading['soc'] is None and reading['odometer'] is None and not data['sessions']:
            raise MateError('nothing', _t('mate.err_nothing_yet'))
        soc = '—' if reading['soc'] is None else f"{reading['soc']:.0f} %"
        odo = ('—' if reading['odometer'] is None
               else f"{int(reading['odometer']):,} km".replace(',', ' '))   # whole km, as stored
        msg = _t('mate.test_found', charges=len(data['sessions']),
                 trips=len(data['trip_rows']), soc=soc, odometer=odo)
        if health.get('status') == 'stale':
            msg += ' ' + _t('mate.warn_stale')
        if data['live']['charging']:
            msg += ' ' + _t('mate.test_charging_now')
        return msg

    def get_status(self, force=False) -> VehicleStatus:
        """``force`` cannot do anything: Mate decides when the car is asked.

        Mate's own health comes first: a Mate in demo mode serves a sample
        car, and its charges must never be filed as the owner's."""
        self.health()
        data = self.fetch()
        try:
            from services.leapmotor_mate_import import attach_odometer
            attach_odometer(data['sessions'], data['trip_rows'])
        except Exception as e:                       # an odometer is a bonus, never a failure
            logger.warning(f"Leapmotor Mate: odometer per charge not attached: {e}")
        self.charge_sessions = data['sessions']
        self.skipped_charges = data['skipped']
        self.trip_sessions = data['trips']
        self.trip_rows_read = len(data['trip_rows'])
        reading = latest_reading(data['sessions'], data['trip_rows'])
        live = data['live']
        soc = live['soc'] if live['charging'] and live['soc'] is not None else reading['soc']
        captured = time.time() if live['charging'] and live['soc'] is not None else reading['at']
        if soc is None and reading['odometer'] is None and not live['charging']:
            raise MateError('nothing', _t('mate.err_nothing_yet'))
        return VehicleStatus(
            soc_percent=int(round(soc)) if soc is not None else None,
            odometer_km=int(reading['odometer']) if reading['odometer'] is not None else None,
            is_charging=bool(live['charging']),
            is_plugged_in=bool(live['charging']),
            charge_power_kw=live['power_kw'] if live['charging'] else None,
            vehicle_model='Leapmotor',
            distance_unit='km',
            captured_at=datetime.fromtimestamp(captured) if captured else None,
            last_updated=datetime.fromtimestamp(captured).isoformat(timespec='seconds')
            if captured else None,
            raw_data={'source': 'leapmotor_mate', 'charges_in_export': data['charge_rows'],
                      'charges_usable': len(data['sessions']), 'charges_skipped': data['skipped'],
                      'trips_in_export': len(data['trip_rows']), 'charging_now': live['charging']},
        )

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return 'Leapmotor (Mate)'


# Plain HTTP with the standard library: nothing to install, always usable.
register('leapmotor_mate', LeapmotorMateConnector)
