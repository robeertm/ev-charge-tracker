# -*- coding: utf-8 -*-
"""The sign-in state of each vehicle, and the brake after a rejected credential.

Two problems this answers:

* **Nothing machine-readable said whether a car's sign-in works.** A watcher
  had to grep container logs for German error texts. Now every background
  sync writes one small JSON record per vehicle into AppConfig
  (``vehicle_{vid}_login_state``)::

      {"state", "kind", "since", "last_attempt_at", "last_ok_at", "attempts",
       "paused_until", "brand", "detail", "key_expires"}

  ``state`` is one of :data:`STATES`. ``since`` is when the current state
  began and does not move while the state stays the same. ``attempts``
  counts consecutive failures (0 when ok). ``last_ok_at`` survives
  failures. ``detail`` is a short error text with every e-mail address,
  URL path, UUID, VIN, token and long number removed — never credentials.
  ``kind`` is the connector's own word for the failure when it names one
  (e.g. Pleos ``key_invalid`` / ``key_expired``), else the state.
  ``key_expires`` (Pleos only) is ``{"date", "source", "days_left"}`` — see
  ``pleos_key``; it is kept across records until replaced.

* **A rejected credential was asked again every tick.** "Not retried"
  only held within one tick, so an install whose stored token had run out
  signed in — and was refused — 96 times a day, each refusal pushing the
  manufacturer account toward a captcha or a lockout. After a rejected
  credential (``token_expired`` / ``credentials_rejected``) the background
  loop now skips automatic sign-ins for that car: 1 h, 3 h, 6 h, 12 h,
  then 24 h per further rejection. Kia/Hyundai since 3.0.182; Renault/Dacia
  since their connector keeps its sign-in between polls — for those two
  only a refusal the connector itself names (``kind`` on the exception)
  counts, never a "401" read off some data error.

  The pause lives in memory only, like the 9035 vendor-block pause in the
  connector, and is merely mirrored into the record for display. A restart
  therefore allows exactly one fresh attempt. Saving changed credentials,
  or the "Test" button, lifts it at once.

Nothing in here ever raises: it sits inside the sync loop.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

KEY = 'vehicle_{vid}_login_state'

STATES = (
    'ok', 'vendor_block', 'token_expired', 'credentials_rejected',
    'rate_limit', 'consent_required', 'otp_required', 'vendor_error', 'error',
    'key_expired',
)

#: Connector kinds that mean one of the states above under another name.
#: Pleos says ``key_invalid`` for a refused Client ID/Secret.
KIND_ALIASES = {'key_invalid': 'credentials_rejected'}

#: Only these states engage the brake: no retry can revive them.
#: ``key_expired``: a Pleos key refused at (or after) its end date.
BRAKE_STATES = frozenset({'token_expired', 'credentials_rejected', 'key_expired'})
#: Only the connector whose rejections were measured to pile up.
BRAKE_BRANDS = frozenset({'kia', 'hyundai'})
#: Braked only on a refusal the connector states as ``kind`` itself.
KIND_BRAKE_BRANDS = frozenset({'renault', 'dacia',
                               'kia_pleos', 'hyundai_pleos', 'genesis_pleos'})
#: Pause per consecutive rejection; the last step repeats (cap).
PAUSE_STEPS_H = (1, 3, 6, 12, 24)

DETAIL_MAX = 160

_lock = threading.Lock()
_paused_until: dict = {}     # vid -> datetime
_reject_streak: dict = {}    # vid -> consecutive rejections since last ok/lift
_pause_logged: set = set()   # vids whose current pause already wrote its line
_bg = threading.local()      # set while the background loop syncs a car


@contextmanager
def background_sync():
    """Marks the calls inside as the background loop's (not "sync now").

    A connector may hold back a sign-in it would repeat too soon (Renault's
    back-off after a 5xx) — but only for the loop, never for a person.
    """
    prev = getattr(_bg, 'on', False)
    _bg.on = True
    try:
        yield
    finally:
        _bg.on = prev


def in_background() -> bool:
    return bool(getattr(_bg, 'on', False))


# ── classification ────────────────────────────────────────────────────
_VENDOR_ERROR_RE = re.compile(
    r'\b50[0234]\b'
    r'|\b(?:http|status(?:\s*code)?|statuscode|code|error)\W{0,3}5\d\d\b'
    r'|timed?\s*out|timeout|zeitüberschreitung|zeitueberschreitung'
    r'|connection\s*(?:error|refused|reset|aborted)|connectionerror'
    r'|verbindungsfehler|verbindung\s+(?:fehlgeschlagen|abgebrochen|verweigert)'
    r'|bad gateway|service unavailable|gateway time|internal server error'
    r'|name resolution|max retries exceeded',
    re.IGNORECASE)

_REJECTED_RE = re.compile(
    r'\b40[13]\b|unauthori[sz]ed|authentication|invalid_grant|forbidden',
    re.IGNORECASE)

_VENDOR_ERROR_TYPES = ('Timeout', 'ConnectionError', 'ConnectTimeout',
                       'ReadTimeout', 'TimeoutError')


def classify(exc) -> str:
    """The state a failed sign-in/sync leaves the vehicle in.

    An explicit ``kind`` on the exception always wins (the Kia/Hyundai
    connector sets one for each of its own refusals). Everything else is
    read off the message: server errors, timeouts and connection problems
    are the vendor's (``vendor_error``); 401/403, "unauthorized",
    "authentication", "invalid_grant" are a refused credential.
    """
    try:
        kind = _kind_state(exc)
        if kind:
            return kind
        try:
            names = {c.__name__ for c in type(exc).__mro__}
        except Exception:
            names = set()
        if any(n in names for n in _VENDOR_ERROR_TYPES):
            return 'vendor_error'
        msg = str(exc or '')
        if _VENDOR_ERROR_RE.search(msg):
            return 'vendor_error'
        if _REJECTED_RE.search(msg):
            return 'credentials_rejected'
    except Exception:
        pass
    return 'error'


def _kind_state(exc):
    """The state the exception's own ``kind`` names, or None."""
    kind = getattr(exc, 'kind', None)
    if not isinstance(kind, str):
        return None
    kind = KIND_ALIASES.get(kind, kind)
    return kind if kind in STATES and kind != 'ok' else None


# ── scrubbing ─────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# scheme://[userinfo@]host[/path?query#frag] -> scheme://host
_URL_RE = re.compile(
    r'\b([A-Za-z][A-Za-z0-9+.\-]*://)(?:[^/\s?#@"\'<>]*@)?([^/\s?#@"\'<>]+)[^\s"\'<>]*')
_UUID_RE = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
# 17 characters of the VIN alphabet with at least one digit and one letter,
# so a plain long word is not taken for a VIN.
_VIN_RE = re.compile(
    r'(?<![A-Za-z0-9])(?=[A-HJ-NPR-Za-hj-npr-z0-9]{0,16}[0-9])'
    r'(?=[A-HJ-NPR-Za-hj-npr-z0-9]{0,16}[A-HJ-NPR-Za-hj-npr-z])'
    r'[A-HJ-NPR-Za-hj-npr-z0-9]{17}(?![A-Za-z0-9])')
_TOKEN_RE = re.compile(r'[A-Za-z0-9]{24,}')
# base64url-style tokens with '-'/'_' — only when they carry a digit, so an
# identifier like a long snake_case label is left readable.
_TOKEN_DASH_RE = re.compile(r'(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{24,}')
_DIGITS_RE = re.compile(r'\d{8,}')


def scrub(text) -> str:
    """A short, shareable version of an error text (≤ 160 characters)."""
    try:
        s = str(text or '')
        s = _URL_RE.sub(lambda m: m.group(1) + m.group(2), s)
        s = _EMAIL_RE.sub('<email>', s)
        s = _UUID_RE.sub('<uuid>', s)
        s = _VIN_RE.sub('<vin>', s)
        s = _TOKEN_RE.sub('<token>', s)
        s = _TOKEN_DASH_RE.sub('<token>', s)
        s = _DIGITS_RE.sub('<n>', s)
        s = ' '.join(s.split())
        if len(s) > DETAIL_MAX:
            s = s[:DETAIL_MAX - 1].rstrip() + '…'
        return s
    except Exception:
        return ''


# ── the brake (in memory) ─────────────────────────────────────────────
def pause_hours(streak: int) -> int:
    """Hours of pause after the ``streak``-th consecutive rejection."""
    idx = max(1, int(streak)) - 1
    return PAUSE_STEPS_H[min(idx, len(PAUSE_STEPS_H) - 1)]


def paused_until(vid, now: datetime | None = None):
    """The end of this vehicle's sign-in pause, or None when not paused."""
    try:
        now = now or datetime.now()
        with _lock:
            until = _paused_until.get(vid)
            if until is not None and until <= now:
                _paused_until.pop(vid, None)
                _pause_logged.discard(vid)
                until = None
        return until
    except Exception:
        return None


def skip_paused(vid, name: str = '') -> bool:
    """True when the background loop must not sign this vehicle in now.

    Writes exactly one INFO line per pause, on the first skipped tick.
    """
    try:
        until = paused_until(vid)
        if until is None:
            return False
        with _lock:
            first = vid not in _pause_logged
            _pause_logged.add(vid)
        if first:
            logger.info(
                f"Vehicle sync [{name or vid}]: sign-in paused until "
                f"{until.isoformat(timespec='minutes')} after a rejected "
                f"credential — save the credentials or press Test to try again"
            )
        return True
    except Exception:
        return False


def _engage(vid, now: datetime) -> datetime:
    with _lock:
        streak = _reject_streak.get(vid, 0) + 1
        _reject_streak[vid] = streak
        until = now + timedelta(hours=pause_hours(streak))
        _paused_until[vid] = until
        _pause_logged.discard(vid)
    return until


def lift_pause(vid) -> None:
    """Allow the next automatic sign-in at once (credentials saved / Test)."""
    try:
        with _lock:
            had = _paused_until.pop(vid, None) is not None
            _reject_streak.pop(vid, None)
            _pause_logged.discard(vid)
        if had:
            logger.info(f"Vehicle {vid}: sign-in pause lifted")
        rec = _load(vid)
        if rec and rec.get('paused_until'):
            rec['paused_until'] = None
            _store(vid, rec)
    except Exception as e:
        logger.debug(f"login state: pause not lifted cleanly: {e}")


def reset() -> None:
    """Forget every pause — what a restart does. For tests."""
    with _lock:
        _paused_until.clear()
        _reject_streak.clear()
        _pause_logged.clear()


# ── the persisted record ──────────────────────────────────────────────
def _now_iso(now: datetime) -> str:
    return now.isoformat(timespec='seconds')


def _load(vid) -> dict:
    try:
        from models.database import AppConfig
        raw = AppConfig.get(KEY.format(vid=vid), '') or ''
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        _rollback()
        return {}


def _store(vid, rec: dict) -> None:
    try:
        from models.database import AppConfig
        AppConfig.set(KEY.format(vid=vid), json.dumps(rec, default=str))
    except Exception as e:
        logger.debug(f"login state for vehicle {vid} not recorded: {e}")
        _rollback()


def _rollback() -> None:
    try:
        from models.database import db
        db.session.rollback()
    except Exception:
        pass


def _record(vid, brand, state, now, detail, paused, attempts, ok, kind=None) -> None:
    prev = _load(vid)
    since = prev.get('since') if prev.get('state') == state and prev.get('since') else _now_iso(now)
    rec = {
        'state': state,
        'kind': kind or state,
        'since': since,
        'last_attempt_at': _now_iso(now),
        'last_ok_at': _now_iso(now) if ok else prev.get('last_ok_at'),
        'attempts': attempts,
        'paused_until': _now_iso(paused) if paused else None,
        'brand': (brand or '').strip().lower() or None,
        'detail': detail,
    }
    if prev.get('key_expires'):
        rec['key_expires'] = prev['key_expires']
    _store(vid, rec)


def set_key_expires(vid, info) -> None:
    """Put a Pleos key's end date (``{"date", "source", "days_left"}``, or
    None) into the stored record, for a watcher that reads only the DB."""
    if vid is None:
        return
    try:
        new = {k: info.get(k) for k in ('date', 'source', 'days_left')} if info else None
        rec = _load(vid)
        if rec.get('key_expires') == new:
            return
        if new:
            rec['key_expires'] = new
        else:
            rec.pop('key_expires', None)
        _store(vid, rec)
    except Exception as e:
        logger.debug(f"key expiry for vehicle {vid} not recorded: {e}")


def note_ok(vid, brand) -> None:
    """A sign-in/sync of this vehicle succeeded."""
    if vid is None:
        return
    try:
        with _lock:
            _paused_until.pop(vid, None)
            _reject_streak.pop(vid, None)
            _pause_logged.discard(vid)
        _record(vid, brand, 'ok', datetime.now(), '', None, 0, True)
    except Exception as e:
        logger.debug(f"login state ok for vehicle {vid} not recorded: {e}")


def note_failure(vid, brand, exc) -> None:
    """A sign-in/sync of this vehicle failed with ``exc``."""
    if vid is None:
        return
    try:
        now = datetime.now()
        state = classify(exc)
        paused = None
        b = (brand or '').strip().lower()
        if state in BRAKE_STATES and (
                b in BRAKE_BRANDS
                or (b in KIND_BRAKE_BRANDS and _kind_state(exc) == state)):
            paused = _engage(vid, now)
        else:
            paused = paused_until(vid, now)
        prev = _load(vid)
        try:
            attempts = int(prev.get('attempts') or 0) if prev.get('state') != 'ok' else 0
        except (TypeError, ValueError):
            attempts = 0
        raw_kind = getattr(exc, 'kind', None)
        kind = raw_kind if isinstance(raw_kind, str) and raw_kind.isidentifier() else state
        _record(vid, brand, state, now, scrub(exc), paused, attempts + 1, False,
                kind=kind[:40])
    except Exception as e:
        logger.debug(f"login state failure for vehicle {vid} not recorded: {e}")


def read(vid):
    """The stored record, with ``paused_until`` as the brake sees it now.

    The pause lives in memory, so after a restart (or a lift) a stored end
    time is no longer true; the live value replaces it. None when nothing
    was recorded yet.
    """
    try:
        rec = _load(vid)
        if not rec:
            return None
        until = paused_until(vid)
        rec['paused_until'] = _now_iso(until) if until else None
        return rec
    except Exception:
        return None
