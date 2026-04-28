"""Fetch per-trip data from Kia UVO / Hyundai Bluelink servers.

The hyundai_kia_connect_api SDK exposes ``update_day_trip_info(vehicle_id,
yyyymmdd)`` which hits the ``/spa/vehicles/<id>/tripinfo`` endpoint — the
same server-side cache the Bluelink/UVO mobile apps read from. The car
itself uploads a trip record at the end of every drive as part of its
normal telemetry, independent of anything we do, so this pull:

  - does NOT wake the vehicle (no 12V drain)
  - returns the authoritative per-trip list (start time, drive/idle
    minutes, distance, avg/max speed) — the detail level Bluelink shows
  - costs exactly one API call per day requested, counted against the
    200/vehicle daily quota the rest of this app already observes.

Since v2.26 this is a **manual backfill tool only**. The primary trip
log is ParkingEvent-pair based (polled GPS) — see services/trips_service
for the rationale. SDK rows surface in the UI only on historical days
where zero ParkingEvent pairs exist, and as stats enrichment (drive/idle
minutes, avg/max speed) on polled trips whose start time matches.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Optional

from models.database import db, VehicleTrip, AppConfig

logger = logging.getLogger(__name__)

# Only Kia UVO and Hyundai Bluelink currently expose trip-info endpoints
# in the EU region we target. Other brands stay on the ParkingEvent path.
SDK_TRIP_BRANDS = {'kia', 'hyundai'}

# Default backfill window when the user manually triggers one.
DEFAULT_BACKFILL_DAYS = 30


def _parse_start_time(trip_date: date, hhmmss: str) -> Optional[datetime]:
    """Combine yyyy-mm-dd + 'HHMMSS' → naive datetime.

    The SDK returns hhmmss as a 6-char zero-padded string. Occasionally
    we've seen 5-char values ('12345' = 01:23:45) — pad defensively.
    """
    if not hhmmss:
        return None
    s = str(hhmmss).zfill(6)
    try:
        hh = int(s[0:2]); mm = int(s[2:4]); ss = int(s[4:6])
        if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
            return None
        return datetime.combine(trip_date, datetime.min.time()).replace(
            hour=hh, minute=mm, second=ss, microsecond=0
        )
    except (TypeError, ValueError):
        return None


def _counter_keys(vehicle_id):
    """Return (date_key, count_key) — Vehicle#1 keeps the legacy names
    so an in-flight 200/day budget survives the v2.29 upgrade; the
    rest get vehicle-id-prefixed keys.
    """
    if vehicle_id is None or vehicle_id == 1:
        return 'vehicle_api_counter_date', 'vehicle_api_counter'
    return f'vehicle_{vehicle_id}_api_counter_date', f'vehicle_{vehicle_id}_api_counter'


def _count_api_call(vehicle_id=None):
    """Bump the per-vehicle daily API counter so the 200/account budget
    tracking stays accurate across polling + trip-info calls."""
    today_str = date.today().isoformat()
    date_key, count_key = _counter_keys(vehicle_id)
    if AppConfig.get(date_key, '') != today_str:
        AppConfig.set(date_key, today_str)
        AppConfig.set(count_key, '0')
    try:
        n = int(AppConfig.get(count_key, '0'))
    except (TypeError, ValueError):
        n = 0
    AppConfig.set(count_key, str(n + 1))


def fetch_day_trip_info(target_date: date, vehicle_id=None) -> dict:
    """Fetch and store the trip list for a single day.

    v2.29: when ``vehicle_id`` is set, use that Vehicle row's brand /
    creds / per-vehicle API counter. None falls back to the legacy
    AppConfig path for single-car installs that haven't migrated yet.

    Returns ``{'date': 'YYYY-MM-DD', 'added': int, 'updated': int,
    'total_sdk_trips': int, 'skipped_reason': str | None}``.
    """
    out = {
        'date': target_date.isoformat(),
        'added': 0, 'updated': 0, 'total_sdk_trips': 0,
        'skipped_reason': None,
        'vehicle_id': vehicle_id,
    }

    # Resolve brand + creds either from the Vehicle row (v2.29) or
    # from the legacy AppConfig path.
    veh = None
    if vehicle_id is not None:
        from models.database import Vehicle
        veh = Vehicle.query.get(vehicle_id)
        if veh is None:
            out['skipped_reason'] = f'vehicle_id {vehicle_id} not found'
            return out
        brand = (veh.api_brand or '').lower()
        creds = {
            'username': veh.api_username or '',
            'password': veh.api_password or '',
            'pin':      veh.api_pin or '',
            'region':   veh.api_region or 'EU',
            'vin':      veh.api_vin or '',
        }
    else:
        brand = (AppConfig.get('vehicle_api_brand', '') or '').lower()
        creds = {
            'username': AppConfig.get('vehicle_api_username', ''),
            'password': AppConfig.get('vehicle_api_password', ''),
            'pin':      AppConfig.get('vehicle_api_pin', ''),
            'region':   AppConfig.get('vehicle_api_region', 'EU'),
            'vin':      AppConfig.get('vehicle_api_vin', ''),
        }

    if brand not in SDK_TRIP_BRANDS:
        out['skipped_reason'] = f'brand {brand!r} has no SDK trip endpoint'
        return out

    # Stay inside the 200/day limit. Leave 10 calls headroom for the
    # regular polling loop so we never starve it out.
    _, count_key = _counter_keys(vehicle_id)
    try:
        api_count = int(AppConfig.get(count_key, '0'))
    except (TypeError, ValueError):
        api_count = 0
    if api_count >= 190:
        out['skipped_reason'] = f'daily API budget near limit ({api_count}/200)'
        return out

    from services.vehicle import get_connector

    try:
        connector = get_connector(brand, creds)
        connector._ensure_auth()
        mgr = connector._get_manager()
        vehicle = connector._get_vehicle()
        _count_api_call(vehicle_id)
        mgr.update_day_trip_info(vehicle.id, target_date.strftime('%Y%m%d'))
        day_info = vehicle.day_trip_info
    except Exception as e:
        out['skipped_reason'] = f'API error: {type(e).__name__}: {e}'
        logger.warning(f"Trip-info fetch failed for {target_date}: {e}")
        return out

    if not day_info or not getattr(day_info, 'trip_list', None):
        out['skipped_reason'] = 'no trips reported for this day'
        return out

    out['total_sdk_trips'] = len(day_info.trip_list)

    for trip in day_info.trip_list:
        start = _parse_start_time(target_date, getattr(trip, 'hhmmss', None))
        if start is None:
            # Shouldn't happen in practice; skip rather than insert garbage.
            continue
        # Per-vehicle uniqueness: same start_time on two different
        # cars is legitimate (parallel fleet). Scope by vehicle_id.
        existing_q = VehicleTrip.query.filter_by(start_time=start)
        if vehicle_id is not None:
            existing_q = existing_q.filter_by(vehicle_id=vehicle_id)
        existing = existing_q.first()
        if existing is None:
            row = VehicleTrip(
                vehicle_id=vehicle_id,
                trip_date=target_date,
                start_time=start,
                drive_minutes=getattr(trip, 'drive_time', None),
                idle_minutes=getattr(trip, 'idle_time', None),
                distance_km=getattr(trip, 'distance', None),
                avg_speed_kmh=getattr(trip, 'avg_speed', None),
                max_speed_kmh=getattr(trip, 'max_speed', None),
                source='sdk_day_trip_info',
                fetched_at=datetime.now(),
            )
            db.session.add(row)
            out['added'] += 1
        else:
            # Re-fetching the same day is allowed — update in place so
            # an early-morning fetch doesn't leave stale numbers when
            # the user adds trips later in the day.
            changed = False
            for attr, val in [
                ('drive_minutes',  getattr(trip, 'drive_time', None)),
                ('idle_minutes',   getattr(trip, 'idle_time', None)),
                ('distance_km',    getattr(trip, 'distance', None)),
                ('avg_speed_kmh',  getattr(trip, 'avg_speed', None)),
                ('max_speed_kmh',  getattr(trip, 'max_speed', None)),
            ]:
                if val is not None and getattr(existing, attr) != val:
                    setattr(existing, attr, val)
                    changed = True
            if changed:
                existing.fetched_at = datetime.now()
                out['updated'] += 1

    db.session.commit()
    logger.info(
        f"Trip-info {target_date.isoformat()}: {out['total_sdk_trips']} trips "
        f"from server, +{out['added']} new / ~{out['updated']} updated"
    )

    # Now that we have fresh SDK rows for this day, realign the PE-pair
    # departed_at for the same day where a clean 1:1 match exists.
    # trip_reconcile brand-gates itself to Kia + Hyundai, so other brands
    # safely no-op. Wrapped so a reconcile failure never blocks the fetch.
    try:
        from services.trip_reconcile import reconcile_day
        r = reconcile_day(target_date, vehicle_id=vehicle_id)
        out['reconciled_applied'] = r.get('applied', 0)
        out['reconciled_skipped'] = r.get('skipped_conflict', 0)
    except Exception as e:
        logger.warning(f"trip_reconcile inline after {target_date}: {e}")

    return out


def backfill(days: int = DEFAULT_BACKFILL_DAYS, vehicle_id=None) -> dict:
    """Manually-triggered bulk backfill. Walks the past ``days`` days
    and fetches each. Stops early if we hit the API quota so a single
    big backfill can't lock the user out of the rest of the day.

    v2.29: ``vehicle_id`` scopes the entire walk to one car. None
    falls through to the legacy single-vehicle path.
    """
    today = date.today()
    results = []
    stopped_early = False
    for i in range(days):
        d = today - timedelta(days=i)
        r = fetch_day_trip_info(d, vehicle_id=vehicle_id)
        results.append(r)
        reason = r.get('skipped_reason') or ''
        if reason.startswith('daily API budget'):
            stopped_early = True
            break
    if vehicle_id is None or vehicle_id == 1:
        AppConfig.set('last_trip_fetch_at', datetime.now().isoformat())
    else:
        AppConfig.set(f'last_trip_fetch_at_{vehicle_id}', datetime.now().isoformat())
    return {
        'days_attempted': len(results),
        'added': sum(r['added'] for r in results),
        'updated': sum(r['updated'] for r in results),
        'stopped_early': stopped_early,
        'results': results,
        'vehicle_id': vehicle_id,
    }
