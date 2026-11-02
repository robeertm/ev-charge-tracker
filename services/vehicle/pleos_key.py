# -*- coding: utf-8 -*-
"""When does a car's Pleos key run out — and how sure are we of the date?

A personal Pleos key (Client ID + Client Secret) has a validity period.
When it ends, every access token issued with it stops working
(document.pleos.ai, "Manage My Vehicle API Key"), and the car silently
stops syncing. The documentation names no period for the personal key and
no endpoint that returns its end date; the portal shows it under
"API key status". So the date comes from one of three places, best first:

``read``
    The token answer or the token's payload carried a field that clearly
    belongs to the KEY (see ``pleos_api.key_expiry_from``). The token's
    own ``exp`` is not that and never counts.
``entered``
    The owner typed the date from the portal into the optional
    "Key valid until" box of the Pleos credential form.
``estimated``
    First successful token call with this key + 90 days. The 90 days are
    a community figure, not documented; the UI therefore says "approx."
    and points to the portal. A new Client ID/Secret starts it afresh.

The result, ``{"date": "YYYY-MM-DD", "source": ..., "days_left": int}``,
is shown in /api/sync/health (``vehicles[].login.key_expires``), stored in
the ``vehicle_<id>_login_state`` record (``key_expires``) for a watcher
that only reads the database, and reduced to ``key_expires_days`` +
``source`` in the read-only API.

Nothing here stores or logs a secret: the key is only ever referred to by
its fingerprint (a truncated SHA-256, as in the token cache).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

KEY = 'vehicle_{vid}_pleos_key'

SOURCES = ('read', 'entered', 'estimated')
ESTIMATE_DAYS = 90
#: From here on the UI mentions the end date (unobtrusively).
NOTICE_DAYS = 14
#: From here on (and after the end) it says so clearly.
URGENT_DAYS = 3


def is_pleos(brand) -> bool:
    return str(brand or '').strip().lower().endswith('_pleos')


def _pleos_brand(brand) -> str:
    return str(brand or '').strip().lower().split('_', 1)[0]


def _creds(vehicle) -> tuple:
    return ((getattr(vehicle, 'api_username', '') or '').strip(),
            (getattr(vehicle, 'api_password', '') or '').strip())


def _today(today=None) -> date:
    if isinstance(today, datetime):
        return today.date()
    return today or date.today()


def parse_date(value) -> Optional[str]:
    """'YYYY-MM-DD' for a typed date (also 'DD.MM.YYYY'), else None."""
    s = str(value or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if 2020 <= d.year <= 2100:
            return d.isoformat()
    return None


# ── persisted per vehicle ─────────────────────────────────────────────
def _load(vid) -> dict:
    try:
        from models.database import AppConfig
        raw = AppConfig.get(KEY.format(vid=vid), '') or ''
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        _rollback()
        return {}


def _store(vid, rec: dict) -> None:
    try:
        from models.database import AppConfig
        AppConfig.set(KEY.format(vid=vid), json.dumps(rec))
    except Exception as e:
        logger.debug(f"Pleos key record for vehicle {vid} not stored: {e}")
        _rollback()


def _rollback() -> None:
    try:
        from models.database import db
        db.session.rollback()
    except Exception:
        pass


def note_success(vehicle, today=None) -> None:
    """A token was obtained for this car: start (or keep) the estimate and
    keep a date the API stated. A different key starts from scratch."""
    try:
        brand = getattr(vehicle, 'api_brand', '')
        if not is_pleos(brand):
            return
        from .pleos_api import fingerprint, key_seen
        cid, secret = _creds(vehicle)
        if not cid or not secret:
            return
        fp = fingerprint(_pleos_brand(brand), cid, secret)
        seen = key_seen(_pleos_brand(brand), cid, secret)
        if not seen:
            return              # no token was obtained with this key yet
        prev = _load(vehicle.id)
        rec = dict(prev)
        if prev.get('fp') != fp:
            first = _today(today)
            if seen.get('first_ok'):
                first = min(first, datetime.fromtimestamp(seen['first_ok']).date())
            rec = {'fp': fp, 'first_ok': first.isoformat(), 'read': None}
        if seen.get('read'):
            rec['read'] = seen['read']
        if rec != prev:
            _store(vehicle.id, rec)
    except Exception as e:
        logger.debug(f"Pleos key success for vehicle {getattr(vehicle, 'id', '?')} not noted: {e}")


def expiry(vehicle, today=None) -> Optional[dict]:
    """``{"date", "source", "days_left"}`` for this car's key, or None."""
    try:
        brand = getattr(vehicle, 'api_brand', '')
        if not is_pleos(brand):
            return None
        cid, secret = _creds(vehicle)
        rec = _load(vehicle.id)
        same_key = False
        if cid and secret and rec.get('fp'):
            from .pleos_api import fingerprint
            same_key = rec['fp'] == fingerprint(_pleos_brand(brand), cid, secret)
        when, source = None, None
        if same_key and parse_date(rec.get('read')):
            when, source = parse_date(rec['read']), 'read'
        elif parse_date(getattr(vehicle, 'api_key_valid_until', None)):
            when, source = parse_date(vehicle.api_key_valid_until), 'entered'
        elif same_key and parse_date(rec.get('first_ok')):
            first = date.fromisoformat(parse_date(rec['first_ok']))
            when, source = (first + timedelta(days=ESTIMATE_DAYS)).isoformat(), 'estimated'
        if not when:
            return None
        days_left = (date.fromisoformat(when) - _today(today)).days
        return {'date': when, 'source': source, 'days_left': days_left}
    except Exception as e:
        logger.debug(f"Pleos key expiry for vehicle {getattr(vehicle, 'id', '?')} unknown: {e}")
        return None


def level(days_left) -> Optional[str]:
    """How loudly to mention the end date: None (> 14 days), ``soon``
    (4–14 days), ``urgent`` (0–3 days, 0 = last valid day), ``expired``."""
    if days_left is None:
        return None
    try:
        d = int(days_left)
    except (TypeError, ValueError):
        return None
    if d < 0:
        return 'expired'
    if d <= URGENT_DAYS:
        return 'urgent'
    if d <= NOTICE_DAYS:
        return 'soon'
    return None


def near_expiry(info: Optional[dict]) -> bool:
    """Is a refusal of this key most likely its end date?

    A stated date (read/entered) is exact: within three days of it. The
    estimate is only a guess, and the key may well have been created a few
    days before this app first used it: within the whole notice window.
    """
    if not info:
        return False
    try:
        d = int(info.get('days_left'))
    except (TypeError, ValueError):
        return False
    limit = NOTICE_DAYS if info.get('source') == 'estimated' else URGENT_DAYS
    return d <= limit


def refine_failure(vehicle, exc, today=None):
    """Turn ``key_invalid`` into ``key_expired`` when the end date is near
    or past. Returns the exception to record (the same one otherwise)."""
    try:
        if getattr(exc, 'kind', None) != 'key_invalid':
            return exc
        if not near_expiry(expiry(vehicle, today=today)):
            return exc
        from .pleos_api import PleosError
        from services.i18n import t
        return PleosError('key_expired', t('pleos.err_key_expired'),
                          status=getattr(exc, 'status', 0), code=getattr(exc, 'code', ''))
    except Exception:
        return exc


def for_readonly(info: Optional[dict]) -> Optional[dict]:
    """What the read-only API shows: days left and the source, no date."""
    if not info:
        return None
    return {'key_expires_days': info.get('days_left'), 'source': info.get('source')}


def hint(vehicle, today=None) -> Optional[dict]:
    """The UI hint for this car: ``{level, text, estimated_note}`` or None."""
    info = expiry(vehicle, today=today)
    lvl = level(info.get('days_left')) if info else None
    if not lvl:
        return None
    from services.i18n import t
    try:
        shown = date.fromisoformat(info['date']).strftime('%d.%m.%Y')
    except Exception:
        shown = info['date']
    est = info['source'] == 'estimated'
    if est:
        shown = t('pleos.key_approx', date=shown)
    days = info['days_left']
    if lvl == 'expired':
        text = t('pleos.key_expired_since', date=shown, days=-days)
    elif days == 0:
        text = t('pleos.key_expires_today', date=shown)
    else:
        text = t('pleos.key_expires_in', date=shown, days=days)
    return {'level': lvl, 'text': text,
            'estimated_note': t('pleos.key_estimated_note') if est else '',
            'source': info['source'], 'days_left': days}
