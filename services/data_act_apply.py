# -*- coding: utf-8 -*-
"""Files what ``services.data_act`` derived into the tracker's tables.

Three rules decide everything here:

* **One real event, one row.** An export of the last 7/14/30 days that is
  requested again overlaps the previous one, and the car may also have been
  read live (the Home Assistant connector files its own charge entries).
  Every imported charge carries a marker ``data-act:<start>-<end>`` in its
  notes; a charge or trip that overlaps one that is already there is not
  inserted again.
* **The user's data wins.** An entry that is already there keeps what it
  has (price, location, a metered kWh figure from a wallbox); the import
  only fills what is empty.
* **Reversible.** Everything applied is journalled (new ids, old values of
  merged rows) so the last import can be undone.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

MARK_RE = re.compile(r'data-act:(\d+)-(\d+)')
TOL_S = 1800                      # slack when comparing windows
SOURCE = 'data_act'
KWH_CAP_FACTOR = 1.3              # same hard cap as the manual charge form


def marker(c):
    return 'data-act:%d-%d' % (c['start'], c['end'])


def _local(ts):
    return datetime.fromtimestamp(int(ts))


def _window_of(ch):
    """(start_epoch, end_epoch, exact) of an existing Charge row."""
    m = MARK_RE.search(ch.notes or '')
    if m:
        return int(m.group(1)), int(m.group(2)), True
    if ch.charge_hour is None:
        return None
    s = datetime(ch.date.year, ch.date.month, ch.date.day, int(ch.charge_hour))
    eh = ch.charge_end_hour if ch.charge_end_hour is not None else ch.charge_hour
    e = datetime(ch.date.year, ch.date.month, ch.date.day, int(eh), 59, 59)
    if e < s:
        e += timedelta(days=1)
    return int(s.timestamp()), int(e.timestamp()), False


def _overlaps(a0, a1, b0, b1, tol=TOL_S):
    return a0 <= b1 + tol and b0 <= a1 + tol


def vin_mismatch(vehicle, export_vin):
    have = (getattr(vehicle, 'api_vin', None) or '').strip().upper()
    got = (export_vin or '').strip().upper()
    return bool(have and got and len(have) >= 10 and len(got) >= 10 and have != got)


def make_plan(result, vehicle_id, battery_kwh=None):
    """Compare the derived events with the database. Pure read.

    Returns ``{'charges': [...], 'trips': [...], 'summary': {...}}`` where
    every item is the derived dict plus ``status`` (new / merge / extend /
    dup / live / implausible) and, where relevant, ``target`` (row id)."""
    from models.database import Charge, VehicleTrip
    charges, trips = [], []
    cap = battery_kwh * KWH_CAP_FACTOR if battery_kwh else None

    ch_rows = {}
    if result['charges']:
        lo = _local(min(c['start'] for c in result['charges'])).date() - timedelta(days=2)
        hi = _local(max(c['end'] for c in result['charges'])).date() + timedelta(days=2)
        for r in Charge.query.filter(Charge.vehicle_id == vehicle_id,
                                     Charge.date >= lo, Charge.date <= hi).all():
            w = _window_of(r)
            if w:
                ch_rows[r.id] = (r, w)
    taken = set()
    for c in sorted(result['charges'], key=lambda x: x['start']):
        item = dict(c)
        item['status'] = 'new'
        if cap and c.get('kwh') and c['kwh'] > cap:
            item['status'] = 'implausible'
        else:
            for rid, (r, (w0, w1, exact)) in ch_rows.items():
                if rid in taken or not _overlaps(c['start'], c['end'], w0, w1):
                    continue
                taken.add(rid)
                item['target'] = rid
                if exact:
                    item['status'] = ('extend' if c['duration'] > (w1 - w0) + 60
                                      else 'dup')
                else:
                    item['status'] = 'merge'
                break
        charges.append(item)

    tr_rows = []
    if result['trips']:
        lo = _local(min(t['start'] for t in result['trips'])) - timedelta(days=1)
        hi = _local(max(t['end'] for t in result['trips'])) + timedelta(days=1)
        tr_rows = VehicleTrip.query.filter(
            VehicleTrip.vehicle_id == vehicle_id,
            VehicleTrip.start_time >= lo, VehicleTrip.start_time <= hi).all()
    spans = []
    for r in tr_rows:
        s = int(r.start_time.timestamp())
        dur = ((r.drive_minutes or 0) + (r.idle_minutes or 0)) * 60
        spans.append((r, s, s + max(dur, 60)))
    used = set()
    for t in sorted(result['trips'], key=lambda x: x['start']):
        item = dict(t)
        item['status'] = 'new'
        for r, s0, s1 in spans:
            if r.id in used or not _overlaps(t['start'], t['end'], s0, s1, 120):
                continue
            used.add(r.id)
            item['target'] = r.id
            if r.source == SOURCE:
                item['status'] = ('extend' if (t['distance_km'] or 0)
                                  > (r.distance_km or 0) + 0.05 else 'dup')
            else:
                item['status'] = 'live'
            break
        trips.append(item)

    def count(items):
        d = {}
        for i in items:
            d[i['status']] = d.get(i['status'], 0) + 1
        return d
    return {'charges': charges, 'trips': trips,
            'summary': {'charges': count(charges), 'trips': count(trips)}}


def apply_plan(plan, vehicle_id, data_dir, job_id, battery_kwh=None,
               efficiency=None):
    """Write the plan. Returns counts. The caller owns the transaction's
    surroundings (backup, background CO2 fill)."""
    from models.database import db, Charge, VehicleTrip
    journal = {'job': job_id, 'at': time.strftime('%Y-%m-%d %H:%M:%S'),
               'vehicle_id': vehicle_id, 'charges_new': [], 'trips_new': [],
               'charges_changed': [], 'trips_changed': []}
    n = {'charges_new': 0, 'charges_merged': 0, 'charges_extended': 0,
         'trips_new': 0, 'trips_extended': 0}

    for c in plan['charges']:
        st = c['status']
        if st in ('dup', 'implausible'):
            continue
        s, e = _local(c['start']), _local(c['end'])
        values = {
            'date': s.date(), 'charge_hour': s.hour, 'charge_end_hour': e.hour,
            'odometer': c.get('odometer'), 'kwh_loaded': c.get('kwh'),
            'charge_type': c.get('type') or 'AC',
            'soc_from': c.get('soc_from'), 'soc_to': c.get('soc_to'),
        }
        note = '%s · peak %s kW%s' % (
            marker(c), c.get('peak_kw'),
            (' · avg %s kW' % c['avg_kw']) if c.get('avg_kw') else '')
        if st == 'new':
            row = Charge(vehicle_id=vehicle_id, notes=note, needs_review=True,
                         **values)
            row.calculate_fields(battery_kwh, efficiency)
            db.session.add(row)
            db.session.flush()
            journal['charges_new'].append(row.id)
            n['charges_new'] += 1
            continue
        row = db.session.get(Charge, c['target'])
        if row is None:
            continue
        before = {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                  for k, v in ((k, getattr(row, k)) for k in
                               list(values) + ['notes', 'soc_charged',
                                               'loss_kwh', 'loss_pct'])}
        if st == 'merge':
            for k in ('odometer', 'kwh_loaded', 'soc_from', 'soc_to',
                      'charge_end_hour'):
                if getattr(row, k) is None and values.get(k) is not None:
                    setattr(row, k, values[k])
            if not row.charge_type:
                row.charge_type = values['charge_type']
            row.notes = ((row.notes + '\n') if row.notes else '') + note
            n['charges_merged'] += 1
        else:                                       # extend an earlier import
            for k, v in values.items():
                if v is not None:
                    setattr(row, k, v)
            row.notes = MARK_RE.sub(marker(c), row.notes or '') or note
            n['charges_extended'] += 1
        if st == 'extend':
            row.loss_kwh = None
            row.loss_pct = None
        row.calculate_fields(battery_kwh, efficiency)
        journal['charges_changed'].append({'id': row.id, 'before': before})

    for t in plan['trips']:
        st = t['status']
        if st in ('dup', 'live'):
            continue
        s = _local(t['start'])
        drive = round(t['duration'] / 60.0)
        moving = round((t.get('moving_s') or 0) / 60.0)
        vals = dict(
            trip_date=s.date(), start_time=s, drive_minutes=drive,
            idle_minutes=max(0, drive - moving),
            distance_km=t['distance_km'], avg_speed_kmh=t.get('avg_speed'),
            max_speed_kmh=t.get('max_speed'),
            consumption_kwh_per_100km=t.get('consumption'),
            regen_kwh_per_100km=t.get('regen_per_100'),
            source=SOURCE, fetched_at=datetime.now())
        if st == 'new':
            row = VehicleTrip(vehicle_id=vehicle_id, **vals)
            db.session.add(row)
            db.session.flush()
            journal['trips_new'].append(row.id)
            n['trips_new'] += 1
        else:
            row = db.session.get(VehicleTrip, t['target'])
            if row is None:
                continue
            before = {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                      for k, v in ((k, getattr(row, k)) for k in vals)}
            for k, v in vals.items():
                setattr(row, k, v)
            journal['trips_changed'].append({'id': row.id, 'before': before})
            n['trips_extended'] += 1
    db.session.commit()
    _write_journal(data_dir, journal)
    return n


def _journal_dir(data_dir):
    d = os.path.join(data_dir, 'data_act', 'journal')
    os.makedirs(d, exist_ok=True)
    return d


def _write_journal(data_dir, journal):
    p = os.path.join(_journal_dir(data_dir), '%s.json' % journal['job'])
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(journal, fh)


def last_journal(data_dir):
    d = _journal_dir(data_dir)
    names = sorted(f for f in os.listdir(d) if f.endswith('.json'))
    if not names:
        return None
    p = os.path.join(d, names[-1])
    with open(p, encoding='utf-8') as fh:
        j = json.load(fh)
    j['_path'] = p
    return j


def undo_last(data_dir, release_fn=None):
    """Take back the newest journalled import. Returns counts or None."""
    from models.database import db, Charge, VehicleTrip
    j = last_journal(data_dir)
    if not j:
        return None
    removed = restored = 0
    for cid in j.get('charges_new', []):
        row = db.session.get(Charge, cid)
        if row is not None:
            if release_fn:
                release_fn(row)
            db.session.delete(row)
            removed += 1
    for tid in j.get('trips_new', []):
        row = db.session.get(VehicleTrip, tid)
        if row is not None:
            db.session.delete(row)
            removed += 1
    for item in j.get('charges_changed', []):
        row = db.session.get(Charge, item['id'])
        if row is None:
            continue
        for k, v in item['before'].items():
            if k == 'date' and isinstance(v, str):
                v = datetime.strptime(v[:10], '%Y-%m-%d').date()
            setattr(row, k, v)
        restored += 1
    for item in j.get('trips_changed', []):
        row = db.session.get(VehicleTrip, item['id'])
        if row is None:
            continue
        for k, v in item['before'].items():
            if k == 'trip_date' and isinstance(v, str):
                v = datetime.strptime(v[:10], '%Y-%m-%d').date()
            elif k in ('start_time', 'fetched_at') and isinstance(v, str):
                v = datetime.fromisoformat(v)
            setattr(row, k, v)
        restored += 1
    db.session.commit()
    os.replace(j['_path'], j['_path'] + '.undone')
    return {'removed': removed, 'restored': restored}
