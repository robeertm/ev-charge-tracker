"""PE ↔ SDK trip reconciliation (Kia + Hyundai).

Both Kia UVO and Hyundai Bluelink return a ``/tripinfo`` endpoint with
minute-accurate start / drive-time / idle-time / distance per trip, but
**no GPS / SoC / odometer**. Our polling-based ParkingEvent chain has
the locations but derived timestamps: ``arrived_at`` = first sync at new
spot (generally within ~10 min of actual arrival), ``departed_at`` =
last at-spot sync (can be HOURS stale — the car usually sleeps at the
origin through the smart-mode window, so the last confirmation of
"still here" is the arrival sync itself, not anything close to the
actual drive-off moment).

v2.28.12 anchors the match on ``curr.arrived_at`` ≈ SDK trip end-time
rather than on ``prev.departed_at`` ≈ SDK start-time, because
``arrived_at`` is the reliable end of a PE pair and ``departed_at`` may
be days off before reconciliation. We overwrite
``prev.departed_at = sdk.start_time`` plus — added in v2.28.20 —
``curr.arrived_at = sdk.start_time + drive + idle`` whenever that
shifts the timestamp EARLIER (first-parked-sync is by definition ≥
actual arrival; any shift the other way is nonsense). This applies to
both brands: both exhibit the "sleep at origin → stale departed_at"
failure mode regardless of platform (400 V or e-GMP), and both suffer
the poll-lag on arrivals.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from models.database import db, AppConfig, ParkingEvent, VehicleTrip

logger = logging.getLogger(__name__)

# Matching tolerances — tight enough that we don't cross-pair adjacent
# trips, loose enough to absorb sparse polling on Hyundai Bluelink.
#
# Hyundai's cloud cache can go 10+ hours between GPS-bearing syncs
# when the car is sleeping at the origin (our state machine correctly
# ignores GPS-less syncs, but then detects the arrival at B only on
# the NEXT GPS-bearing poll — which can be hours after the car physically
# arrived). The old 20-min window misses these cases entirely: PE
# arrival is 2 h+ later than the SDK-reported drive end, so no match,
# so the trip renders as BOTH a polled "14-hour drive" PE pair and a
# duplicate SDK-only fallback.
#
# 4 h covers the observed worst case (overnight → morning commute).
# Safe because:
#  1. The physical conflict check in the apply loop still requires
#     ``sdk.start_time ∈ (prev.arrived_at, curr.arrived_at]``.
#  2. The ``km`` check (REL/ABS below) rejects mismatched distances.
#  3. The greedy allocator scores by closest time-delta, so even when
#     multiple SDK trips fall in the window the best pairing wins.
_ARRIVAL_TOL_MIN = 240   # sdk.end_time must be within ±4 h of curr.arrived_at
_KM_TOL_REL = 0.25       # distance may differ by ≤ 25 %…
_KM_TOL_ABS = 3.0        # …or ≤ 3 km absolute (helps very short trips)


def _brand_supports_trip_info(vehicle_id=None) -> bool:
    """Gate: only run where the SDK actually returns day_trip_info data.
    Currently both Kia UVO and Hyundai Bluelink do.

    v2.29: when ``vehicle_id`` is set, check that vehicle's brand;
    otherwise fall back to the legacy AppConfig single-vehicle path.
    """
    brand = ''
    if vehicle_id is not None:
        from models.database import Vehicle
        v = Vehicle.query.get(vehicle_id)
        if v is not None and v.api_brand:
            brand = v.api_brand.lower()
    if not brand:
        brand = (AppConfig.get('vehicle_api_brand', '') or '').lower()
    return brand in ('kia', 'hyundai')


def reconcile_day(target_date: date, vehicle_id=None) -> dict:
    """Align each PE pair's ``departed_at`` with its matching SDK trip
    for a single calendar day.

    Returns a summary dict:
    ``{'date', 'applied', 'skipped_conflict', 'unmatched_pe', 'unmatched_sdk',
       'changes': [...]}``

    No-op (with ``skipped_reason``) when the active brand doesn't
    expose a day_trip_info endpoint.
    """
    out = {
        'date': target_date.isoformat(),
        'applied': 0,
        'arr_applied': 0,
        'skipped_conflict': 0,
        'unmatched_pe': 0,
        'unmatched_sdk': 0,
        'changes': [],
        'skipped_reason': None,
    }
    if not _brand_supports_trip_info(vehicle_id):
        out['skipped_reason'] = 'brand_unsupported'
        return out

    # PE pairs where either the (stored, possibly-stale) departed_at or
    # the (reliable) arrived_at falls on target_date. v2.29: scoped to
    # the vehicle when given so different fleet members' PE chains
    # don't share one reconcile pass.
    _ev_q = ParkingEvent.query
    if vehicle_id is not None:
        _ev_q = _ev_q.filter_by(vehicle_id=vehicle_id)
    events = list(_ev_q.order_by(ParkingEvent.arrived_at.asc()).all())
    pairs = []
    for prev, curr in zip(events, events[1:]):
        if prev.departed_at is None:
            continue
        if (prev.departed_at.date() != target_date
                and curr.arrived_at.date() != target_date):
            continue
        if prev.odometer_departed is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_departed, 0)
        elif prev.odometer_arrived is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_arrived, 0)
        else:
            km = None
        pairs.append((prev, curr, km))

    # SDK trips for that day, scoped to the same vehicle.
    _sdk_q = VehicleTrip.query.filter_by(trip_date=target_date)
    if vehicle_id is not None:
        _sdk_q = _sdk_q.filter_by(vehicle_id=vehicle_id)
    sdk_rows = list(_sdk_q.order_by(VehicleTrip.start_time.asc()).all())

    if not pairs or not sdk_rows:
        out['unmatched_pe'] = len(pairs)
        out['unmatched_sdk'] = len(sdk_rows)
        return out

    # Score every (pair, sdk) combo that passes tolerance + km check,
    # then greedy-allocate closest first so one SDK trip can only bind
    # to one PE pair (and vice versa). Anchor: sdk.end_time ≈ arrived_at.
    scored = []
    for pi, (prev, curr, km) in enumerate(pairs):
        for si, t in enumerate(sdk_rows):
            total_min = (t.drive_minutes or 0) + (t.idle_minutes or 0)
            sdk_arrived = t.start_time + timedelta(minutes=total_min)
            delta_min = abs((sdk_arrived - curr.arrived_at).total_seconds()) / 60.0
            if delta_min > _ARRIVAL_TOL_MIN:
                continue
            sdk_km = float(t.distance_km) if t.distance_km is not None else None
            if km is not None and sdk_km is not None:
                diff_abs = abs(km - sdk_km)
                diff_rel = diff_abs / max(km, sdk_km, 1.0)
                if diff_abs > _KM_TOL_ABS and diff_rel > _KM_TOL_REL:
                    continue
            scored.append((delta_min, pi, si, prev, curr, t, km))
    scored.sort(key=lambda r: r[0])

    used_pair: set = set()
    used_sdk: set = set()
    matches = []
    for delta_min, pi, si, prev, curr, t, km in scored:
        if pi in used_pair or si in used_sdk:
            continue
        # Reject conflicted matches HERE, before marking used. A conflict
        # (new_dep < prev.arrived_at, typical for phantom Home→X→Home PE
        # splits where a phantom pair would demand an impossibly-early
        # departure) must not burn either the SDK trip or the PE pair —
        # otherwise the valid alternative match can't claim them. Before
        # v2.28.15 the conflict check lived in the apply-loop below, so
        # both slots stayed used-marked and the real pair got no SDK
        # stats and no departed_at correction.
        new_dep = t.start_time
        conflict = None
        if prev.arrived_at and new_dep < prev.arrived_at:
            conflict = 'new_dep < prev.arrived_at'
        elif curr.arrived_at and new_dep > curr.arrived_at:
            conflict = 'new_dep > curr.arrived_at'
        if conflict:
            out['skipped_conflict'] += 1
            logger.info(
                f"trip_reconcile {target_date} PE#{prev.id}→#{curr.id} "
                f"SDK#{t.id} skipped: {conflict}"
            )
            continue
        used_pair.add(pi)
        used_sdk.add(si)
        matches.append((prev, curr, t, km, delta_min))

    # Apply — rewrite prev.departed_at (always when the SDK start_time
    # differs) and curr.arrived_at (only when the SDK-derived end-time
    # is EARLIER than the stored arrival — first-parked-sync cannot
    # happen before the actual arrival, so shifts later would be
    # spurious). Conflict checks already happened in the greedy
    # allocator above.
    for prev, curr, t, km, delta_min in matches:
        change = None
        new_dep = t.start_time
        old_dep = prev.departed_at
        if new_dep != old_dep:
            prev.departed_at = new_dep
            out['applied'] += 1
            change = {
                'pe_from': prev.id, 'pe_to': curr.id, 'sdk_id': t.id,
                'old_dep': old_dep.isoformat() if old_dep else None,
                'new_dep': new_dep.isoformat(),
                'pe_arrived_at': curr.arrived_at.isoformat(),
                'sdk_km': float(t.distance_km) if t.distance_km is not None else None,
                'pe_km': km,
                'delta_min': round(delta_min, 1),
            }

        total_min = (t.drive_minutes or 0) + (t.idle_minutes or 0)
        new_arr = t.start_time + timedelta(minutes=total_min)
        old_arr = curr.arrived_at
        if new_arr < old_arr:
            arr_delta_s = (old_arr - new_arr).total_seconds()
            if arr_delta_s >= 1:
                curr.arrived_at = new_arr
                out['arr_applied'] += 1
                if change is None:
                    change = {
                        'pe_from': prev.id, 'pe_to': curr.id, 'sdk_id': t.id,
                        'sdk_km': float(t.distance_km) if t.distance_km is not None else None,
                        'pe_km': km,
                        'delta_min': round(delta_min, 1),
                    }
                change['old_arr'] = old_arr.isoformat()
                change['new_arr'] = new_arr.isoformat()
                change['arr_delta_min'] = round(arr_delta_s / 60.0, 2)

        if change is not None:
            out['changes'].append(change)

    if out['applied'] or out['arr_applied']:
        db.session.commit()

    out['unmatched_pe'] = len(pairs) - len(matches)
    out['unmatched_sdk'] = len(sdk_rows) - len(matches)
    return out


def reconcile_range(days: int = 7, vehicle_id=None) -> dict:
    """Reconcile the last ``days`` calendar days, newest first. Returns
    a shape-stable dict even when the active brand is unsupported.

    v2.29: ``vehicle_id`` scopes everything; None falls back to the
    legacy single-vehicle path that mostly only worked correctly when
    AppConfig.vehicle_api_brand was set anyway.
    """
    out = {'days_attempted': 0, 'total_applied': 0, 'total_arr_applied': 0,
           'total_conflicts': 0, 'per_day': [], 'vehicle_id': vehicle_id}
    if not _brand_supports_trip_info(vehicle_id):
        out['skipped_reason'] = 'brand_unsupported'
        return out
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        r = reconcile_day(d, vehicle_id=vehicle_id)
        out['per_day'].append(r)
        out['total_applied'] += r.get('applied', 0)
        out['total_arr_applied'] += r.get('arr_applied', 0)
        out['total_conflicts'] += r.get('skipped_conflict', 0)
        out['days_attempted'] += 1
    if vehicle_id is None or vehicle_id == 1:
        AppConfig.set('last_reconcile_at', datetime.now().isoformat())
    else:
        AppConfig.set(f'last_reconcile_at_{vehicle_id}', datetime.now().isoformat())
    logger.info(
        f"trip_reconcile [vid={vehicle_id}]: {days}d walk, "
        f"{out['total_applied']} departed_at + "
        f"{out['total_arr_applied']} arrived_at corrected, "
        f"{out['total_conflicts']} conflicts skipped"
    )
    return out


def should_run_daily(vehicle_id=None) -> bool:
    """Gate for the sync-loop: only return True once per calendar day
    on brands that expose day_trip_info. The caller should call
    ``reconcile_range`` (and ``trip_log_fetch.backfill``) when this
    returns True.

    v2.29: per-vehicle key ``last_reconcile_at_{id}`` (Vehicle#1 keeps
    the legacy ``last_reconcile_at`` for nahtlosen Upgrade) so two
    Kia/Hyundai cars in a fleet each get their own once-per-day gate.
    """
    if not _brand_supports_trip_info(vehicle_id):
        return False
    if vehicle_id is None or vehicle_id == 1:
        last = AppConfig.get('last_reconcile_at', '')
    else:
        last = AppConfig.get(f'last_reconcile_at_{vehicle_id}', '')
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return last_dt.date() < date.today()


def gap_days_since_last_reconcile(vehicle_id=None,
                                  default_days: int = 3,
                                  cap_days: int = 30) -> int:
    """How many days back the next backfill should walk.

    Computes the gap between ``last_reconcile_at_{vid}`` and today.
    Used by the daily reconcile to handle long offline gaps (e.g. an
    EV-tracker VM that was LUKS-locked for a week — the SDK still has
    the trips, we just need to walk back further than the default 3).

    Returns ``default_days`` (3) for fresh installs / unparseable
    timestamps, the actual gap otherwise, capped at ``cap_days`` (30
    is roughly the Kia/Hyundai server-side trip-history retention).
    """
    if vehicle_id is None or vehicle_id == 1:
        last = AppConfig.get('last_reconcile_at', '')
    else:
        last = AppConfig.get(f'last_reconcile_at_{vehicle_id}', '')
    if not last:
        return default_days
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return default_days
    gap = (date.today() - last_dt.date()).days
    if gap < default_days:
        return default_days
    return min(gap, cap_days)
