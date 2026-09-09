# -*- coding: utf-8 -*-
"""Client for the official MyŠkoda Public API.

Škoda is closing the unofficial app API that `myskoda` and
`carconnectivity-connector-skoda` speak — announced for October 2026 —
and published an official one in its place:

    https://public.api.connect.skoda-auto.cz/docs

Three things about it shape everything below.

**1. One read endpoint.** ``GET /api/v1/vehicles/{vin}`` returns the whole
vehicle state; every other endpoint is a remote command. There is no trips
endpoint and no charging-history endpoint, so the parts of this app that
used to read those from MyŠkoda have no counterpart here (see
``connector_skoda_public`` for what that costs).

**2. An API key, not a password.** The owner creates it in the MyŠkoda app
(https://go.skoda.eu/api-keys), bound to the vehicles they pick, and it
**expires**: every successful response carries ``X-API-Key-Expires-At``.
A client that ignores that header discovers the expiry as a sudden 401
with nothing useful to tell the user, so we read it on every call and keep
it where the UI can warn in time.

**3. A hard rate limit — the reason this file exists at all.** The
documentation states 20 requests per hour per VIN, and every response
carries the standard ``RateLimit-Limit``/``-Remaining``/``-Reset`` fields.
Twenty per hour is not a lot for an app that also refreshes on demand, so
the budget is tracked here rather than trusted to callers: a caller that
would spend the last of the quota is refused *before* the request, and the
refusal says when the window resets. Spending the quota on a background
poll and leaving the user's own "sync now" to fail would be exactly the
wrong way round.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

BASE_URL = 'https://public.api.connect.skoda-auto.cz'
VEHICLE_PATH = '/api/v1/vehicles/{vin}'

# Where the owner creates and revokes keys. Shown in the UI — a user who
# cannot find this page cannot use the API at all.
KEY_MANAGEMENT_URL = 'https://go.skoda.eu/api-keys'

# Documented quota at the time of writing. Only a fallback: the real
# numbers come from the RateLimit-* headers of the last response, which
# is what the server actually enforces.
ASSUMED_HOURLY_QUOTA = 20

# Leave this much of the quota unspent so a person pressing "sync now"
# is not blocked by the background loop having used the last request.
RESERVE_FOR_USER = 3

DEFAULT_TIMEOUT = 30


class SkodaApiError(RuntimeError):
    """Any failure that the user can act on, with the reason attached.

    ``kind`` is a stable token the UI can branch on without parsing
    German (or English) prose: ``key_expired``, ``key_not_authorized``,
    ``unknown_vin``, ``rate_limited``, ``asleep``, ``http``, ``network``.
    """

    def __init__(self, kind: str, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class _Budget:
    """What the server last told us about the remaining quota.

    Deliberately per-VIN and process-wide: the limit is enforced per
    vehicle, and two threads (background sync and a user request) share
    one budget. Guarded by a lock because both may touch it at once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {}

    def note(self, vin: str, limit: Optional[int], remaining: Optional[int],
             reset_s: Optional[int]) -> None:
        with self._lock:
            self._state[vin] = {
                'limit': limit,
                'remaining': remaining,
                'resets_at': (time.time() + reset_s) if reset_s is not None else None,
                'seen_at': time.time(),
            }

    def snapshot(self, vin: str) -> dict:
        with self._lock:
            s = dict(self._state.get(vin) or {})
        if s.get('resets_at') and time.time() >= s['resets_at']:
            # Window has rolled over; what we knew is stale in our favour.
            s['remaining'] = s.get('limit')
            s['resets_at'] = None
        return s

    def seconds_to_reset(self, vin: str) -> Optional[int]:
        s = self.snapshot(vin)
        if not s.get('resets_at'):
            return None
        return max(0, int(s['resets_at'] - time.time()))


_budget = _Budget()


def budget_state(vin: str) -> dict:
    """What the UI may show about the quota. Empty before the first call."""
    return _budget.snapshot(vin)


def _int_header(headers, name: str) -> Optional[int]:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _problem(body: bytes) -> Tuple[str, str]:
    """Pull ``type`` and ``detail`` out of an RFC 9457 problem document."""
    try:
        d = json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return '', ''
    if not isinstance(d, dict):
        return '', ''
    return str(d.get('type') or ''), str(d.get('detail') or d.get('title') or '')


def get_vehicle(api_key: str, vin: str, parts: Optional[list] = None,
                timeout: int = DEFAULT_TIMEOUT,
                spend_reserve: bool = False) -> dict:
    """Read one vehicle's state.

    ``parts`` maps to the ``include`` query parameter. Asking only for
    what we use is not a micro-optimisation here: parts the vehicle does
    not support are reported as errors *only when explicitly requested*,
    which is how we learn that, say, a car has no parking position —
    with no ``include`` the part is simply missing and indistinguishable
    from a temporary gap.

    ``spend_reserve`` marks a request a person is waiting for. Background
    work leaves the last few requests of the hour alone so that a manual
    refresh still has room.
    """
    if not api_key:
        raise SkodaApiError('no_key', 'Kein API-Schlüssel hinterlegt.')
    if not vin:
        raise SkodaApiError('no_vin', 'Keine Fahrzeug-Identifikationsnummer hinterlegt.')

    floor = 0 if spend_reserve else RESERVE_FOR_USER
    snap = _budget.snapshot(vin)
    remaining = snap.get('remaining')
    if remaining is not None and remaining <= floor:
        wait = _budget.seconds_to_reset(vin)
        raise SkodaApiError(
            'rate_limited',
            'Das Stundenkontingent der Škoda-API ist aufgebraucht.',
            retry_after=wait,
        )

    url = BASE_URL + VEHICLE_PATH.format(vin=urllib.parse.quote(vin, safe=''))
    if parts:
        url += '?' + urllib.parse.urlencode({'include': ','.join(parts)})

    req = urllib.request.Request(url, headers={
        'X-API-Key': api_key,
        'Accept': 'application/json',
        'User-Agent': 'ev-charge-tracker',
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            h = resp.headers
            _budget.note(vin,
                         _int_header(h, 'RateLimit-Limit'),
                         _int_header(h, 'RateLimit-Remaining'),
                         _int_header(h, 'RateLimit-Reset'))
            expires_at = h.get('X-API-Key-Expires-At')
            data = json.loads(body.decode('utf-8', 'replace'))
            if isinstance(data, dict) and expires_at:
                data['_key_expires_at'] = expires_at
            return data
    except urllib.error.HTTPError as e:
        body = b''
        try:
            body = e.read()
        except Exception:
            pass
        h = getattr(e, 'headers', {}) or {}
        # A 429 still carries the quota headers; recording them is what
        # stops us hammering a closed door for the rest of the window.
        _budget.note(vin,
                     _int_header(h, 'RateLimit-Limit'),
                     _int_header(h, 'RateLimit-Remaining'),
                     _int_header(h, 'RateLimit-Reset'))
        ptype, detail = _problem(body)
        raise _translate(e.code, ptype, detail, _int_header(h, 'Retry-After'))
    except urllib.error.URLError as e:
        raise SkodaApiError('network', f'Škoda-API nicht erreichbar: {e.reason}')
    except json.JSONDecodeError:
        raise SkodaApiError('http', 'Škoda-API lieferte kein JSON.')


def _translate(code: int, ptype: str, detail: str, retry_after: Optional[int]):
    """HTTP status + problem type -> something the UI can act on.

    The status alone is not enough: 401 means "key expired" or "key
    wrong", and those need different words. 503 is the interesting one —
    it is not "Škoda is down" but usually "this car is asleep", and
    telling the user their service is broken would be wrong.
    """
    t = (ptype or '').rsplit('/', 1)[-1]
    if code == 401:
        if 'expired' in t:
            return SkodaApiError('key_expired',
                                 'Der API-Schlüssel ist abgelaufen — '
                                 'in der MyŠkoda-App einen neuen erzeugen.')
        return SkodaApiError('key_invalid', detail or 'API-Schlüssel wurde abgelehnt.')
    if code == 403:
        return SkodaApiError('key_not_authorized',
                             detail or 'Der Schlüssel gilt nicht für dieses Fahrzeug.')
    if code == 404:
        return SkodaApiError('unknown_vin',
                             detail or 'Zu dieser Fahrgestellnummer gibt es kein Fahrzeug.')
    if code == 429:
        return SkodaApiError('rate_limited',
                             'Das Stundenkontingent der Škoda-API ist aufgebraucht.',
                             retry_after=retry_after)
    if code in (503, 504):
        return SkodaApiError('asleep',
                             detail or 'Das Fahrzeug ist gerade nicht erreichbar.',
                             retry_after=retry_after)
    return SkodaApiError('http', detail or f'Škoda-API antwortete mit HTTP {code}.')


# ── Remote commands ───────────────────────────────────────────────────
#
# Opt-in only, and gated a second time at the API layer rather than only
# in the UI: a remote command moves a real car's hardware, and "the
# button was hidden" is not a safety property. The caller has to say
# ``enabled=True``, which the route only does after checking the
# vehicle's own opt-in flag.
#
# Not implemented on purpose: auxiliary heating. Its start call requires
# ``spin`` — the vehicle's security PIN — which is a different credential
# with a different risk profile than a read-only API key, and asking
# people to store it for a convenience feature is a bad trade.

COMMANDS = {
    # name              method  path suffix                     body builder
    'charging_start':   ('POST', '/charging/start',              None),
    'charging_stop':    ('POST', '/charging/stop',               None),
    'charging_limit':   ('PUT',  '/charging/limit',              'limit'),
    'ac_start':         ('POST', '/air-conditioning/start',      'ac'),
    'ac_stop':          ('POST', '/air-conditioning/stop',       None),
    'ventilation_start': ('POST', '/active-ventilation/start',   None),
    'ventilation_stop': ('POST', '/active-ventilation/stop',     None),
}


def _build_body(kind: Optional[str], params: dict):
    if kind is None:
        return None
    if kind == 'limit':
        pct = int(params.get('target_soc') or 0)
        if not 50 <= pct <= 100:
            # The car rejects values outside its own range with a bare
            # 400; saying which values are allowed is more useful than
            # forwarding that.
            raise SkodaApiError('bad_request',
                                'Ladelimit muss zwischen 50 und 100 % liegen.')
        return {'targetStateOfChargeInPercent': pct}
    if kind == 'ac':
        temp = params.get('target_temp')
        body: dict = {}
        if temp is not None:
            body['targetTemperature'] = {
                'temperatureValue': float(temp), 'unitInCar': 'CELSIUS'}
        # Without this the car refuses to pre-heat unless it is plugged
        # in — which is exactly the case people want it for.
        body['airConditioningWithoutExternalPower'] = bool(
            params.get('without_external_power', True))
        return body
    raise SkodaApiError('bad_request', f'Unbekannter Befehlskörper: {kind}')


def send_command(api_key: str, vin: str, command: str, *, enabled: bool,
                 params: Optional[dict] = None,
                 timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Send one remote command. Returns ``{'accepted': True}`` on 202.

    ``enabled`` is not a courtesy flag — refusing here means a bug that
    forgets the opt-in cannot move somebody's car.
    """
    if not enabled:
        raise SkodaApiError('remote_disabled',
                            'Fernsteuerung ist für dieses Fahrzeug nicht aktiviert.')
    spec = COMMANDS.get(command)
    if not spec:
        raise SkodaApiError('bad_request', f'Unbekannter Befehl: {command}')
    if not api_key or not vin:
        raise SkodaApiError('no_key', 'Zugangsdaten unvollständig.')

    method, suffix, body_kind = spec
    body = _build_body(body_kind, params or {})

    # A command spends the same hourly quota as a read. Someone pressing
    # a button is the definition of "a person is waiting", so it may use
    # the reserve — but it must still be refused when the window is
    # genuinely empty, otherwise the car silently does nothing.
    snap = _budget.snapshot(vin)
    if snap.get('remaining') is not None and snap['remaining'] <= 0:
        raise SkodaApiError('rate_limited',
                            'Das Stundenkontingent der Škoda-API ist aufgebraucht.',
                            retry_after=_budget.seconds_to_reset(vin))

    url = BASE_URL + VEHICLE_PATH.format(vin=urllib.parse.quote(vin, safe='')) + suffix
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'X-API-Key': api_key,
        'Accept': 'application/json',
        'User-Agent': 'ev-charge-tracker',
        **({'Content-Type': 'application/json'} if data is not None else {}),
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            h = resp.headers
            _budget.note(vin,
                         _int_header(h, 'RateLimit-Limit'),
                         _int_header(h, 'RateLimit-Remaining'),
                         _int_header(h, 'RateLimit-Reset'))
            # 202 Accepted: the car has been asked, not yet obeyed. The
            # UI must say "sent", never "done" — the result only shows up
            # in a later state read.
            return {'accepted': resp.status in (200, 202), 'status': resp.status}
    except urllib.error.HTTPError as e:
        h = getattr(e, 'headers', {}) or {}
        _budget.note(vin,
                     _int_header(h, 'RateLimit-Limit'),
                     _int_header(h, 'RateLimit-Remaining'),
                     _int_header(h, 'RateLimit-Reset'))
        body_b = b''
        try:
            body_b = e.read()
        except Exception:
            pass
        ptype, detail = _problem(body_b)
        raise _translate(e.code, ptype, detail, _int_header(h, 'Retry-After'))
    except urllib.error.URLError as e:
        raise SkodaApiError('network', f'Škoda-API nicht erreichbar: {e.reason}')
