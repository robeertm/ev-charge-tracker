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

# Maximum age (minutes) of the ECU-side GPS timestamp before we treat a
# sync as "no reliable location". Hyundai/Bluelink cached responses often
# echo the last-known GPS for hours after the car went to sleep — without
# this filter the parking-event state machine reacts to those echoes as
# if they were live reports. 30 min is a generous upper bound: smart-mode
# polling runs every 10 min, so legitimate fresh data is typically 0–20
# min old. Anything older has a very high prior of being a cache echo.
STALE_GPS_MAX_MIN = 30.0

# How long an origin PE may sit without a fresh-GPS confirmation before its
# trip-render origin label degrades to ``'unknown'``. Hyundai Bluelink
# returns cache-echoed GPS with a stale ``gps_ts`` for hours while the car
# is parked, so the PE's ``last_seen_at`` is only advanced on truly fresh
# syncs. When the last fresh-GPS confirmation for the origin PE is more
# than this many minutes before ``departed_at``, we don't actually know
# whether the car was still at the labelled spot at drive start — the
# odometer being unchanged is strong circumstantial evidence, but the
# user's rule is "honest over clever": without a fresh GPS lock, render
# the origin as Unknown. Kia/UVO bumps ``last_seen_at`` on every cached
# read (its GPS is always fresh), so this never fires for Kia.
ORIGIN_SILENCE_MAX_MIN = 60.0


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
    if sync is None:
        return None

    open_evt = (ParkingEvent.query
                .filter(ParkingEvent.departed_at.is_(None))
                .order_by(ParkingEvent.arrived_at.desc())
                .first())

    brand = (AppConfig.get('vehicle_api_brand', '') or '').lower()

    # Fresh-GPS shortcut (used for upgrade and odo-advance branches).
    # Strict: requires ``location_last_updated_at`` to be present AND
    # within the staleness threshold. A missing gps_ts is treated as
    # not-fresh — observed on Hyundai where secondary cache-echo syncs
    # (the same cached coord re-served a minute after a stale-gps-ts
    # primary) come back with no ts at all. Accepting them as fresh
    # would cause phantom upgrades of Unknown PEs to echo coords.
    def _is_fresh_gps() -> bool:
        if sync.location_lat is None or sync.location_lon is None:
            return False
        if sync.location_last_updated_at is None:
            return False
        age = (sync.timestamp - sync.location_last_updated_at).total_seconds() / 60.0
        return age <= STALE_GPS_MAX_MIN

    # Odometer-advance is ground truth: as soon as the odo reading shows
    # the car drove somewhere, close the open PE — regardless of whether
    # the reported GPS coordinate can be trusted. Runs BEFORE the stale-GPS
    # gate so Hyundai-style "odo jumped, GPS still cached at origin" syncs
    # don't get silently dropped. Polling cadence is ≤ 10 min in smart mode,
    # so sync.timestamp is within ≤ 10 min of the car's true arrival — a
    # good-enough arrived_at anchor until SDK reconcile snaps it to the
    # real trip-end moment. Hyundai-only: Kia pushes fresh GPS with every
    # update, so it never hits the "odo advanced while GPS still stale"
    # data shape that this path exists to rescue.
    if (brand == 'hyundai'
            and open_evt is not None
            and sync.odometer_km is not None):
        last_odo = open_evt.odometer_departed or open_evt.odometer_arrived
        if last_odo is not None and sync.odometer_km - last_odo >= 1:
            open_evt.departed_at = open_evt.last_seen_at or open_evt.arrived_at
            # Realign arrival/departure SoC with the trip-display derivation
            # (min-in-first-30min for arrival; last sync before departure for
            # departure) now that this PE is closed. The running same-place
            # updates miss the Kia/Hyundai "first post-drive sync carries
            # pre-drive SoC" echo; trip row uses the corrected values via
            # VehicleSync scan, so stored fields have to match.
            recompute_pe_soc(open_evt)
            db.session.commit()
            try:
                from services.vehicle.sync_service import (
                    request_force_refresh, request_post_move_reconcile,
                )
                request_force_refresh(reason='odo_advance')
                request_post_move_reconcile()
            except Exception:
                pass
            # Hyundai at odo-advance: the fresh-GPS sync either carries
            # (a) a CACHE ECHO of the origin coord (cloud hasn't caught
            # up with the move yet) or (b) the TRUE destination coord
            # (cloud just delivered the new fix). Before v2.28.52 we
            # assumed (a) universally and stamped the closing PE —
            # which was correct for the Home-echo morning commute case
            # but silently shifted labels by one whenever Hyundai
            # actually returned (b). The 23.04 ev-dirk chain had a
            # short Home → Micktner hop followed by Micktner → Ponytruppe
            # where the fresh-GPS at every odo-advance was the TRUE
            # destination; the closing-side stamp wrote each PE with
            # the NEXT PE's coord, shifting Micktner/Ponytruppe/Dohnaer
            # one step down the chain.
            #
            # New rule: stamp the NEW (just-opened) PE with the fresh
            # coord whenever it doesn't look like an echo of the spot
            # we just left. "Echo" means the fresh coord matches the
            # closing PE's own coord (if that PE is labelled) or,
            # when the closing PE is Unknown, its most recent labelled
            # predecessor. On match → open Unknown; the destination
            # will be stamped later, when Hyundai catches up, via the
            # upgrade path. No match → open a labelled PE directly.
            if _is_fresh_gps():
                new_lat = float(sync.location_lat)
                new_lon = float(sync.location_lon)
                ref_pe = open_evt if (open_evt.label and open_evt.label != 'unknown') \
                         else _previous_labelled_pe(open_evt)
                is_echo = False
                if ref_pe is not None:
                    d = _haversine_m(ref_pe.lat, ref_pe.lon, new_lat, new_lon)
                    is_echo = d <= SAME_PLACE_M
                if not is_echo:
                    return _open_event(sync, new_lat, new_lon)
            return _open_unknown(sync)

    # Upgrade path: if the currently open PE is an Unknown placeholder
    # (opened from an odo-advance with stale-or-missing GPS) and this
    # sync finally carries a fresh GPS fix, stamp the real location onto
    # the existing PE instead of creating a new one. The arrived_at
    # timestamp stays at the odo-advance moment (our best estimate of
    # when the car actually arrived); SDK reconcile can still snap it.
    #
    # Two echo guards ensure we don't upgrade to a Hyundai cache echo:
    #   (a) Teleport guard — fresh-GPS coord matches the most recent
    #       labelled PE (where we just came from) AND the odometer
    #       advanced between them. The car cannot have returned to a
    #       spot it already left; the coord is a stale echo.
    #   (b) Same-odo flip guard — an earlier fresh-GPS sync within
    #       this Unknown PE's own lifetime reported a DIFFERENT coord
    #       at the same odometer reading. Two disagreeing fresh-GPS
    #       fixes without any movement: at least one is an echo;
    #       refuse to pick either over the other.
    # User principle: "wenn die logik sich unsicher ist, das auto
    # kann sich ja nicht wegbeamen" — prefer Unknown over a guess.
    if (open_evt is not None
            and open_evt.label == 'unknown'
            and _is_fresh_gps()):
        new_lat = float(sync.location_lat)
        new_lon = float(sync.location_lon)
        prev_labelled = _previous_labelled_pe(open_evt)
        teleport = False
        if prev_labelled is not None:
            d = _haversine_m(prev_labelled.lat, prev_labelled.lon,
                             new_lat, new_lon)
            if d <= SAME_PLACE_M:
                this_odo = open_evt.odometer_arrived or sync.odometer_km
                prev_odo = prev_labelled.odometer_departed or prev_labelled.odometer_arrived
                if (this_odo is not None and prev_odo is not None
                        and this_odo - prev_odo >= 1):
                    teleport = True
        flip = False
        if not teleport:
            from models.database import VehicleSync
            earlier = VehicleSync.query.filter(
                VehicleSync.timestamp >= open_evt.arrived_at,
                VehicleSync.timestamp < sync.timestamp,
                VehicleSync.location_lat.isnot(None),
                VehicleSync.location_last_updated_at.isnot(None),
            ).all()
            for es in earlier:
                age = (es.timestamp - es.location_last_updated_at).total_seconds() / 60.0
                if age > STALE_GPS_MAX_MIN:
                    continue
                d = _haversine_m(float(es.location_lat), float(es.location_lon),
                                 new_lat, new_lon)
                if d > SAME_PLACE_M:
                    flip = True
                    break
        if teleport or flip:
            return open_evt
        _upgrade_unknown(open_evt, sync)
        return open_evt

    # Unknown PE still unknown: we can't upgrade without fresh GPS, but
    # we can still extend last_seen_at (and soc_departed) on any sync
    # whose odometer matches — the car is demonstrably still parked at
    # the unknown spot. Lets the next drive's departed_at reflect the
    # actual leave time rather than the PE-open moment.
    if (open_evt is not None
            and open_evt.label == 'unknown'
            and sync.odometer_km is not None):
        last_odo = open_evt.odometer_departed or open_evt.odometer_arrived
        if last_odo is not None and sync.odometer_km == last_odo:
            if open_evt.last_seen_at is None or sync.timestamp > open_evt.last_seen_at:
                open_evt.last_seen_at = sync.timestamp
            if sync.soc_percent is not None:
                open_evt.soc_departed = sync.soc_percent
            db.session.commit()
            return open_evt

    # Arrival-echo correction: pull ``soc_arrived`` down to the min
    # seen within the first 30 min of the PE's lifetime, regardless of
    # this sync's GPS freshness. Kia/Hyundai's first post-drive sync
    # often carries the PRE-drive SoC as a cloud cache echo, followed
    # by a partial-fresh sync (gps_ts = None) 10-20 s later with the
    # real post-drive SoC — that partial sync is rejected by the
    # freshness gate for GPS/location purposes (correct) but its SoC
    # reading is trustworthy when its odometer matches the PE's own
    # odometer (car is provably at the destination, not mid-drive).
    # Without this patch the stored ``soc_arrived`` keeps the echo and
    # the Fahrtenbuch edit modal disagrees with the trip row's delta
    # (which already uses ``_soc_min_in`` to bypass the echo).
    if (open_evt is not None
            and open_evt.arrived_at is not None
            and open_evt.odometer_arrived is not None
            and sync.odometer_km == open_evt.odometer_arrived
            and sync.soc_percent is not None
            and open_evt.soc_arrived is not None
            and sync.soc_percent < open_evt.soc_arrived
            and (sync.timestamp - open_evt.arrived_at).total_seconds() / 60.0
                <= _ARRIVAL_SOC_WINDOW_MIN):
        open_evt.soc_arrived = sync.soc_percent
        db.session.commit()

    if sync.location_lat is None or sync.location_lon is None:
        return None

    # Staleness gate (v2.28.11). When the sync's own GPS timestamp is
    # significantly older than the sync request, the lat/lon is almost
    # certainly a cloud-cache echo of the last known position — not a
    # fresh fix. Treat it exactly like Kia's "None" GPS behaviour: ignore
    # entirely, so PE doesn't build phantom transitions out of echoes.
    if sync.location_last_updated_at is not None:
        age_min = (sync.timestamp - sync.location_last_updated_at).total_seconds() / 60.0
        if age_min > STALE_GPS_MAX_MIN:
            return None

    lat, lon = float(sync.location_lat), float(sync.location_lon)

    if open_evt is None:
        # No open event → open one at the current location
        return _open_event(sync, lat, lon)

    distance = _haversine_m(open_evt.lat, open_evt.lon, lat, lon)

    if distance <= SAME_PLACE_M:
        # Trust gate for same-place updates: only advance PE state from
        # syncs whose GPS fix is KNOWN-FRESH (gps_ts present AND within
        # the staleness threshold). A missing gps_ts is the Kia/Hyundai
        # cache-echo fingerprint: the cloud returns the origin's GPS
        # (matching this PE's coords by chance) while the car is already
        # driving, with partially-fresh telemetry (e.g. post-drive SoC)
        # riding along. Advancing soc_departed from such a sync captured
        # mid-drive SoC drops as if they happened at the origin —
        # e.g. a 50 % → 45 % Home arrival that was actually the SoC
        # drop across the commute itself. Without a fresh gps_ts we
        # cannot distinguish the two.
        gps_fresh = (
            sync.location_last_updated_at is not None
            and (sync.timestamp - sync.location_last_updated_at).total_seconds() / 60.0
                <= STALE_GPS_MAX_MIN
        )
        if not gps_fresh:
            return open_evt

        # Odometer-jump split: if a fresh-GPS sync lands on the same
        # spot but the odometer has advanced ≥ 1 km since the last
        # at-spot reading, the car took an invisible round trip (left
        # and returned before any move was observed). Close the current
        # PE at its last confirmed at-spot timestamp and open a fresh
        # one at the same coord. The in-between window becomes a
        # clearly-marked PE gap that SDK trip-reconcile can bind a
        # matching trip to.
        last_odo = open_evt.odometer_departed or open_evt.odometer_arrived
        if (last_odo is not None
                and sync.odometer_km is not None
                and sync.odometer_km - last_odo >= 1):
            open_evt.departed_at = open_evt.last_seen_at or open_evt.arrived_at
            recompute_pe_soc(open_evt)
            db.session.commit()
            try:
                from services.vehicle.sync_service import (
                    request_force_refresh, request_post_move_reconcile,
                )
                request_force_refresh(reason='odo_jump_split')
                request_post_move_reconcile()
            except Exception:
                pass
            return _open_event(sync, lat, lon)

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
        # Odometer guard: if the car's odometer hasn't advanced since the
        # last at-spot sync, the GPS "move" is a cache echo, not a real
        # relocation. Seen on Hyundai Bluelink: cloud occasionally serves
        # a stale GPS fix with a deceptively fresh timestamp (e.g. 4 min
        # old, below the 30-min staleness gate) pointing at an earlier
        # location (morning's Work coord). The odometer is the ground
        # truth — if it hasn't moved, the car hasn't moved. Ignoring the
        # sync here keeps the current PE intact and prevents a phantom
        # PE-pair from appearing in Fahrtenbuch.
        last_odo = open_evt.odometer_departed or open_evt.odometer_arrived
        if (last_odo is not None
                and sync.odometer_km is not None
                and abs(sync.odometer_km - last_odo) < 1):
            return open_evt
        # Close the PE at this sync's timestamp — the first sync at the
        # NEW location. That over-estimates drive-time (real departure
        # was earlier) but is never poisoned by a cache-echo that
        # advanced ``last_seen_at`` past the true leave moment. For
        # Kia/Hyundai the reconciler snaps ``departed_at`` to the SDK
        # trip start shortly afterwards anyway; for brands without
        # tripinfo this is the safest single-sync-sourced anchor.
        open_evt.departed_at = sync.timestamp
        if open_evt.odometer_departed is None and sync.odometer_km is not None:
            open_evt.odometer_departed = sync.odometer_km
        if open_evt.soc_departed is None and sync.soc_percent is not None:
            open_evt.soc_departed = sync.soc_percent
        recompute_pe_soc(open_evt)
        db.session.commit()
        # Ask the sync service to schedule an immediate force-refresh on
        # the next tick so curr.arrived_at / soc_arrived / odometer_arrived
        # at the new location reflect a fresh reading, not whatever stale
        # cached state the periodic sync happened to pull.
        try:
            from services.vehicle.sync_service import (
                request_force_refresh, request_post_move_reconcile,
            )
            request_force_refresh(reason='motion_detected')
            # Kick off a one-shot SDK backfill + reconcile so the
            # finished trip's departed_at/arrived_at snap to SDK times
            # immediately instead of waiting until the 03:00 nightly
            # task. Regen/SoC lookups then see the correct timestamps.
            request_post_move_reconcile()
        except Exception:
            pass
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


def _open_unknown(sync) -> ParkingEvent:
    """Open a placeholder PE when an odo-advance closed the previous one
    but no fresh GPS is available to anchor the new location. Coords are
    sentinel (0,0) and label is 'unknown'. A later fresh-GPS sync
    upgrades it via ``_upgrade_unknown``."""
    evt = ParkingEvent(
        arrived_at=sync.timestamp,
        last_seen_at=sync.timestamp,
        lat=0.0,
        lon=0.0,
        label='unknown',
        favorite_name=None,
        odometer_arrived=sync.odometer_km,
        odometer_departed=sync.odometer_km,
        soc_arrived=sync.soc_percent,
        soc_departed=sync.soc_percent,
    )
    db.session.add(evt)
    db.session.commit()
    return evt


def _upgrade_unknown(evt: ParkingEvent, sync) -> None:
    """Fill in real coords and label on an Unknown PE from a fresh-GPS
    sync. Keeps ``arrived_at`` (the odo-advance anchor) — the fresh-GPS
    sync may be many minutes after actual arrival. SDK reconcile can
    still snap arrived_at to the true trip-end moment."""
    lat, lon = float(sync.location_lat), float(sync.location_lon)
    label, fav_name = _classify_location(lat, lon)
    evt.lat = lat
    evt.lon = lon
    evt.label = label
    evt.favorite_name = fav_name
    evt.address = None  # will be populated by geocode loop
    if evt.last_seen_at is None or sync.timestamp > evt.last_seen_at:
        evt.last_seen_at = sync.timestamp
    if sync.soc_percent is not None:
        evt.soc_departed = sync.soc_percent
    db.session.commit()


def _stamp_closed_pe(evt: ParkingEvent, sync) -> None:
    """Retroactively stamp the fresh-GPS coord + classified label onto
    a just-closed Unknown PE. Used by the Hyundai 1-step-behind rule
    at odo-advance: Hyundai Bluelink's fresh-GPS at the moment of a
    detected drive represents the car's LAST known location (where it
    was DURING the closing PE's lifetime), not the drive's destination.
    So when the open PE was Unknown — opened because its own arrival
    sync had no fresh GPS — this later odo-advance is our chance to
    reveal what spot it actually was. Only timestamps stay untouched;
    the whole identity (coord/label/name) gets written in place."""
    lat, lon = float(sync.location_lat), float(sync.location_lon)
    label, fav_name = _classify_location(lat, lon)
    evt.lat = lat
    evt.lon = lon
    evt.label = label
    evt.favorite_name = fav_name
    evt.address = None


def recompute_pe_soc(evt: ParkingEvent, soc_lookup=None) -> bool:
    """Align a PE's ``soc_arrived`` / ``soc_departed`` with the values
    that the trip display would derive from ``VehicleSync``.

    Two data-quality problems the raw state-machine capture leaves behind:

    1. **Arrival echo.** Kia/Hyundai clouds often return the pre-drive SoC
       on the very first sync at the new odometer — fresh GPS + new
       odometer + stale SoC. Seconds later the real post-drive SoC lands.
       The PE is opened from that first sync and sticks with the echo
       unless a same-place update beats the trust gate. ``_soc_min_in``
       over the first 30 min of arrival window picks the lowest reading
       — i.e. the real post-drive SoC — because any in-window charging
       pulse would only drag it further below and comes back above later.

    2. **Departure gating.** The same trust gate blocks SoC updates on
       any same-place sync whose ``gps_ts`` is missing (the partial-echo
       fingerprint). That's correct for coord updates but under-reports
       ``soc_departed`` at drives where only the partial syncs carried
       real post-parked SoC. ``_soc_before(departed_at)`` scans raw
       VehicleSync regardless of gps_ts, which is what the trip
       ``start_soc`` uses — so aligning ``soc_departed`` to it matches
       what the user sees in the Fahrtenbuch row.

    Returns True if either field changed.
    """
    if evt is None or evt.arrived_at is None:
        return False
    if soc_lookup is None:
        soc_lookup = _load_soc_lookup()

    changed = False
    window_end = evt.arrived_at + timedelta(minutes=_ARRIVAL_SOC_WINDOW_MIN)
    if evt.departed_at is not None and evt.departed_at < window_end:
        window_end = evt.departed_at
    new_arr = _soc_min_in(soc_lookup, evt.arrived_at, window_end)
    if new_arr is not None and new_arr != evt.soc_arrived:
        evt.soc_arrived = new_arr
        changed = True

    if evt.departed_at is not None:
        new_dep = _soc_before(soc_lookup, evt.departed_at)
        # Fallback: when no strictly-before sync exists (e.g. the PE
        # never saw a fresh-gps-ts reading so ``_soc_before`` skips its
        # own in-lifetime syncs too), keep the running soc_departed the
        # state machine collected — better than blanking it out.
        if new_dep is not None and new_dep != evt.soc_departed:
            evt.soc_departed = new_dep
            changed = True

    return changed


def repair_all_pe_soc() -> dict:
    """One-shot maintenance: walk every PE and run ``recompute_pe_soc``.

    Returns ``{'checked': N, 'fixed': M}``.
    """
    soc_lookup = _load_soc_lookup()
    events = ParkingEvent.query.order_by(ParkingEvent.arrived_at.asc()).all()
    fixed = 0
    for evt in events:
        if recompute_pe_soc(evt, soc_lookup):
            fixed += 1
    if fixed:
        db.session.commit()
    return {'checked': len(events), 'fixed': fixed}


def _previous_labelled_pe(evt: ParkingEvent) -> Optional[ParkingEvent]:
    """Most recent ParkingEvent ending before ``evt`` that has a real
    label (not ``'unknown'``, not ``None``) and real coords. Used by
    the 1-step-behind repeat-echo guard to compare a candidate stamp
    coord against the last confirmed location."""
    return (ParkingEvent.query
            .filter(ParkingEvent.id != evt.id)
            .filter(ParkingEvent.departed_at.isnot(None))
            .filter(ParkingEvent.label.isnot(None))
            .filter(ParkingEvent.label != 'unknown')
            .filter(ParkingEvent.arrived_at < evt.arrived_at)
            .order_by(ParkingEvent.arrived_at.desc())
            .first())




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


def _cum_regen_at_or_after(lookup, ts):
    """Return cumulative regen at the first sync with ts >= ``ts``.

    Used for trip-end regen after v2.28.20 started snapping
    ``curr.arrived_at`` to the SDK-derived physical arrival moment
    (which is typically EARLIER than the first at-destination sync).
    ``_cum_regen_at`` would then return the last pre-drive sync's
    cumulative value — making regen look like zero. We want the first
    post-arrival regen reading instead."""
    if not lookup or ts is None:
        return None
    keys = [r[0] for r in lookup]
    idx = bisect.bisect_left(keys, ts)
    if idx >= len(lookup):
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


def _soc_min_in(lookup, start_ts, end_ts):
    """Lowest SoC seen in the half-open window [start_ts, end_ts]. None
    if no syncs in that window.

    Used to find the real trip-end SoC when the first at-destination
    sync is a stale cache echo carrying the pre-drive SoC. Kia e-GMP
    and Hyundai Bluelink both exhibit this: after a drive ends, the
    server's first response to our poll sometimes still reports the
    SoC value it held before the drive (the ECU has uploaded fresh
    GPS / odometer but not yet the fresh SoC). A minute or two later
    the true post-drive SoC lands. Taking the minimum over a short
    window after arrival catches the real value and ignores the echo.
    Charging at the destination only pulls the minimum *lower* than
    soc_arrived briefly — by the time SoC recovers above soc_arrived
    the window is usually closed — so this stays safe there too.
    """
    if not lookup or start_ts is None or end_ts is None:
        return None
    keys = [r[0] for r in lookup]
    lo = bisect.bisect_left(keys, start_ts)
    hi = bisect.bisect_right(keys, end_ts)
    if lo >= hi:
        return None
    return min(lookup[i][1] for i in range(lo, hi))


_SDK_STATS_MATCH_TOLERANCE_MIN = 20
_ARRIVAL_SOC_WINDOW_MIN = 30


def _find_sdk_stats(sdk_rows, pe_departed_at, exclude_ids=None):
    """Find the SDK trip whose start_time is closest to this PE-pair's
    departure, within ±20 min, skipping any IDs in ``exclude_ids``.

    Returns the matching VehicleTrip row or None.

    The exclude_ids dedup prevents one SDK trip from attaching to two
    PE pairs when a phantom PE (e.g. GPS jitter creating a spurious
    home→elsewhere→home split) sits near the real trip. After v2.28.12
    reconcile, real PE pairs have ``prev.departed_at`` == ``sdk.start_time``
    exactly, so the phantom has no chance of outscoring the real pair
    — it only wins when it's the only candidate. Tightened from the
    pre-v2.28.15 60-min tolerance, which was routinely catching the
    wrong SDK trip when drives were closely spaced.
    """
    if not sdk_rows or pe_departed_at is None:
        return None
    excluded = exclude_ids or set()
    tol = timedelta(minutes=_SDK_STATS_MATCH_TOLERANCE_MIN)
    best = None
    best_delta = tol + timedelta(seconds=1)
    for t in sdk_rows:
        if t.id in excluded:
            continue
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


def _event_to_dict(evt, include_departed: bool = False,
                   time_override: Optional[datetime] = None):
    """Shape a ParkingEvent into the dict the trips UI expects.

    ``time_override`` is used when an SDK-only trip borrows this PE's
    location label/address but the actual departure/arrival time comes
    from the SDK trip, not the PE (e.g. a round trip within one Home
    PE where the car left and returned without generating a new PE).
    """
    out = {
        'id': evt.id,
        'lat': evt.lat, 'lon': evt.lon,
        'label': evt.label, 'name': evt.favorite_name,
        'address': evt.address,
        'arrived_at': (time_override.isoformat() if time_override is not None
                       else (evt.arrived_at.isoformat() if evt.arrived_at else None)),
    }
    if include_departed:
        out['departed_at'] = (time_override.isoformat() if time_override is not None
                              else (evt.departed_at.isoformat() if evt.departed_at else None))
    return out


def _find_pe_containing(events, ts):
    """First PE whose [arrived_at, departed_at or +infinity] contains
    ``ts`` (inclusive on both ends). Returns None when no PE covers ts."""
    for pe in events:
        if pe.arrived_at is None:
            continue
        if pe.arrived_at <= ts and (pe.departed_at is None or ts <= pe.departed_at):
            return pe
    return None


def _find_pe_after(events, ts):
    """First PE whose ``arrived_at`` is at or after ``ts``. Returns None
    when none exists (i.e. no later confirmed location)."""
    for pe in events:
        if pe.arrived_at is not None and pe.arrived_at >= ts:
            return pe
    return None


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

    brand = (AppConfig.get('vehicle_api_brand', '') or '').lower()

    trips = []
    pe_covered_dates = set()
    used_sdk_ids: set = set()

    # Primary: ParkingEvent pairs.
    for prev, curr in zip(events, events[1:]):
        if prev.departed_at is None:
            continue

        # Trip km: prefer odometer_departed (last at-spot reading) as the
        # origin km — it's the reading we're most confident reflects the
        # actual pre-drive odometer. Fall back to odometer_arrived for
        # events written before v2.28.3, which may still have stale
        # *_departed columns from the v2.24–v2.28.2 capture bug.
        km = None
        start_km = prev.odometer_departed if prev.odometer_departed is not None else prev.odometer_arrived
        if start_km is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - start_km, 0)

        # Trip SoC consumption: _soc_before(departed_at) is primary.
        # Reads from VehicleSync directly — catches SoC updates that the
        # v2.28.11 staleness filter blocked from advancing PE.soc_departed
        # (e.g. PV-charging the car while it sleeps at home: the SoC on
        # every cached-echo sync is fresh even though the GPS is stale,
        # so it's in VehicleSync but never made it into PE). Without this,
        # start_soc would be stuck on the arrival SoC and a 100→85 %
        # drive after overnight charge would render as 0 % consumption.
        # Fallbacks kick in only when no earlier sync row exists at all.
        start_soc = _soc_before(soc_lookup, prev.departed_at)
        if start_soc is None:
            start_soc = prev.soc_departed
        if start_soc is None:
            start_soc = prev.soc_arrived
        # Trip-end SoC: MIN over the first 30 min at destination, not
        # just the arrival sync. First arrival sync often carries a
        # stale cache-echo SoC (same value the car had before the drive)
        # while the true post-drive SoC lands 1–10 min later. See
        # _soc_min_in docstring. Fallback to curr.soc_arrived when the
        # destination has no sync rows in the window (shouldn't happen
        # in practice, but keeps the read resilient).
        end_soc = _soc_min_in(
            soc_lookup,
            curr.arrived_at,
            curr.arrived_at + timedelta(minutes=_ARRIVAL_SOC_WINDOW_MIN),
        )
        if end_soc is None:
            end_soc = curr.soc_arrived
        soc_used = None
        if start_soc is not None and end_soc is not None:
            soc_used = max(start_soc - end_soc, 0)

        regen_kwh = None
        # Anchor regen lookup on ``prev.departed_at`` only — never
        # ``last_seen_at``. The pair-iteration loop already skips
        # pairs where ``prev.departed_at is None`` (see earlier
        # ``if prev.departed_at is None: continue``), so the old
        # ``or prev.last_seen_at`` fallback was never reached in
        # practice. Explicit now that this lookup path doesn't touch
        # last_seen_at — the field is allowed to drift without
        # affecting Fahrtenbuch arithmetic. ``strict=True`` picks the
        # last sync STRICTLY BEFORE the drive began.
        dep_ts = prev.departed_at
        cum_dep = _cum_regen_at(regen_lookup, dep_ts, strict=True)
        cum_arr = _cum_regen_at_or_after(regen_lookup, curr.arrived_at)
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

        # Hyundai silence degradation: if the origin PE's last fresh-GPS
        # confirmation is more than ORIGIN_SILENCE_MAX_MIN before the trip's
        # departure, the stored label ("Ponytruppe" / "Home" / …) reflects
        # where the car was at its *last confirmed* GPS fix — not
        # necessarily where it is now. Hyundai Bluelink routinely echoes
        # the last GPS for 8+ hours overnight with a stale ``gps_ts`` that
        # our state machine correctly refuses to advance ``last_seen_at``
        # from. At render time, honestly surface that ambiguity as Unknown
        # instead of carrying the label forward. Only applies to Hyundai;
        # Kia/UVO's GPS is always fresh.
        #
        # Exception: if the odometer is stable (odometer_arrived ==
        # odometer_departed) across the full PE lifetime, the car
        # demonstrably didn't move even though we had no fresh-GPS
        # contact. Odo is monotonic and accurate once reported — a
        # stable reading over hours is strong evidence the car stayed
        # put, so keep the original label instead of pretending we
        # don't know where the car was.
        if brand == 'hyundai' and prev.departed_at:
            last_ok = prev.last_seen_at or prev.arrived_at
            if last_ok:
                silence_min = (prev.departed_at - last_ok).total_seconds() / 60.0
                odo_stable = (prev.odometer_arrived is not None
                              and prev.odometer_departed is not None
                              and prev.odometer_arrived == prev.odometer_departed)
                if silence_min > ORIGIN_SILENCE_MAX_MIN and not odo_stable:
                    # Preserve timestamps/id; mask label + coords + name.
                    trip['from'] = {
                        **trip['from'],
                        'label': 'unknown',
                        'name': None,
                        'address': None,
                        'lat': None,
                        'lon': None,
                    }

        # Best-effort SDK stats attach. Not required — a polled trip
        # stands on its own. One SDK trip binds to at most one PE pair
        # (see used_sdk_ids), so phantom PE pairs — e.g. GPS-jitter
        # "trips" of 0 km on top of a real drive — no longer steal
        # the real drive's stats.
        sdk = _find_sdk_stats(sdk_rows, prev.departed_at, exclude_ids=used_sdk_ids)
        if sdk is not None:
            trip['drive_min'] = sdk.drive_minutes
            trip['idle_min'] = sdk.idle_minutes
            trip['avg_speed_kmh'] = sdk.avg_speed_kmh
            trip['max_speed_kmh'] = sdk.max_speed_kmh
            used_sdk_ids.add(sdk.id)

        # Phantom filter: drop 0-km "trips" that no SDK trip confirms.
        # These are GPS-jitter artefacts (the car briefly appearing at a
        # distant spot for a handful of seconds, then back where it
        # actually was) that leave behind a spurious PE pair. Underlying
        # PE rows stay in the database — only the driving-log rendering
        # hides them. A real drive would either move the odometer OR get
        # a matching SDK trip-info record, usually both.
        if (km in (0, None)) and trip.get('drive_min') is None:
            continue

        pe_covered_dates.add(prev.departed_at.date())
        trips.append(trip)

    # Fallback: show SDK-only trips on days where polling produced no
    # pairs at all (historical backfill or Hyundai's sparse-GPS
    # mornings). Endpoints are inferred from the surrounding PE
    # context when possible:
    #
    #   - The PE containing ``sdk.start_time`` supplies the origin
    #     (car was confirmed parked there when the drive began).
    #   - The PE containing ``sdk.end_time``, or the first PE opened
    #     after it, supplies the destination.
    #
    # If both anchors resolve to the SAME PE (e.g. a round trip that
    # left and returned to Home before any new PE was opened), we
    # render origin = destination = that PE's location — the trip is
    # visually a "Home → Home" loop, which is more informative than
    # "unknown → unknown". Falls back to unknown when no PE covers the
    # time window at all.
    for row in sdk_rows:
        if row.trip_date in pe_covered_dates:
            continue
        start = row.start_time
        total_min = (row.drive_minutes or 0) + (row.idle_minutes or 0)
        end = start + timedelta(minutes=total_min) if total_min > 0 else start

        # Inference walks the FULL PE timeline (not the since-filtered
        # ``events`` list), otherwise a PE that opened before ``since``
        # but still covers the SDK trip's timestamp would be invisible
        # to the inference and the trip would degrade to
        # "unknown → other" even though its origin PE exists.
        all_events = (ParkingEvent.query
                      .order_by(ParkingEvent.arrived_at.asc())
                      .all()) if since else events
        origin_pe = _find_pe_containing(all_events, start)
        dest_pe = _find_pe_containing(all_events, end) or _find_pe_after(all_events, end)

        from_dict = (_event_to_dict(origin_pe, include_departed=True, time_override=start)
                     if origin_pe is not None
                     else _unknown_endpoint_dict(include_departed=True, time_override=start))
        to_time = end if total_min > 0 else None
        to_dict = (_event_to_dict(dest_pe, time_override=to_time)
                   if dest_pe is not None
                   else _unknown_endpoint_dict(include_departed=False, time_override=to_time))

        trips.append({
            'from': from_dict,
            'to':   to_dict,
            'km': round(row.distance_km, 1) if row.distance_km is not None else None,
            'soc_used': None,
            'regen_kwh': None,
            'source': 'sdk',
            'drive_min': row.drive_minutes,
            'idle_min': row.idle_minutes,
            'avg_speed_kmh': row.avg_speed_kmh,
            'max_speed_kmh': row.max_speed_kmh,
            'inferred_endpoints': (origin_pe is not None or dest_pe is not None),
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
               .filter(ParkingEvent.label != 'unknown')
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
