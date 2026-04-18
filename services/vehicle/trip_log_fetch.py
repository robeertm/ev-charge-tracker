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


def _count_api_call():
    """Bump the shared vehicle_api_counter so the daily 200/vehicle budget
    tracking stays accurate across polling + trip-info calls."""
    today_str = date.today().isoformat()
    counter_date = AppConfig.get('vehicle_api_counter_date', '')
    if counter_date != today_str:
        AppConfig.set('vehicle_api_counter_date', today_str)
        AppConfig.set('vehicle_api_counter', '0')
    try:
        n = int(AppConfig.get('vehicle_api_counter', '0'))
    except (TypeError, ValueError):
        n = 0
    AppConfig.set('vehicle_api_counter', str(n + 1))


def fetch_day_trip_info(target_date: date) -> dict:
    """Fetch and store the trip list for a single day.

    Returns ``{'date': 'YYYY-MM-DD', 'added': int, 'updated': int,
    'total_sdk_trips': int, 'skipped_reason': str | None}``.
    """
    out = {
        'date': target_date.isoformat(),
        'added': 0, 'updated': 0, 'total_sdk_trips': 0,
        'skipped_reason': None,
    }

    brand = AppConfig.get('vehicle_api_brand', '')
    if brand not in SDK_TRIP_BRANDS:
        out['skipped_reason'] = f'brand {brand!r} has no SDK trip endpoint'
        return out

    # Stay inside the 200/day limit. Leave 10 calls headroom for the
    # regular polling loop so we never starve it out.
    try:
        api_count = int(AppConfig.get('vehicle_api_counter', '0'))
    except (TypeError, ValueError):
        api_count = 0
    if api_count >= 190:
        out['skipped_reason'] = f'daily API budget near limit ({api_count}/200)'
        return out

    from services.vehicle import get_connector
    creds = {
        'username': AppConfig.get('vehicle_api_username', ''),
        'password': AppConfig.get('vehicle_api_password', ''),
        'pin':      AppConfig.get('vehicle_api_pin', ''),
        'region':   AppConfig.get('vehicle_api_region', 'EU'),
        'vin':      AppConfig.get('vehicle_api_vin', ''),
    }

    try:
        connector = get_connector(brand, creds)
        connector._ensure_auth()
        mgr = connector._get_manager()
        vehicle = connector._get_vehicle()
        _count_api_call()
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
        existing = VehicleTrip.query.filter_by(start_time=start).first()
        if existing is None:
            row = VehicleTrip(
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
    return out


def backfill(days: int = DEFAULT_BACKFILL_DAYS) -> dict:
    """Manually-triggered bulk backfill. Walks the past ``days`` days
    and fetches each. Stops early if we hit the API quota so a single
    big backfill can't lock the user out of the rest of the day."""
    today = date.today()
    results = []
    stopped_early = False
    for i in range(days):
        d = today - timedelta(days=i)
        r = fetch_day_trip_info(d)
        results.append(r)
        reason = r.get('skipped_reason') or ''
        if reason.startswith('daily API budget'):
            stopped_early = True
            break
    AppConfig.set('last_trip_fetch_at', datetime.now().isoformat())
    return {
        'days_attempted': len(results),
        'added': sum(r['added'] for r in results),
        'updated': sum(r['updated'] for r in results),
        'stopped_early': stopped_early,
        'results': results,
    }
