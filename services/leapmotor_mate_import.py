# -*- coding: utf-8 -*-
"""Files the finished charges Leapmotor Mate measured into the tracker.

Called by the sync after every successful read of a car on the
``leapmotor_mate`` connector (see ``services/vehicle/connector_leapmotor_mate``).
The rules are the Data-Act import's (``services/data_act_apply``):

* **One real charge, one row.** Every filed charge carries
  ``leapmotor-mate:<id>@<start>-<end>`` in its notes. The same Mate id is
  never filed twice, and a charge that overlaps any charge already in the
  tracker — typed by hand, from a wallbox, from a Data-Act export — is
  left alone: the owner's entry wins.
* **Nothing the owner has reviewed is changed.** Mate may join two
  pieces of one plug-in later (its "merge"); a filed charge that still
  waits for review takes the new figures, a reviewed one keeps its own.
* **New rows wait for review** like every automatically detected charge,
  so price and place are confirmed by a person.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MARK_RE = re.compile(r'leapmotor-mate:(\d+)@(\d+)-(\d+)')
KWH_CAP_FACTOR = 1.3              # same hard cap as the manual charge form
_SOURCE_NOTE = {'charger': 'charger', 'wallbox': 'wallbox meter',
                'battery': 'battery side (car)'}


def marker(s) -> str:
    return 'leapmotor-mate:%d@%d-%d' % (s['id'] or 0, s['start'], s['end'])


def clock_text(s) -> str:
    """The real start and end as clock times, e.g. ``13:05–13:52``.

    The charge row keeps the minutes too (since v3.0.200); the note repeats
    them in readable form so they can be read without decoding the marker."""
    a, b = datetime.fromtimestamp(s['start']), datetime.fromtimestamp(s['end'])
    text = '%s–%s' % (a.strftime('%H:%M'), b.strftime('%H:%M'))
    days = (b.date() - a.date()).days
    return text + (' (+%d d)' % days if days > 0 else '')


def _note(s) -> str:
    parts = [marker(s), clock_text(s)]
    if s.get('kwh') is not None:
        parts.append('%s kWh from %s' % (s['kwh'], _SOURCE_NOTE.get(s.get('kwh_source'), '?')))
    if s.get('peak_kw') is not None:
        parts.append('peak %s kW' % s['peak_kw'])
    if s.get('place'):
        parts.append(str(s['place'])[:120])
    return ' · '.join(parts)


def attach_odometer(sessions, trip_rows):
    """A session without an odometer of its own (older Mate versions write
    none at the charge) gets the one of the last drive that ended before it."""
    from services.vehicle.connector_leapmotor_mate import _epoch, _odo
    readings = sorted(
        (e, o) for e, o in ((_epoch(r.get('ended_at')), _odo(r.get('end_odometer_km')))
                            for r in trip_rows or [])
        if e and o is not None)
    for s in sessions:
        if s.get('odometer') is not None:          # Mate wrote it at the charge
            continue
        best = None
        for end, odo in readings:
            if end > s['start'] + 60:
                break
            best = odo if best is None else max(best, odo)
        s['odometer'] = int(best) if best is not None else None
    return sessions


def _values(s):
    start, end = datetime.fromtimestamp(s['start']), datetime.fromtimestamp(s['end'])
    soc_from = int(round(s['soc_from'])) if s.get('soc_from') is not None else None
    soc_to = int(round(s['soc_to'])) if s.get('soc_to') is not None else None
    return {
        'date': start.date(), 'charge_hour': start.hour, 'charge_end_hour': end.hour,
        'charge_start_min': start.minute, 'charge_end_min': end.minute,
        'odometer': s.get('odometer'), 'kwh_loaded': s.get('kwh'),
        'charge_type': s.get('type') or 'AC', 'soc_from': soc_from, 'soc_to': soc_to,
        'location_lat': s.get('lat'), 'location_lon': s.get('lon'),
    }


def import_sessions(vehicle_id, sessions, battery_kwh=None, efficiency=None) -> dict:
    """File ``sessions`` for ``vehicle_id``. Returns counts. Commits."""
    from models.database import db, Charge
    from services.data_act_apply import _overlaps, _window_of

    n = {'new': 0, 'updated': 0, 'dup': 0, 'overlap': 0, 'implausible': 0}
    if not sessions or vehicle_id is None:
        return n
    cap = battery_kwh * KWH_CAP_FACTOR if battery_kwh else None
    lo = datetime.fromtimestamp(min(s['start'] for s in sessions)).date() - timedelta(days=2)
    hi = datetime.fromtimestamp(max(s['end'] for s in sessions)).date() + timedelta(days=2)
    rows = Charge.query.filter(Charge.vehicle_id == vehicle_id,
                               Charge.date >= lo, Charge.date <= hi).all()
    by_mate_id, windows = {}, []
    for r in rows:
        m = MARK_RE.search(r.notes or '')
        if m:
            by_mate_id[int(m.group(1))] = r
            windows.append((r, int(m.group(2)), int(m.group(3))))
            continue
        w = _window_of(r)
        if w:
            windows.append((r, w[0], w[1]))

    for s in sessions:
        if cap and s.get('kwh') and s['kwh'] > cap:
            n['implausible'] += 1
            continue
        own = by_mate_id.get(s['id']) if s.get('id') else None
        if own is not None:
            if own.needs_review and MARK_RE.search(own.notes or '').group(0) != marker(s):
                for k, v in _values(s).items():
                    if v is not None:
                        setattr(own, k, v)
                own.notes = MARK_RE.sub(marker(s), own.notes or '', count=1)
                own.loss_kwh = None
                own.loss_pct = None
                own.calculate_fields(battery_kwh, efficiency)
                n['updated'] += 1
            else:
                n['dup'] += 1
            continue
        clash = any(_overlaps(s['start'], s['end'], w0, w1, 300)
                    for r, w0, w1 in windows if r is not own)
        if clash:
            n['overlap'] += 1
            continue
        row = Charge(vehicle_id=vehicle_id, notes=_note(s), needs_review=True, **_values(s))
        row.calculate_fields(battery_kwh, efficiency)
        db.session.add(row)
        db.session.flush()
        windows.append((row, s['start'], s['end']))
        if s.get('id'):
            by_mate_id[s['id']] = row
        n['new'] += 1
    db.session.commit()
    if n['new'] or n['updated']:
        logger.info('Leapmotor Mate import for vehicle %s: %s', vehicle_id, n)
    return n


TRIP_SOURCE = 'leapmotor_mate'


def import_trips(vehicle_id, trips) -> dict:
    """File Mate's finished trips as trip-log rows. Returns counts. Commits.

    Mate's trips export has no coordinates, so a row has date, start time,
    duration, distance, the SoC at both ends and Mate's place names as text;
    the trip log shows the place names unless the parking history knows the
    stop better. A trip is filed once (same start time); one that overlaps a
    trip already in the log — from the vehicle's cloud, a data export — is
    left alone. A Mate trip filed before SoC and places were kept gets the
    EMPTY ones of those four fields filled on the next sync (``filled``);
    a value already there is never overwritten."""
    from models.database import db, VehicleTrip

    n = {'new': 0, 'dup': 0, 'overlap': 0, 'filled': 0}
    if not trips or vehicle_id is None:
        return n
    lo = datetime.fromtimestamp(min(t['start'] for t in trips)) - timedelta(days=1)
    hi = datetime.fromtimestamp(max(t['end'] for t in trips)) + timedelta(days=1)
    rows = VehicleTrip.query.filter(VehicleTrip.vehicle_id == vehicle_id,
                                    VehicleTrip.start_time >= lo,
                                    VehicleTrip.start_time <= hi).all()
    windows = []
    by_start = {}
    for r in rows:
        a = r.start_time
        mins = (r.drive_minutes or 0) + (r.idle_minutes or 0)
        windows.append((a, a + timedelta(minutes=mins)))
        by_start.setdefault(a, r)
    for t in trips:
        a = datetime.fromtimestamp(t['start']).replace(microsecond=0)
        b = datetime.fromtimestamp(t['end'])
        if any(w0 == a for w0, _ in windows):
            n['dup'] += 1
            old = by_start.get(a)
            if old is not None and old.source == TRIP_SOURCE:
                changed = False
                for col, key in (('start_soc', 'soc_from'), ('end_soc', 'soc_to'),
                                 ('start_place', 'place_from'), ('end_place', 'place_to')):
                    if getattr(old, col) is None and t.get(key) is not None:
                        setattr(old, col, t.get(key))
                        changed = True
                if changed:
                    n['filled'] += 1
            continue
        if any(w0 <= b and w1 >= a for w0, w1 in windows):
            n['overlap'] += 1
            continue
        db.session.add(VehicleTrip(
            vehicle_id=vehicle_id, trip_date=a.date(), start_time=a,
            drive_minutes=t['minutes'], idle_minutes=0, distance_km=t['km'],
            avg_speed_kmh=t.get('avg_kmh'), source=TRIP_SOURCE,
            start_soc=t.get('soc_from'), end_soc=t.get('soc_to'),
            start_place=t.get('place_from'), end_place=t.get('place_to'),
            fetched_at=datetime.now()))
        windows.append((a, b))
        n['new'] += 1
    db.session.commit()
    if n['new'] or n['filled']:
        logger.info('Leapmotor Mate trips for vehicle %s: %s', vehicle_id, n)
    return n


def import_all(vehicle_id, connector, battery_kwh=None, efficiency=None) -> dict:
    """What a read of ``connector`` left behind, filed: charges and trips.

    Shared by the background sync and the trip log's "sync now" button.
    Each part fails on its own; the result says what was read and filed."""
    from models.database import db
    res = {'charges': None, 'trips': None,
           'charges_read': len(getattr(connector, 'charge_sessions', None) or []),
           'trips_read': int(getattr(connector, 'trip_rows_read', 0) or 0),
           'trips_usable': len(getattr(connector, 'trip_sessions', None) or [])}
    try:
        res['charges'] = import_sessions(
            vehicle_id, getattr(connector, 'charge_sessions', None) or [],
            battery_kwh, efficiency)
    except Exception as e:
        db.session.rollback()
        logger.warning('Leapmotor Mate: charge import failed: %s', e)
    try:
        res['trips'] = import_trips(vehicle_id, getattr(connector, 'trip_sessions', None) or [])
    except Exception as e:
        db.session.rollback()
        logger.warning('Leapmotor Mate: trip import failed: %s', e)
    return res
