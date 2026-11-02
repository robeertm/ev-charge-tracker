# -*- coding: utf-8 -*-
"""Client for the official Pleos personal Vehicle Data API (Hyundai, Kia, Genesis).

On 13/14 October 2026 Kia and Hyundai Europe started refusing the app
sign-in that ``hyundai_kia_connect_api`` performs: the password check
itself still passes, but the token exchange right after it answers
``HTTP 400 {"code": 9035, "message": "Unusual activity has been detected
so you've been logged out"}``. Every third-party client in Europe broke
the same morning, and the manufacturer's support confirmed that automated
logins are no longer allowed. There is nothing to fix on our side of that
door, and we do not try to get past it.

Pleos is the manufacturer group's own developer platform, and its
**personal Vehicle Data API** is the sanctioned way for an owner to read
their own car. The owner creates a key once (pleos.ai → account menu →
"My Vehicle Data API" → pick the brand → "Get API Key" → sign in with the
car account), and gets a Client ID and a Client Secret. This module turns
those into an access token and reads the snapshot endpoints.

Shape of the API, as documented and as observed:

  * ``POST /v1/auth/personal-token`` with ``{"clientId", "clientSecret"}``
    and a ``Brand`` header → ``data.accessToken``. A bad key answers
    ``400 {"error": {"code": "4002"}}`` (observed with made-up values),
    a revoked or expired one 401/403.
  * Every read is ``GET /v1/vehicles/{vin}/<endpoint>`` with
    ``Authorization: Bearer`` and the same ``Brand`` header, answering
    ``{"data": {...}, "meta": {...}}`` or ``{"error": {"code": ...}}``.
    An expired bearer token comes back as 403, not 401, so a 403 is
    retried once with a fresh token before it counts as "not allowed".
  * ``4045`` means "no data for this endpoint", which is a normal answer
    for a car that does not report that part. One missing part must never
    cost the others.
  * There is no wake-up call: the API serves what the car last reported.

Secrets and VINs never go into a log line or an exception text: the VIN
is part of every URL, so URLs are never logged either.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.pleos.ai'

# Where the owner creates the key. Shown next to the credential boxes —
# somebody who cannot find this page cannot use the API at all.
KEY_MANAGEMENT_URL = 'https://pleos.ai/'
KEY_GUIDE_URL = ('https://document.pleos.ai/en/api-reference/vehicle-data-api/'
                 'getting-started/for-private-users/request-my-vehicle-api-key')

BRANDS = ('hyundai', 'kia', 'genesis')

# The current-state endpoints this app maps. The trip endpoints exist too
# but carry samples without timestamps; trips are derived here from
# parking events, exactly as for the official Škoda API.
SNAPSHOT_ENDPOINTS = ('batteries', 'powertrains', 'driving', 'locations', 'status')

DEFAULT_TIMEOUT = 30
MAX_BODY = 16 * 1024 * 1024
# How long the consented-vehicle list is trusted. It only changes when the
# owner issues a new key, and asking for it on every sync would add a
# request to each one.
VEHICLES_TTL = 6 * 3600
# A 429 without Retry-After still keeps us away this long.
MIN_BACKOFF = 300

# Error codes that mean "this token is no good any more".
_TOKEN_CODES = {'401', '4011', '40003', '40004'}
# Codes on the token endpoint that mean "this key is no good".
_KEY_CODES = {'401', '403', '4002', '40001', '40404'} | _TOKEN_CODES


def _t(key, **kw):
    try:
        from services.i18n import t
        return t(key, **kw)
    except Exception:                      # pragma: no cover - i18n always importable
        return key


class PleosError(RuntimeError):
    """A failure the owner can act on, with a stable ``kind`` to branch on.

    ``kind``: ``no_key``, ``key_invalid``, ``not_authorized``, ``no_data``,
    ``rate_limited``, ``no_vehicle``, ``pick_vin``, ``vin_not_in_key``,
    ``network``, ``http``; ``key_expired`` is set by ``pleos_key.refine_failure``
    when a ``key_invalid`` arrives at or after the key's end date.
    """

    def __init__(self, kind: str, message: str, status: int = 0, code: str = '',
                 retry_after: Optional[int] = None, vehicles: Optional[list] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.vehicles = vehicles or []


# ── process-wide state ────────────────────────────────────────────────
# Shared by the background loop and a person pressing a button, hence the
# lock. Keyed on a fingerprint of the key, never on the key itself.
_lock = threading.Lock()
_tokens: dict = {}
_retry_at: dict = {}
_vehicle_cache: dict = {}
# fp -> {'first_ok': epoch, 'read': 'YYYY-MM-DD' | None}: what a successful
# token call told us about the KEY (not the token). See ``key_seen``.
_key_seen: dict = {}


def _fp(brand: str, client_id: str, client_secret: str) -> str:
    h = hashlib.sha256(f'{brand}\0{client_id}\0{client_secret}'.encode('utf-8'))
    return h.hexdigest()[:16]


def reset_state() -> None:
    """Forget tokens, back-off and vehicle lists (tests, key replacement)."""
    with _lock:
        _tokens.clear()
        _retry_at.clear()
        _vehicle_cache.clear()
        _key_seen.clear()


def _retry_after_seconds(headers) -> int:
    raw = (headers or {}).get('Retry-After') if headers is not None else None
    try:
        return max(MIN_BACKOFF, int(str(raw).strip()))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(str(raw))
        return max(MIN_BACKOFF, int((when - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return MIN_BACKOFF


def _error_code(payload) -> str:
    if not isinstance(payload, dict):
        return ''
    err = payload.get('error')
    if isinstance(err, dict):
        return str(err.get('code', ''))
    meta = payload.get('meta')
    if isinstance(meta, dict) and str(meta.get('success')).lower() == 'false':
        return str(meta.get('code', ''))
    return ''


def _request(method: str, path: str, brand: str, fp: str, token: Optional[str] = None,
             body: Optional[dict] = None, timeout: int = DEFAULT_TIMEOUT):
    """One call. Returns ``data``; raises PleosError(status, code)."""
    with _lock:
        until = _retry_at.get(fp, 0)
    if time.time() < until:
        raise PleosError('rate_limited', _t('pleos.err_rate_limited'),
                         status=429, code='rate_limited',
                         retry_after=int(until - time.time()))
    headers = {'Brand': brand, 'Accept': 'application/json',
               'User-Agent': 'ev-charge-tracker'}
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
    status, raw, resp_headers = 0, b'', None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = resp.headers
            raw = resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = getattr(e, 'headers', None)
        try:
            raw = e.read(MAX_BODY + 1)
        except Exception:
            raw = b''
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # No URL in the text: it carries the VIN.
        reason = getattr(e, 'reason', e)
        raise PleosError('network', _t('pleos.err_network', reason=str(reason)[:120]))
    if len(raw) > MAX_BODY:
        raise PleosError('http', _t('pleos.err_http', status=status), status=status,
                         code='oversized')
    try:
        payload = json.loads(raw.decode('utf-8', 'replace')) if raw else {}
    except ValueError:
        payload = None
    if status == 429:
        wait = _retry_after_seconds(resp_headers)
        with _lock:
            _retry_at[fp] = time.time() + wait
        raise PleosError('rate_limited', _t('pleos.err_rate_limited'),
                         status=429, code='429', retry_after=wait)
    if not isinstance(payload, dict):
        raise PleosError('http', _t('pleos.err_http', status=status), status=status,
                         code='invalid_response')
    code = _error_code(payload)
    if status >= 300 or code:
        raise PleosError('http', _t('pleos.err_http', status=status), status=status,
                         code=code or str(status))
    return payload.get('data')


def _token(brand: str, client_id: str, client_secret: str, fp: str) -> str:
    with _lock:
        tok = _tokens.get(fp)
    if tok:
        return tok
    try:
        data = _request('POST', '/v1/auth/personal-token', brand, fp,
                        body={'clientId': client_id, 'clientSecret': client_secret})
    except PleosError as e:
        if e.kind == 'http' and (e.status in (400, 401, 403) or e.code in _KEY_CODES):
            raise PleosError('key_invalid', _t('pleos.err_key_invalid'),
                             status=e.status, code=e.code) from None
        raise
    tok = data.get('accessToken') if isinstance(data, dict) else None
    if not isinstance(tok, str) or not tok:
        raise PleosError('http', _t('pleos.err_http', status=200), status=200,
                         code='missing_token')
    read = key_expiry_from(data, tok)
    with _lock:
        _tokens[fp] = tok
        seen = _key_seen.setdefault(fp, {'first_ok': time.time(), 'read': None})
        if read:
            seen['read'] = read
    return tok


# ── the key's own end date, if the API ever states it ─────────────────
# The documentation (document.pleos.ai, "Manage My Vehicle API Key") knows
# the states Generated / Revoked / Expired and says that the access tokens
# of an expired key stop working — but names no validity period for the
# personal key and no endpoint that returns its end date; the date is only
# shown in the portal under "API key status". If the token answer (or the
# token's own payload) ever carries a field that unmistakably belongs to
# the KEY, it is used. ``exp`` / ``expiresIn`` of the access token are the
# TOKEN's lifetime (minutes to hours) and deliberately do not count.
_KEY_EXPIRY_FIELDS = frozenset({
    'apikeyexpiresat', 'apikeyexpiredat', 'apikeyexpiry', 'apikeyexpiration',
    'apikeyexpirationdate', 'apikeyexpiredate', 'apikeyexpirydate', 'apikeyvaliduntil',
    'apikeyenddate', 'keyexpiresat', 'keyexpiredat', 'keyexpiry', 'keyexpiration',
    'keyexpiredate', 'keyexpirydate', 'keyvaliduntil',
    'clientsecretexpiresat', 'clientsecretexpiry', 'clientsecretexpiredat',
    'clientsecretvaliduntil', 'credentialexpiresat',
})


def _norm_field(name) -> str:
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())


def _as_date(value) -> Optional[str]:
    """'YYYY-MM-DD' from an ISO date/datetime, 'YYYYMMDD' or epoch s/ms."""
    from datetime import datetime, timezone
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:                  # RFC 7591: 0 means "never expires"
            return None
        if v > 1e11:                # milliseconds
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if s.isdigit():
        if len(s) == 8:
            try:
                return datetime.strptime(s, '%Y%m%d').date().isoformat()
            except ValueError:
                return None
        return _as_date(int(s))
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def _jwt_payload(token: str) -> dict:
    """The payload of a JWT, decoded locally. Never logged."""
    import base64
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        seg = parts[1] + '=' * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg.encode('ascii')).decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def key_expiry_from(data, token) -> Optional[str]:
    """The key's end date from the token answer or the token payload."""
    for src in (data if isinstance(data, dict) else {},
                _jwt_payload(token) if isinstance(token, str) else {}):
        for k, v in src.items():
            if _norm_field(k) in _KEY_EXPIRY_FIELDS:
                d = _as_date(v)
                if d:
                    return d
    return None


def key_seen(brand: str, client_id: str, client_secret: str) -> Optional[dict]:
    """What this process learnt about the key on a successful token call:
    ``{'first_ok': epoch, 'read': 'YYYY-MM-DD' | None}``, or None when no
    token was obtained with it since the start."""
    fp = fingerprint(brand, client_id, client_secret)
    with _lock:
        seen = _key_seen.get(fp)
        return dict(seen) if seen else None


def fingerprint(brand: str, client_id: str, client_secret: str) -> str:
    """Stable, non-reversible id of a key (for "is this still the same key").

    Same inputs as the connector sends (it strips the boxes), same hash as
    the token cache."""
    return _fp((brand or '').lower(), client_id or '', client_secret or '')


def _get(brand: str, client_id: str, client_secret: str, path: str):
    """GET with one token renewal. 403 is how this API says "expired"."""
    brand = (brand or '').lower()
    if brand not in BRANDS:
        raise PleosError('http', _t('pleos.err_http', status=0), code='brand')
    if not (client_id or '').strip() or not (client_secret or '').strip():
        raise PleosError('no_key', _t('pleos.err_no_key'))
    fp = _fp(brand, client_id, client_secret)
    for attempt in (0, 1):
        tok = _token(brand, client_id, client_secret, fp)
        try:
            return _request('GET', path, brand, fp, token=tok)
        except PleosError as e:
            stale = e.status in (401, 403) or e.code in _TOKEN_CODES or e.code == '403'
            if e.kind == 'http' and stale:
                with _lock:
                    _tokens.pop(fp, None)
                if attempt == 0:
                    continue
                if e.status == 401 or e.code in _TOKEN_CODES:
                    raise PleosError('key_invalid', _t('pleos.err_key_invalid'),
                                     status=e.status, code=e.code) from None
                raise PleosError('not_authorized', _t('pleos.err_not_authorized'),
                                 status=e.status, code=e.code) from None
            if e.kind == 'http' and e.code == '4045':
                raise PleosError('no_data', _t('pleos.err_no_data'),
                                 status=e.status, code=e.code) from None
            raise


def vehicles(brand: str, client_id: str, client_secret: str, fresh: bool = False) -> list:
    """The vehicles this key was issued for: ``[{vin, nickname, name, type, sellname}]``."""
    fp = _fp((brand or '').lower(), client_id or '', client_secret or '')
    if not fresh:
        with _lock:
            hit = _vehicle_cache.get(fp)
        if hit and time.time() - hit[0] < VEHICLES_TTL:
            return list(hit[1])
    data = _get(brand, client_id, client_secret, '/v1/vehicles/consent')
    found = data.get('vehicles') if isinstance(data, dict) else None
    if not isinstance(found, list):
        raise PleosError('http', _t('pleos.err_http', status=200), status=200,
                         code='invalid_vehicle_list')
    out = [v for v in found if isinstance(v, dict) and isinstance(v.get('vin'), str) and v['vin']]
    with _lock:
        _vehicle_cache[fp] = (time.time(), out)
    return list(out)


def mask_vin(vin: str) -> str:
    vin = str(vin or '')
    return ('…' + vin[-6:]) if len(vin) > 6 else vin


def pick_vehicle(brand: str, client_id: str, client_secret: str, vin: str = '',
                 fresh: bool = False) -> dict:
    """The one vehicle to read. A stored VIN must be covered by the key;
    without one, the key must cover exactly one car."""
    cars = vehicles(brand, client_id, client_secret, fresh=fresh)
    if not cars:
        raise PleosError('no_vehicle', _t('pleos.err_no_vehicle'))
    vin = (vin or '').strip().upper()
    if vin:
        for c in cars:
            if c['vin'].strip().upper() == vin:
                return c
        raise PleosError('vin_not_in_key', _t('pleos.err_vin_not_in_key'),
                         vehicles=[{'vin': c['vin'], 'label': _label(c)} for c in cars])
    if len(cars) == 1:
        return cars[0]
    raise PleosError('pick_vin', _t('pleos.err_pick_vin'),
                     vehicles=[{'vin': c['vin'], 'label': _label(c)} for c in cars])


def _label(car: dict) -> str:
    name = car.get('nickname') or car.get('sellname') or car.get('name') or ''
    return f'{name} ({mask_vin(car.get("vin"))})'.strip()


def snapshot(brand: str, client_id: str, client_secret: str, vin: str,
             endpoints=SNAPSHOT_ENDPOINTS) -> dict:
    """Read each snapshot endpoint; one failing part never hides the others.

    Returns ``{endpoint: data, ..., '_errors': {endpoint: code}}``. A key
    problem or a rate limit stops the round — every further call would
    fail the same way and only dig the hole deeper.
    """
    out: dict = {'_errors': {}}
    quoted = urllib.parse.quote(vin, safe='')
    for ep in endpoints:
        try:
            out[ep] = _get(brand, client_id, client_secret, f'/v1/vehicles/{quoted}/{ep}')
        except PleosError as e:
            if e.kind in ('key_invalid', 'rate_limited', 'no_key', 'network'):
                if not any(k for k in out if not k.startswith('_')):
                    raise
                out['_errors'][ep] = e.code or e.kind
                break
            out['_errors'][ep] = e.code or e.kind
    if not any(k for k in out if not k.startswith('_')) and out['_errors']:
        codes = set(out['_errors'].values())
        if codes <= {'4045'}:
            raise PleosError('no_data', _t('pleos.err_no_data'), code='4045')
        if codes & {'403', 'not_authorized'}:
            raise PleosError('not_authorized', _t('pleos.err_not_authorized'), code='403')
        raise PleosError('http', _t('pleos.err_http', status=0),
                         code=','.join(sorted(codes)))
    return out
