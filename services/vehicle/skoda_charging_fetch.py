"""Skoda charging-history fetcher.

Pulls ``get_charging_history`` from the MySkoda v3 API and upserts each
charging session into the ``charges`` table. Dedup: any existing Charge
whose ``date`` matches and whose ``charge_hour`` is within ±2 h of the
MySkoda session's start hour wins — we don't overwrite it. Otherwise a
new row gets inserted with:

- ``date`` and ``charge_hour`` from the session ``start_at``
- ``kwh_loaded`` from ``charged_in_kwh``
- ``charge_type`` from ``current_type`` (AC/DC)
- ``source``-style marker: ``notes='[MySkoda-Historie]'`` so the user
  can spot backfilled rows in the History view. ``needs_review=True``
  so the row shows up red until the user confirms the price.

MySkoda doesn't return the price the user actually paid (or pre/post
SoC) — those still have to be filled in manually after the backfill,
which is why we mark these rows ``needs_review``.
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional

from models.database import db, Charge

logger = logging.getLogger(__name__)


_DEDUP_HOURS = 2


def _parse_session_start(s):
    """Best-effort ISO-8601 → naive datetime. Accepts ``Z`` and ``+00:00``.
    Returns None on failure."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None) if s.tzinfo else s
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None


def _normalise_charge_type(ct: Optional[str]) -> str:
    """MySkoda returns ``AC`` / ``DC`` / sometimes a longer enum value
    like ``DC_QUICK``. Reduce to the two values our schema accepts."""
    if not ct:
        return 'AC'
    s = str(ct).upper()
    if 'DC' in s:
        return 'DC'
    if 'AC' in s:
        return 'AC'
    return 'AC'


def fetch_skoda_charging(days: int = 90,
                         vehicle_id: Optional[int] = None,
                         email: Optional[str] = None,
                         password: Optional[str] = None,
                         vin: Optional[str] = None,
                         limit: int = 200) -> dict:
    """Walk the MySkoda charging history for the last ``days`` and
    upsert sessions into Charge. Returns summary counts.

    Dedup: a session whose start_at falls on the same calendar date AND
    within ±2 h of an existing Charge.charge_hour for the same vehicle
    is considered already-recorded — we touch nothing. Sessions that
    don't match are inserted with ``needs_review=True``.
    """
    from services.vehicle.myskoda_client import MySkodaSync, HAS_MYSKODA
    from models.database import AppConfig, Vehicle

    out = {'inserted': 0, 'skipped_dedup': 0, 'periods_seen': 0,
           'sessions_seen': 0, 'days_requested': days}

    if not HAS_MYSKODA:
        out['error'] = 'myskoda_lib_not_installed'
        return out

    if not (email and password and vin):
        if vehicle_id is not None:
            v = Vehicle.query.get(vehicle_id)
            if v is not None:
                email = email or v.api_username
                password = password or v.api_password
                vin = vin or v.api_vin
        if not (email and password):
            email = email or AppConfig.get('vehicle_api_username', '')
            password = password or AppConfig.get('vehicle_api_password', '')
            vin = vin or AppConfig.get('vehicle_api_vin', '')

    if not (email and password and vin):
        out['error'] = 'missing_credentials'
        return out

    # Same probe/cache pattern as the aggregate stats endpoint —
    # the Enyaq 60 reference install returns 500 here, presumably
    # because the subscription tier doesn't include it. Skip the
    # API call entirely on a cached 'unsupported' until the next
    # weekly re-probe.
    from models.database import AppConfig as _AC
    supported_key = f'skoda_charging_history_supported_{vehicle_id}'
    last_probe_key = f'skoda_charging_history_last_probe_{vehicle_id}'
    last_probe_raw = _AC.get(last_probe_key, '') or ''
    last_probe = None
    if last_probe_raw:
        try:
            last_probe = date.fromisoformat(last_probe_raw)
        except ValueError:
            last_probe = None
    supported_raw = (_AC.get(supported_key, '') or '').lower()
    if (supported_raw == 'false' and last_probe is not None
            and (date.today() - last_probe) < timedelta(days=7)):
        out['error'] = 'unsupported_cached'
        return out

    client = MySkodaSync(email=email, password=password, vin=vin)
    end_dt = datetime.now().replace(microsecond=0)
    start_dt = end_dt - timedelta(days=max(days, 1))
    result = client.get_charging_history(start=start_dt, end=end_dt, limit=limit)
    if result is None:
        _AC.set(supported_key, 'false')
        _AC.set(last_probe_key, date.today().isoformat())
        out['error'] = 'fetch_failed'
        return out
    _AC.set(supported_key, 'true')
    _AC.set(last_probe_key, date.today().isoformat())

    periods = getattr(result, 'periods', None) or []
    out['periods_seen'] = len(periods)

    for period in periods:
        sessions = getattr(period, 'sessions', None) or []
        for session in sessions:
            out['sessions_seen'] += 1
            start_at = _parse_session_start(getattr(session, 'start_at', None))
            kwh = getattr(session, 'charged_in_kwh', None)
            ct = getattr(session, 'current_type', None)
            if start_at is None or kwh is None:
                continue

            session_date = start_at.date()
            session_hour = start_at.hour

            dedup_q = Charge.query.filter(Charge.date == session_date)
            if vehicle_id is not None:
                dedup_q = dedup_q.filter(Charge.vehicle_id == vehicle_id)
            dupe = False
            for cand in dedup_q.all():
                ch = cand.charge_hour
                if ch is None or abs(ch - session_hour) <= _DEDUP_HOURS:
                    dupe = True
                    break
            if dupe:
                out['skipped_dedup'] += 1
                continue

            row = Charge(
                vehicle_id=vehicle_id,
                date=session_date,
                charge_hour=session_hour,
                kwh_loaded=float(kwh),
                charge_type=_normalise_charge_type(ct),
                needs_review=True,
                notes='[MySkoda-Historie]',
                created_at=datetime.now(),
            )
            db.session.add(row)
            out['inserted'] += 1
    db.session.commit()
    logger.info(
        f"skoda charging-history backfill: {out['periods_seen']} periods, "
        f"{out['sessions_seen']} sessions, +{out['inserted']} new / "
        f"{out['skipped_dedup']} skipped (dedup)"
    )
    return out
