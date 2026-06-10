"""Skoda trip-statistics fetcher.

Pulls ``get_single_trip_statistics`` from the MySkoda v3 API and stores
each individual trip into ``VehicleTrip`` with ``source='myskoda'``. The
trips_service / /trips view already overlays VehicleTrip rows as the
SDK-side trip stats (drive minutes, distance, avg speed), so populating
this table is enough to make Skoda trips show up in the Fahrtenbuch
without any template change.

myskoda's ``Trip`` dataclass reports ``end_time`` + ``travel_time_in_min``
but not a start timestamp; we derive ``start_time = end_time - travel_time``.
"""
import logging
from datetime import datetime, date, timedelta, time
from typing import Optional

from models.database import db, VehicleTrip

logger = logging.getLogger(__name__)


def _parse_dt(s):
    """Best-effort ISO-8601 → naive datetime parser.

    myskoda returns end_time as an ISO string like ``2026-06-09T17:43:00Z``
    or with a ``+00:00`` offset. We store naive datetimes elsewhere in
    the schema so strip the tz info after parsing.
    """
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None) if s.tzinfo else s
    try:
        # Python 3.11+ tolerates the trailing Z; older versions don't.
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None


def fetch_skoda_trips(days: int = 30,
                      vehicle_id: Optional[int] = None,
                      email: Optional[str] = None,
                      password: Optional[str] = None,
                      vin: Optional[str] = None) -> dict:
    """Pull the last ``days`` days of trip statistics and upsert into
    ``VehicleTrip``.

    Credentials are resolved in this order:
    - explicit ``email``/``password``/``vin`` arguments
    - the ``Vehicle`` row for ``vehicle_id``
    - the legacy single-vehicle ``AppConfig`` ``vehicle_api_*`` keys

    Returns a summary dict with counts (``added``, ``updated``,
    ``daily_trips``, ``trips_seen``, optional ``error``).
    """
    from services.vehicle.myskoda_client import MySkodaSync, HAS_MYSKODA
    from models.database import AppConfig, Vehicle

    out = {'added': 0, 'updated': 0, 'daily_trips': 0, 'trips_seen': 0,
           'days_requested': days}

    if not HAS_MYSKODA:
        out['error'] = 'myskoda_lib_not_installed'
        return out

    # Resolve credentials
    if not (email and password and vin):
        if vehicle_id is not None:
            v = Vehicle.query.get(vehicle_id)
            if v is not None:
                email = email or v.api_username
                password = password or v.api_password
                vin = vin or v.api_vin
        if not (email and password):
            # Legacy single-vehicle fallback
            email = email or AppConfig.get('vehicle_api_username', '')
            password = password or AppConfig.get('vehicle_api_password', '')
            vin = vin or AppConfig.get('vehicle_api_vin', '')

    if not (email and password and vin):
        out['error'] = 'missing_credentials'
        return out

    client = MySkodaSync(email=email, password=password, vin=vin)
    end_dt = datetime.combine(date.today(), time(23, 59, 59))
    start_dt = datetime.combine(date.today() - timedelta(days=max(days, 1) - 1),
                                time(0, 0, 0))
    result = client.get_single_trip_statistics(start=start_dt, end=end_dt)
    if result is None:
        out['error'] = 'fetch_failed'
        return out

    daily_trips = getattr(result, 'daily_trips', None) or []
    out['daily_trips'] = len(daily_trips)

    for daily in daily_trips:
        # daily.date is a string like "2026-06-09"
        try:
            day = date.fromisoformat(daily.date) if isinstance(daily.date, str) else daily.date
        except (TypeError, ValueError):
            day = None
        trips = getattr(daily, 'trips', None) or []
        for trip in trips:
            out['trips_seen'] += 1
            end_time = _parse_dt(getattr(trip, 'end_time', None))
            travel_min = getattr(trip, 'travel_time_in_min', None)
            if end_time is None or travel_min is None:
                logger.debug(
                    f"skoda trip skipped — end_time={end_time!r} "
                    f"travel_min={travel_min!r}")
                continue
            start_time = end_time - timedelta(minutes=int(travel_min))
            distance = getattr(trip, 'mileage_in_km', None)
            avg_speed = getattr(trip, 'average_speed_in_kmph', None)

            existing_q = VehicleTrip.query.filter_by(start_time=start_time)
            if vehicle_id is not None:
                existing_q = existing_q.filter_by(vehicle_id=vehicle_id)
            existing = existing_q.first()
            if existing is None:
                row = VehicleTrip(
                    vehicle_id=vehicle_id,
                    trip_date=day or end_time.date(),
                    start_time=start_time,
                    drive_minutes=int(travel_min) if travel_min is not None else None,
                    idle_minutes=None,
                    distance_km=float(distance) if distance is not None else None,
                    avg_speed_kmh=float(avg_speed) if avg_speed is not None else None,
                    max_speed_kmh=None,
                    source='myskoda',
                    fetched_at=datetime.now(),
                )
                db.session.add(row)
                out['added'] += 1
            else:
                changed = False
                for attr, val in [
                    ('drive_minutes', int(travel_min) if travel_min is not None else None),
                    ('distance_km', float(distance) if distance is not None else None),
                    ('avg_speed_kmh', float(avg_speed) if avg_speed is not None else None),
                ]:
                    if val is not None and getattr(existing, attr) != val:
                        setattr(existing, attr, val)
                        changed = True
                if changed:
                    existing.fetched_at = datetime.now()
                    if existing.source != 'myskoda':
                        # An sdk_day_trip_info row from a different brand
                        # period got overwritten — should never happen
                        # in practice but keep the audit trail.
                        existing.source = 'myskoda'
                    out['updated'] += 1
    db.session.commit()
    logger.info(
        f"skoda trip fetch: {out['daily_trips']} days, {out['trips_seen']} trips, "
        f"+{out['added']} new / ~{out['updated']} updated"
    )
    return out
