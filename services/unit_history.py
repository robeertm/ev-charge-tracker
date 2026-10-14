"""A car's history recorded in miles, converted to the km the app stores
(v3.0.155).

Up to v3.0.154 every connector except Tesla handed the odometer, range and
trip distances over exactly as the manufacturer's cloud sent them. For a
US Kia or Hyundai that is MILES (hyundai_kia_connect_api hard-codes the
unit for the USA), and the app wrote them into columns called ``_km``.
Everything the app computed from them — trips, km/h, kWh per 100 km — was
therefore in miles too, consistently, just labelled wrong.

From v3.0.155 the connectors convert at the door, so new numbers are km.
The rows already stored are converted here, once per vehicle, so the old
and the new half of the history agree.

🔑 Nothing is lost:

* one transaction — either every column of the car is converted or none;
* a copy of the database file is written first, next to it;
* the vehicle remembers that its history was converted (``history_unit_fixed_at``)
  — the marker sits on the car, not on the installation, so a second car
  that arrives later is judged on its own;
* the conversion can be undone (``direction='km_to_mi'``). An integer
  odometer survives the round trip exactly: a mile is longer than a
  kilometre, so rounding to whole km never lands two miles on one value.

Which columns: everything that is a distance or a rate per distance and
belongs to one car. GPS positions are not distances (they stay as they
are), wallbox and price data have no distance at all.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

from sqlalchemy import text

from services.units import KM_PER_MI

logger = logging.getLogger(__name__)

# (table, column, shape) — 'int' and 'len' are lengths, 'rate' is per length.
COLUMNS = (
    ('charges', 'odometer', 'int'),
    ('vehicle_syncs', 'odometer_km', 'int'),
    ('vehicle_syncs', 'estimated_range_km', 'int'),
    ('vehicle_syncs', 'consumption_30d_kwh_per_100km', 'rate'),
    ('parking_events', 'odometer_arrived', 'int'),
    ('parking_events', 'odometer_departed', 'int'),
    ('vehicle_trips', 'distance_km', 'len'),
    ('vehicle_trips', 'avg_speed_kmh', 'len'),
    ('vehicle_trips', 'max_speed_kmh', 'int'),
    ('vehicle_trips', 'regen_kwh_per_100km', 'rate'),
    ('vehicle_trips', 'consumption_kwh_per_100km', 'rate'),
    ('maintenance_log', 'odometer_km', 'int'),
    ('maintenance_log', 'next_due_km', 'int'),
    ('obd_readings', 'odometer_km', 'int'),
)

# Cars whose cloud reports miles without saying so per value: the
# hyundai_kia_connect_api USA implementations pass DISTANCE_UNITS[3] ("mi")
# for the odometer and the trips. Canada reports km.
MILES_REGIONS = {'kia': {'US'}, 'hyundai': {'US'}}


def reports_miles(vehicle) -> bool:
    brand = (getattr(vehicle, 'api_brand', None) or '').strip().lower()
    region = (getattr(vehicle, 'api_region', None) or '').strip().upper()
    return region in MILES_REGIONS.get(brand, set())


def _db_path(db):
    url = str(db.engine.url)
    if not url.startswith('sqlite:///'):
        return None
    path = db.engine.url.database
    return path if path and path != ':memory:' and os.path.exists(path) else None


def backup(db, tag: str):
    """A consistent copy of the live database next to it (SQLite's own
    backup API, safe while the app writes). Returns the path or None for
    an in-memory database."""
    src = _db_path(db)
    if not src:
        return None
    dst = '%s.vor-%s-%s.bak' % (src, tag, datetime.now().strftime('%Y%m%d-%H%M%S'))
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    os.chmod(dst, 0o600)
    return dst


def count_rows(db, vehicle_id: int) -> dict:
    """How many values per column would change — shown before anyone presses."""
    out = {}
    for table, col, _ in COLUMNS:
        try:
            n = db.session.execute(text(
                f'SELECT COUNT(*) FROM {table} WHERE vehicle_id = :v AND {col} IS NOT NULL'),
                {'v': vehicle_id}).scalar()
        except Exception:
            n = 0  # table/column absent on a very old schema
        if n:
            out[f'{table}.{col}'] = int(n)
    return out


def convert(db, vehicle, direction: str = 'mi_to_km', reason: str = 'manual') -> dict:
    """Convert every stored distance of one car. ``mi_to_km`` is the fix,
    ``km_to_mi`` its undo. Refuses to run twice in the same direction."""
    if direction not in ('mi_to_km', 'km_to_mi'):
        raise ValueError(direction)
    already = vehicle.history_unit_fixed_at is not None
    if direction == 'mi_to_km' and already:
        return {'ok': False, 'reason': 'already'}
    if direction == 'km_to_mi' and (not already or vehicle.distance_source_unit == 'mi'):
        # never undo what the car's own cloud established — its new syncs
        # arrive in km, an undo would split the history again
        return {'ok': False, 'reason': 'not_converted'}

    length = KM_PER_MI if direction == 'mi_to_km' else 1 / KM_PER_MI
    rate = 1 / length
    path = backup(db, 'meilen-%s-fz%d' % (direction, vehicle.id))
    counts = count_rows(db, vehicle.id)
    try:
        for table, col, shape in COLUMNS:
            if f'{table}.{col}' not in counts:
                continue
            if shape == 'int':
                expr = f'CAST(ROUND({col} * :f) AS INTEGER)'
                f = length
            elif shape == 'len':
                expr, f = f'{col} * :f', length
            else:
                expr, f = f'{col} * :f', rate
            db.session.execute(text(
                f'UPDATE {table} SET {col} = {expr} WHERE vehicle_id = :v AND {col} IS NOT NULL'),
                {'f': f, 'v': vehicle.id})
        vehicle.history_unit_fixed_at = datetime.now() if direction == 'mi_to_km' else None
        if direction == 'mi_to_km' and reason != 'settings':
            # the car's cloud said so — a fact, not a choice; the settings
            # card offers no undo for it (new syncs arrive in km)
            vehicle.distance_source_unit = 'mi'
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('unit_history: conversion of vehicle %s failed — nothing changed', vehicle.id)
        raise
    logger.warning('unit_history: vehicle %s %s (%s) — %s, backup %s',
                   vehicle.id, direction, reason, counts, path)
    return {'ok': True, 'counts': counts, 'backup': path}


def convert_miles_cars_on_start(db):
    """At start-up: a US Kia/Hyundai whose history is still in miles is
    converted before its first sync under this version writes km next to
    it. Runs per car; ``distance_source_unit`` set means "already judged",
    so a later start never converts the new km rows a second time."""
    from models.database import Vehicle
    done = []
    for v in Vehicle.query.all():
        if v.distance_source_unit is not None or not reports_miles(v):
            continue
        if count_rows(db, v.id):
            convert(db, v, 'mi_to_km', reason='start: %s %s' % (v.api_brand, v.api_region))
            done.append(v.id)
        else:
            v.distance_source_unit = 'mi'   # nothing stored yet, nothing to convert
            db.session.commit()
    return done


def note_reported_unit(db, vehicle, unit):
    """Called on every sync with the unit the car's cloud named. The first
    time a car says "mi", what it stored before was miles → convert once.
    A car that says "km" is simply remembered."""
    if unit not in ('km', 'mi') or vehicle.distance_source_unit is not None:
        return None
    if unit == 'mi' and vehicle.history_unit_fixed_at is None and count_rows(db, vehicle.id):
        return convert(db, vehicle, 'mi_to_km', reason='sync reported miles')
    vehicle.distance_source_unit = unit
    db.session.commit()
    return None
