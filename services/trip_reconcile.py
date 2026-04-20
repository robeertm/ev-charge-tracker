"""PE ↔ SDK trip reconciliation (Hyundai-only for now).

The Hyundai server's ``/tripinfo`` endpoint returns minute-accurate
start / drive-time / idle-time / distance per trip, but **no GPS / SoC /
odometer**. Our polling-based ParkingEvent chain has the locations but
rough timestamps (``departed_at`` = last at-spot sync, ``arrived_at`` =
first sync at new spot). On Hyundai e-GMP the sync cadence is coarse
enough that these timestamps can be 10 min → several hours off.

Where a PE pair cleanly maps 1:1 to a single SDK trip (close start time
+ matching distance), we overwrite the PE timestamps with the SDK
values. Other mismatches — PE merging several short SDK trips into one,
PE splitting one SDK trip via GPS jitter, PE missing a short SDK trip
entirely — are **not** touched here. Those represent different views of
the same journey and there is no mechanical way to reconcile without
risking data loss.

Why Hyundai-only: the 400 V platform cars (Kia Niro EV MY21 on our
reference installs) have consistently aligned PE / SDK timestamps in
practice; it's the e-GMP polling cadence that benefits from this fix.
Kept explicitly brand-gated via :func:`_is_hyundai` rather than running
everywhere and hoping.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from models.database import db, AppConfig, ParkingEvent, VehicleTrip

logger = logging.getLogger(__name__)

# Matching tolerances — tight enough that we don't cross-pair adjacent
# trips, loose enough to absorb normal polling-interval slack.
_START_TOL_MIN = 20      # SDK start_time must be within ±20 min of PE departed_at
_KM_TOL_REL = 0.25       # distance may differ by ≤ 25 %…
_KM_TOL_ABS = 3.0        # …or ≤ 3 km absolute (helps very short trips)


def _is_hyundai() -> bool:
    """Brand gate. Only run on Hyundai installs."""
    return AppConfig.get('vehicle_api_brand', '').lower() == 'hyundai'


def reconcile_day(target_date: date) -> dict:
    """Align PE pair timestamps with SDK trips for a single calendar day.

    Returns a summary dict:
    ``{'date', 'applied', 'skipped_conflict', 'unmatched_pe', 'unmatched_sdk',
       'changes': [...]}``

    No-op (with ``skipped_reason``) when the active brand isn't Hyundai
    — callers still get a consistent shape.
    """
    out = {
        'date': target_date.isoformat(),
        'applied': 0,
        'skipped_conflict': 0,
        'unmatched_pe': 0,
        'unmatched_sdk': 0,
        'changes': [],
        'skipped_reason': None,
    }
    if not _is_hyundai():
        out['skipped_reason'] = 'not_hyundai'
        return out

    # PE pairs whose departed_at falls on target_date
    events = list(ParkingEvent.query.order_by(ParkingEvent.arrived_at.asc()).all())
    pairs = []
    for prev, curr in zip(events, events[1:]):
        if prev.departed_at is None:
            continue
        if prev.departed_at.date() != target_date:
            continue
        if prev.odometer_departed is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_departed, 0)
        elif prev.odometer_arrived is not None and curr.odometer_arrived is not None:
            km = max(curr.odometer_arrived - prev.odometer_arrived, 0)
        else:
            km = None
        pairs.append((prev, curr, km))

    # SDK trips for that day
    sdk_rows = list(VehicleTrip.query
                    .filter_by(trip_date=target_date)
                    .order_by(VehicleTrip.start_time.asc())
                    .all())

    if not pairs or not sdk_rows:
        out['unmatched_pe'] = len(pairs)
        out['unmatched_sdk'] = len(sdk_rows)
        return out

    # Score every (pair, sdk) combo that passes tolerance + km check,
    # then greedy-allocate closest first so one SDK trip can only bind
    # to one PE pair (and vice versa).
    scored = []
    for pi, (prev, curr, km) in enumerate(pairs):
        for si, t in enumerate(sdk_rows):
            delta_min = abs((t.start_time - prev.departed_at).total_seconds()) / 60.0
            if delta_min > _START_TOL_MIN:
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
        used_pair.add(pi)
        used_sdk.add(si)
        matches.append((prev, curr, t, km, delta_min))

    # Apply — only when the new times don't violate adjacent-event boundaries.
    for prev, curr, t, km, delta_min in matches:
        total_min = (t.drive_minutes or 0) + (t.idle_minutes or 0)
        new_dep = t.start_time
        new_arr = t.start_time + timedelta(minutes=total_min)
        conflict = None
        if prev.arrived_at and new_dep < prev.arrived_at:
            conflict = 'new_dep < prev.arrived_at'
        elif curr.departed_at and new_arr > curr.departed_at:
            conflict = 'new_arr > curr.departed_at'
        elif new_arr < new_dep:
            conflict = 'new_arr < new_dep'
        if conflict:
            out['skipped_conflict'] += 1
            logger.info(
                f"trip_reconcile {target_date} PE#{prev.id}→#{curr.id} "
                f"SDK#{t.id} skipped: {conflict}"
            )
            continue

        old_dep, old_arr = prev.departed_at, curr.arrived_at
        if new_dep == old_dep and new_arr == old_arr:
            continue  # already aligned, no-op
        prev.departed_at = new_dep
        curr.arrived_at = new_arr
        out['applied'] += 1
        out['changes'].append({
            'pe_from': prev.id, 'pe_to': curr.id, 'sdk_id': t.id,
            'old_dep': old_dep.isoformat(), 'new_dep': new_dep.isoformat(),
            'old_arr': old_arr.isoformat(), 'new_arr': new_arr.isoformat(),
            'sdk_km': float(t.distance_km) if t.distance_km is not None else None,
            'pe_km': km,
            'delta_min': round(delta_min, 1),
        })

    if out['applied']:
        db.session.commit()

    out['unmatched_pe'] = len(pairs) - len(matches)
    out['unmatched_sdk'] = len(sdk_rows) - len(matches)
    return out


def reconcile_range(days: int = 7) -> dict:
    """Reconcile the last ``days`` calendar days, newest first. Stops
    at first Hyundai brand-gate reject so the caller sees the skip.
    """
    out = {'days_attempted': 0, 'total_applied': 0, 'total_conflicts': 0,
           'per_day': []}
    if not _is_hyundai():
        out['skipped_reason'] = 'not_hyundai'
        return out
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        r = reconcile_day(d)
        out['per_day'].append(r)
        out['total_applied'] += r.get('applied', 0)
        out['total_conflicts'] += r.get('skipped_conflict', 0)
        out['days_attempted'] += 1
    AppConfig.set('last_reconcile_at', datetime.now().isoformat())
    logger.info(
        f"trip_reconcile: {days}d walk, "
        f"{out['total_applied']} PE pairs corrected, "
        f"{out['total_conflicts']} conflicts skipped"
    )
    return out


def should_run_daily() -> bool:
    """Gate for the sync-loop: only return True once per calendar day
    for Hyundai installs. The caller should call ``reconcile_range``
    (and ``trip_log_fetch.backfill``) when this returns True."""
    if not _is_hyundai():
        return False
    last = AppConfig.get('last_reconcile_at', '')
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return last_dt.date() < date.today()
