"""Driving log / trips service.

Derives parking events from VehicleSync rows and groups them into trips.

Park-event lifecycle:
- A new ParkingEvent is opened the first time we see the car at a fresh
  location (>100 m from the last open one).
- The previous open event is closed (departed_at + odometer_departed +
  soc_departed) the moment movement is detected.
- The latest event for a fully-stopped car stays open with departed_at = NULL.

Trips are derived implicitly: each closed parking event has a successor
event whose arrival defines the trip end. Trip distance = odo difference.

We deliberately do NOT compute trip duration or average speed: with a
sparse polling cadence (even in smart mode) the "arrived_at" of the next
event can be up to the polling interval late, so any duration/speed
figure would mislead. Km from the odometer is rock-solid — that's what
we report.
"""
from __future__ import annotations

import bisect
import json
import math
from datetime import datetime, timedelta
from typing import Optional

from models.database import db, ParkingEvent, AppConfig


# Move thresholds (meters) — small enough to catch real movement but large
# enough that GPS noise from a stationary car doesn't open new events.
MOVE_THRESHOLD_M = 100.0
SAME_PLACE_M = 80.0  # within this radius we consider it "the same spot"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two coordinates."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _load_locations():
    """Return dict {home, work, favorites[]} from AppConfig."""
    out = {'home': None, 'work': None, 'favorites': []}
    try:
        h_lat = AppConfig.get('home_lat')
        h_lon = AppConfig.get('home_lon')
        if h_lat and h_lon:
            out['home'] = {'lat': float(h_lat), 'lon': float(h_lon),
                           'label': AppConfig.get('home_label', 'Home')}
    except (ValueError, TypeError):
        pass
    try:
        w_lat = AppConfig.get('work_lat')
        w_lon = AppConfig.get('work_lon')
        if w_lat and w_lon:
            out['work'] = {'lat': float(w_lat), 'lon': float(w_lon),
                           'label': AppConfig.get('work_label', 'Work')}
    except (ValueError, TypeError):
        pass
    try:
        favs_raw = AppConfig.get('favorite_locations', '[]')
        favs = json.loads(favs_raw)
        if isinstance(favs, list):
            for f in favs:
                if isinstance(f, dict) and 'lat' in f and 'lon' in f:
                    out['favorites'].append(f)
    except (ValueError, json.JSONDecodeError):
        pass
    return out


def _classify_location(lat: float, lon: float, locations=None):
    """Return (label, favorite_name) for a coordinate based on saved locations."""
    if locations is None:
        locations = _load_locations()
    if locations['home'] and _haversine_m(lat, lon, locations['home']['lat'],
                                          locations['home']['lon']) <= 200.0:
        return ('home', locations['home'].get('label', 'Home'))
    if locations['work'] and _haversine_m(lat, lon, locations['work']['lat'],
                                          locations['work']['lon']) <= 200.0:
        return ('work', locations['work'].get('label', 'Work'))
    for fav in locations['favorites']:
        try:
            if _haversine_m(lat, lon, float(fav['lat']), float(fav['lon'])) <= 200.0:
                return ('favorite', fav.get('name', 'Favorite'))
        except (ValueError, TypeError, KeyError):
            continue
    return ('other', None)


def update_parking_from_sync(sync) -> Optional[ParkingEvent]:
    """Hook called from _save_vehicle_sync after a new sync row is created.

    Decides whether to:
    - Open a new ParkingEvent (car arrived somewhere new)
    - Close the currently open event (car has moved)
    - Do nothing (car is moving / no GPS data / same spot)
    """
    if sync is None or sync.location_lat is None or sync.location_lon is None:
        return None

    lat, lon = float(sync.location_lat), float(sync.location_lon)
    open_evt = (ParkingEvent.query
                .filter(ParkingEvent.departed_at.is_(None))
                .order_by(ParkingEvent.arrived_at.desc())
                .first())

    if open_evt is None:
        # No open event → open one at the current location
        return _open_event(sync, lat, lon)

    distance = _haversine_m(open_evt.lat, open_evt.lon, lat, lon)

    if distance <= SAME_PLACE_M:
        # Car still at the same spot. Update arrival data with the latest
        # snapshot in case earlier values were missing, and bump last_seen_at
        # so the trip-duration math has a tighter lower bound.
        if open_evt.odometer_arrived is None and sync.odometer_km:
            open_evt.odometer_arrived = sync.odometer_km
        if open_evt.soc_arrived is None and sync.soc_percent:
            open_evt.soc_arrived = sync.soc_percent
        # Only advance last_seen_at forward, never backward (matters during backfill)
        if open_evt.last_seen_at is None or sync.timestamp > open_evt.last_seen_at:
            open_evt.last_seen_at = sync.timestamp
        db.session.commit()
        return open_evt

    if distance >= MOVE_THRESHOLD_M:
        # Car has moved away from the open spot → close it, open a new one.
        open_evt.departed_at = sync.timestamp
        if sync.odometer_km is not None:
            open_evt.odometer_departed = sync.odometer_km
        if sync.soc_percent is not None:
            open_evt.soc_departed = sync.soc_percent
        db.session.commit()
        return _open_event(sync, lat, lon)

    return open_evt


def _open_event(sync, lat: float, lon: float) -> ParkingEvent:
    label, fav_name = _classify_location(lat, lon)
    evt = ParkingEvent(
        arrived_at=sync.timestamp,
        last_seen_at=sync.timestamp,
        lat=lat,
        lon=lon,
        label=label,
        favorite_name=fav_name,
        odometer_arrived=sync.odometer_km,
        soc_arrived=sync.soc_percent,
    )
    db.session.add(evt)
    db.session.commit()
    return evt


def get_parking_events(limit: Optional[int] = None,
                       since: Optional[datetime] = None):
    q = ParkingEvent.query.order_by(ParkingEvent.arrived_at.desc())
    if since:
        q = q.filter(ParkingEvent.arrived_at >= since)
    if limit:
        q = q.limit(limit)
    return q.all()


def _load_regen_lookup():
    """Return a list of (timestamp, cumulative_kwh) for all vehicle syncs
    that have a cumulative regen value, sorted ascending. Used for O(log n)
    per-trip lookups via bisect."""
    from models.database import VehicleSync
    rows = (VehicleSync.query
            .filter(VehicleSync.regen_cumulative_kwh.isnot(None))
            .order_by(VehicleSync.timestamp.asc())
            .all())
    return [(r.timestamp, r.regen_cumulative_kwh) for r in rows]


def _cum_regen_at(lookup, ts):
    """Return cumulative regen at or before ts; None if no data before ts."""
    if not lookup or ts is None:
        return None
    keys = [r[0] for r in lookup]
    idx = bisect.bisect_right(keys, ts) - 1
    if idx < 0:
        return None
    return lookup[idx][1]


def get_trips(limit: Optional[int] = None,
              since: Optional[datetime] = None):
    """Derive trips from consecutive parking events.

    Trip = (departure event, arrival event) where the arrival event has a
    later arrived_at than the departure event's departed_at.
    """
    q = ParkingEvent.query.order_by(ParkingEvent.arrived_at.asc())
    if since:
        q = q.filter(ParkingEvent.arrived_at >= since)
    events = q.all()

    regen_lookup = _load_regen_lookup()

    trips = []
    for prev, curr in zip(events, events[1:]):
        if prev.departed_at is None:
            continue
        # Trip km = odometer at the next arrival minus odometer at the prev arrival.
        # We use arrival values because closing a parking event happens at the
        # moment the next arrival is noticed — the "departed_at" odometer would
        # already include the trip distance and yield 0.
        km = None
        if prev.odometer_arrived is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_arrived, 0)
        soc_used = None
        if prev.soc_arrived is not None and curr.soc_arrived is not None:
            soc_used = max(prev.soc_arrived - curr.soc_arrived, 0)
        # Per-trip recuperation from cumulative regen delta
        regen_kwh = None
        cum_dep = _cum_regen_at(regen_lookup, prev.departed_at)
        cum_arr = _cum_regen_at(regen_lookup, curr.arrived_at)
        if cum_dep is not None and cum_arr is not None:
            regen_kwh = round(max(cum_arr - cum_dep, 0), 2)
        trips.append({
            'from': {
                'lat': prev.lat, 'lon': prev.lon,
                'label': prev.label, 'name': prev.favorite_name,
                'address': prev.address,
                'departed_at': prev.departed_at.isoformat() if prev.departed_at else None,
            },
            'to': {
                'lat': curr.lat, 'lon': curr.lon,
                'label': curr.label, 'name': curr.favorite_name,
                'address': curr.address,
                'arrived_at': curr.arrived_at.isoformat() if curr.arrived_at else None,
            },
            'km': km,
            'soc_used': soc_used,
            'regen_kwh': regen_kwh,
        })

    trips.reverse()  # newest first
    if limit:
        trips = trips[:limit]
    return trips


def get_trip_summary(since: Optional[datetime] = None):
    """Aggregate trip statistics: total km, count, home<->work split, regen."""
    trips = get_trips(since=since)
    total_km = sum(t['km'] for t in trips if t['km'])
    home_work_km = sum(
        t['km'] for t in trips
        if t['km'] and {t['from']['label'], t['to']['label']} == {'home', 'work'}
    )
    total_regen = sum(t['regen_kwh'] for t in trips if t.get('regen_kwh'))
    regen_km = sum(t['km'] for t in trips if t.get('regen_kwh') and t.get('km'))
    return {
        'count': len(trips),
        'total_km': round(total_km, 1) if total_km else 0,
        'home_work_km': round(home_work_km, 1) if home_work_km else 0,
        'avg_km': round(total_km / len(trips), 1) if trips and total_km else 0,
        'total_regen_kwh': round(total_regen, 2) if total_regen else 0,
        'regen_per_km': round(total_regen / regen_km, 4) if regen_km else 0,
    }


def reclassify_all_events():
    """Re-run classification on every event (e.g. after the user changed
    home/work coordinates)."""
    locations = _load_locations()
    events = ParkingEvent.query.all()
    for evt in events:
        label, fav_name = _classify_location(evt.lat, evt.lon, locations)
        evt.label = label
        evt.favorite_name = fav_name
    db.session.commit()
    return len(events)


def backfill_parking_events(wipe_existing: bool = False) -> dict:
    """Replay every VehicleSync row chronologically through the parking hook.

    Used to retroactively build the driving log from a database that was
    populated before the parking hook existed (or after a long sync history
    where the hook only fired occasionally).

    Returns a summary dict ``{'syncs_processed': N, 'events_after': M}``.
    """
    from models.database import VehicleSync

    if wipe_existing:
        ParkingEvent.query.delete()
        db.session.commit()

    syncs = (VehicleSync.query
             .filter(VehicleSync.location_lat.isnot(None),
                     VehicleSync.location_lon.isnot(None))
             .order_by(VehicleSync.timestamp.asc())
             .all())
    for s in syncs:
        update_parking_from_sync(s)

    return {
        'syncs_processed': len(syncs),
        'events_after': ParkingEvent.query.count(),
    }


def geocode_missing_events(limit: int = 50) -> int:
    """Resolve addresses for parking events that don't yet have one.

    Called on /trips page load (background thread) and from /api/trips/geocode_missing.
    Hits Nominatim with the in-service 1.1s rate limiter + permanent DB cache,
    so repeat calls are cheap. Returns the number of events that got filled in.
    """
    from services.geocode_service import reverse
    from models.database import AppConfig as _AppConfig

    pending = (ParkingEvent.query
               .filter(ParkingEvent.address.is_(None))
               .order_by(ParkingEvent.arrived_at.desc())
               .limit(limit)
               .all())
    if not pending:
        return 0

    lang = _AppConfig.get('app_language', 'de')
    filled = 0
    for evt in pending:
        try:
            addr = reverse(evt.lat, evt.lon, language=lang)
            if addr:
                evt.address = addr
                filled += 1
        except Exception:
            continue
    if filled:
        db.session.commit()
    return filled


def is_brand_supports_location(brand: str) -> bool:
    """Cheap helper to ask the feature matrix without a circular import."""
    try:
        from services.vehicle.feature_matrix import get_features
        return get_features(brand).get('location') in ('yes', 'partial')
    except Exception:
        return False
