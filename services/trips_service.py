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

from models.database import db, ParkingEvent, AppConfig, VehicleTrip


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
        # Car still at the same spot. Top up arrival fields that were
        # missing on open, continuously track the latest at-spot state
        # in odometer_departed/soc_departed (so they reflect "last known
        # while still here" when the move is later detected — not the
        # first sync at the new location), and bump last_seen_at.
        if open_evt.odometer_arrived is None and sync.odometer_km:
            open_evt.odometer_arrived = sync.odometer_km
        if open_evt.soc_arrived is None and sync.soc_percent:
            open_evt.soc_arrived = sync.soc_percent
        if sync.odometer_km is not None:
            open_evt.odometer_departed = sync.odometer_km
        if sync.soc_percent is not None:
            open_evt.soc_departed = sync.soc_percent
        # Only advance last_seen_at forward, never backward (matters during backfill)
        if open_evt.last_seen_at is None or sync.timestamp > open_evt.last_seen_at:
            open_evt.last_seen_at = sync.timestamp
        db.session.commit()
        return open_evt

    if distance >= MOVE_THRESHOLD_M:
        # Car has moved away → close the event at the detection timestamp.
        # Leave odometer_departed / soc_departed alone — they already hold
        # the last at-spot values from same-place updates above, which is
        # the semantically correct "state when leaving here". Only fill
        # them from the new-location sync as a last-resort fallback when
        # no same-place sync ever topped them up.
        open_evt.departed_at = sync.timestamp
        if open_evt.odometer_departed is None and sync.odometer_km is not None:
            open_evt.odometer_departed = sync.odometer_km
        if open_evt.soc_departed is None and sync.soc_percent is not None:
            open_evt.soc_departed = sync.soc_percent
        db.session.commit()
        return _open_event(sync, lat, lon)

    return open_evt


def _open_event(sync, lat: float, lon: float) -> ParkingEvent:
    label, fav_name = _classify_location(lat, lon)
    # Initialize odometer_departed / soc_departed to the arrival values
    # so even a one-sync parking event has meaningful "when-leaving" data.
    # Same-place syncs will overwrite with newer at-spot values until the
    # car moves.
    evt = ParkingEvent(
        arrived_at=sync.timestamp,
        last_seen_at=sync.timestamp,
        lat=lat,
        lon=lon,
        label=label,
        favorite_name=fav_name,
        odometer_arrived=sync.odometer_km,
        odometer_departed=sync.odometer_km,
        soc_arrived=sync.soc_percent,
        soc_departed=sync.soc_percent,
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


def _cum_regen_at(lookup, ts, strict=False):
    """Return cumulative regen at (or strictly before, if strict=True) ts.
    None if no data before ts."""
    if not lookup or ts is None:
        return None
    keys = [r[0] for r in lookup]
    idx = (bisect.bisect_left(keys, ts) if strict else bisect.bisect_right(keys, ts)) - 1
    if idx < 0:
        return None
    return lookup[idx][1]


def _load_soc_lookup():
    """Return (ts, soc_percent) for every VehicleSync that carries SoC,
    sorted ascending. Used to find the last known SoC strictly before a
    trip's departure timestamp — which is the only way to recover the
    SoC *at origin* as the car leaves, because ParkingEvent.soc_departed
    is captured by the first sync at the *new* location (post-trip)."""
    from models.database import VehicleSync
    rows = (VehicleSync.query
            .filter(VehicleSync.soc_percent.isnot(None))
            .order_by(VehicleSync.timestamp.asc())
            .all())
    return [(r.timestamp, r.soc_percent) for r in rows]


def _soc_before(lookup, ts):
    """Most recent SoC strictly before ts. None if no earlier sync."""
    if not lookup or ts is None:
        return None
    keys = [r[0] for r in lookup]
    idx = bisect.bisect_left(keys, ts) - 1
    if idx < 0:
        return None
    return lookup[idx][1]


_SDK_STATS_MATCH_TOLERANCE_MIN = 60


def _find_sdk_stats(sdk_rows, pe_departed_at):
    """Find the SDK trip whose start_time is within 60 min of this
    PE-pair's departure. Used purely to attach drive-minute / speed
    stats to a polled trip. Returns the VehicleTrip row or None."""
    if not sdk_rows or pe_departed_at is None:
        return None
    tol = timedelta(minutes=_SDK_STATS_MATCH_TOLERANCE_MIN)
    best = None
    best_delta = tol + timedelta(seconds=1)
    for t in sdk_rows:
        delta = abs(t.start_time - pe_departed_at)
        if delta <= tol and delta < best_delta:
            best, best_delta = t, delta
    return best


def _unknown_endpoint_dict(include_departed: bool = False,
                           time_override: Optional[datetime] = None):
    """Stub for SDK-fallback trips on historical days where no polling
    data exists. label='unknown' lets the template render 'Ort unbekannt'."""
    return {
        'id': None, 'lat': None, 'lon': None,
        'label': 'unknown', 'name': None, 'address': None,
        'arrived_at': time_override.isoformat() if time_override else None,
        **({'departed_at': time_override.isoformat() if time_override else None}
           if include_departed else {}),
    }


def _event_to_dict(evt, include_departed: bool = False):
    """Shape a ParkingEvent into the dict the trips UI expects."""
    out = {
        'id': evt.id,
        'lat': evt.lat, 'lon': evt.lon,
        'label': evt.label, 'name': evt.favorite_name,
        'address': evt.address,
        'arrived_at': evt.arrived_at.isoformat() if evt.arrived_at else None,
    }
    if include_departed:
        out['departed_at'] = evt.departed_at.isoformat() if evt.departed_at else None
    return out


def get_trips(limit: Optional[int] = None,
              since: Optional[datetime] = None):
    """Unified trip feed.

    Source of truth: ParkingEvent pairs (same as pre-v2.24). SDK data
    only surfaces on days with zero PE coverage, as a historical
    backfill fallback. Where a PE pair's departure aligns with an SDK
    row, the SDK row's stats (drive/idle minutes, avg/max speed) ride
    along on the polled trip for extra detail.

    Trips are sorted newest-first.
    """
    ev_q = ParkingEvent.query.order_by(ParkingEvent.arrived_at.asc())
    if since:
        ev_q = ev_q.filter(ParkingEvent.arrived_at >= since)
    events = ev_q.all()

    sdk_q = VehicleTrip.query.order_by(VehicleTrip.start_time.asc())
    if since:
        sdk_q = sdk_q.filter(VehicleTrip.start_time >= since)
    sdk_rows = sdk_q.all()

    regen_lookup = _load_regen_lookup()
    soc_lookup = _load_soc_lookup()

    trips = []
    pe_covered_dates = set()

    # Primary: ParkingEvent pairs.
    for prev, curr in zip(events, events[1:]):
        if prev.departed_at is None:
            continue
        pe_covered_dates.add(prev.departed_at.date())

        km = None
        if prev.odometer_arrived is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_arrived, 0)

        # SoC "used". The two ParkingEvent columns (soc_arrived /
        # soc_departed) are both captured by the first sync at the
        # origin / at the *next* location respectively — neither
        # actually represents SoC at the moment the car drove off.
        # Recovering that properly requires querying vehicle_syncs for
        # the last known SoC strictly before prev.departed_at: in smart
        # mode that's ≤ 10 min pre-move, which is accurate enough. End
        # SoC is curr.soc_arrived (first sync at destination = true
        # post-trip state). Falls back to prev.soc_arrived only when no
        # earlier sync with SoC exists (initial-setup edge case).
        start_soc = _soc_before(soc_lookup, prev.departed_at)
        if start_soc is None:
            start_soc = prev.soc_arrived
        end_soc = curr.soc_arrived
        soc_used = None
        if start_soc is not None and end_soc is not None:
            soc_used = max(start_soc - end_soc, 0)

        regen_kwh = None
        dep_ts = prev.last_seen_at or prev.departed_at
        cum_dep = _cum_regen_at(regen_lookup, dep_ts, strict=(prev.last_seen_at is None))
        cum_arr = _cum_regen_at(regen_lookup, curr.arrived_at)
        if cum_dep is not None and cum_arr is not None:
            regen_kwh = round(max(cum_arr - cum_dep, 0), 2)

        trip = {
            'from': _event_to_dict(prev, include_departed=True),
            'to':   _event_to_dict(curr),
            'km': km,
            'soc_used': soc_used,
            'regen_kwh': regen_kwh,
            'source': 'polled',
        }

        # Best-effort SDK stats attach. Not required — a polled trip
        # stands on its own.
        sdk = _find_sdk_stats(sdk_rows, prev.departed_at)
        if sdk is not None:
            trip['drive_min'] = sdk.drive_minutes
            trip['idle_min'] = sdk.idle_minutes
            trip['avg_speed_kmh'] = sdk.avg_speed_kmh
            trip['max_speed_kmh'] = sdk.max_speed_kmh

        trips.append(trip)

    # Fallback: show SDK-only trips on days where polling produced no
    # pairs at all (historical backfill). Locations are 'unknown' —
    # we honestly have no GPS data for those dates.
    for row in sdk_rows:
        if row.trip_date in pe_covered_dates:
            continue
        start = row.start_time
        total_min = (row.drive_minutes or 0) + (row.idle_minutes or 0)
        end = start + timedelta(minutes=total_min) if total_min > 0 else start
        trips.append({
            'from': _unknown_endpoint_dict(include_departed=True, time_override=start),
            'to':   _unknown_endpoint_dict(include_departed=False,
                                            time_override=end if total_min > 0 else None),
            'km': round(row.distance_km, 1) if row.distance_km is not None else None,
            'soc_used': None,
            'regen_kwh': None,
            'source': 'sdk',
            'drive_min': row.drive_minutes,
            'idle_min': row.idle_minutes,
            'avg_speed_kmh': row.avg_speed_kmh,
            'max_speed_kmh': row.max_speed_kmh,
        })

    def _sort_key(t):
        return t['from'].get('departed_at') or t['to'].get('arrived_at') or ''
    trips.sort(key=_sort_key, reverse=True)

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
