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


def _combine_day_and_time(day, end_time_str):
    """myskoda's Trip.end_time is just ``HH:MM`` (not a full timestamp);
    the date sits on the enclosing DailyTrip. Combine them into a naive
    datetime, returning None on any parse failure.

    Note: the trip may have STARTED on the previous day if the drive
    crossed midnight. travel_time_in_min lets us detect that case
    downstream, but ``end_time`` belongs unambiguously to ``day``.
    """
    if day is None or end_time_str is None:
        return None
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except (TypeError, ValueError):
            return None
    if isinstance(end_time_str, datetime):
        return end_time_str.replace(tzinfo=None) if end_time_str.tzinfo else end_time_str
    s = str(end_time_str).strip()
    # Accept "HH:MM" or "HH:MM:SS"
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            tt = datetime.strptime(s, fmt).time()
            return datetime.combine(day, tt)
        except ValueError:
            continue
    # Fall back to full ISO parse for forward-compat if the API ever
    # starts returning a full timestamp.
    try:
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

    if not (email and password):
        out['error'] = 'missing_credentials'
        return out

    # Auto-discover VIN if the Vehicle row doesn't have one set yet,
    # and persist it back so we don't pay the discovery round-trip
    # on every subsequent fetch.
    if not vin:
        bootstrap = MySkodaSync(email=email, password=password, vin=None)
        vins = bootstrap.list_vins()
        if not vins:
            out['error'] = 'no_vins_in_account'
            return out
        vin = vins[0]
        out['vin_discovered'] = vin
        if vehicle_id is not None:
            v = Vehicle.query.get(vehicle_id)
            if v is not None and not (v.api_vin or '').strip():
                v.api_vin = vin
                db.session.commit()
                out['vin_persisted'] = True

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

    # The TripStatistics aggregate (a separate myskoda endpoint, but
    # included in the same SingleTrips payload as ``statistics``)
    # carries day-level averages for recuperation and electric
    # consumption. Build a {date → (regen, consumption)} lookup so
    # we can stamp each Trip row with the day's averages — per-trip
    # values aren't exposed.
    daily_stats = {}
    stats = getattr(result, 'statistics', None)
    if stats is not None:
        entries = getattr(stats, 'statistics_entries', None) or []
        for e in entries:
            d = getattr(e, 'date', None)
            if isinstance(d, str):
                try:
                    d = date.fromisoformat(d)
                except (TypeError, ValueError):
                    d = None
            if d is None:
                continue
            daily_stats[d] = (
                getattr(e, 'average_recuperation', None),
                getattr(e, 'average_electric_consumption', None),
            )

    for daily in daily_trips:
        # daily.date is a string like "2026-06-09"
        try:
            day = date.fromisoformat(daily.date) if isinstance(daily.date, str) else daily.date
        except (TypeError, ValueError):
            day = None
        trips = getattr(daily, 'trips', None) or []
        for trip in trips:
            out['trips_seen'] += 1
            end_time = _combine_day_and_time(day, getattr(trip, 'end_time', None))
            travel_min = getattr(trip, 'travel_time_in_min', None)
            if end_time is None or travel_min is None:
                logger.debug(
                    f"skoda trip skipped — end_time={end_time!r} "
                    f"travel_min={travel_min!r}")
                continue
            start_time = end_time - timedelta(minutes=int(travel_min))
            distance = getattr(trip, 'mileage_in_km', None)
            avg_speed = getattr(trip, 'average_speed_in_kmph', None)
            day_regen, day_consumption = daily_stats.get(day, (None, None))

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
                    regen_kwh_per_100km=float(day_regen) if day_regen is not None else None,
                    consumption_kwh_per_100km=float(day_consumption) if day_consumption is not None else None,
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
                    ('regen_kwh_per_100km', float(day_regen) if day_regen is not None else None),
                    ('consumption_kwh_per_100km', float(day_consumption) if day_consumption is not None else None),
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
