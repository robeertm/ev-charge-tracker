# -*- coding: utf-8 -*-
"""A stay's proof does not end where the vendor's trip times say it does.

Instance case (2026-11, ten stays "Unknown location" although the raw syncs
held coordinates): a stay's arrival and departure are often the vendor's trip
times. The first sync that shows the car at the stay's odometer reading — and
the forced refresh with the real fix right behind it — can come a few minutes
AFTER the recorded departure. ``heal_unknown_stops`` only looked inside
[arrival, departure], so it saw nothing and the stay stayed a placeholder.

The odometer is the real boundary of a stay: same reading = same stay, until
the first sync that shows the car gone.

Also covered: a stay labelled ``unknown`` that already holds a real position,
and the address retry (stops without an address are picked up again without
anybody opening the trips page, and a stop the geocoder cannot resolve does not
block the ones behind it).

Coordinates and times are invented; only the shape is real.

Run with:  python3 -m pytest tests/test_unknown_stop_window.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evwin-'))

import pytest                                                        # noqa: E402
from flask import Flask                                              # noqa: E402

HOME = (50.1010, 10.2020)
MID = (50.1500, 10.2600)      # the stay in question
FEED_MID = (50.1300, 10.2300)  # what the lagging newer feed said meanwhile
OTHER = (50.2500, 10.4000)


@pytest.fixture(autouse=True)
def _berlin():
    before = os.environ.get('TZ')
    os.environ['TZ'] = 'Europe/Berlin'
    time.tzset()
    yield
    if before is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = before
    time.tzset()


@pytest.fixture()
def env():
    sys.modules.pop('services.trips_service', None)
    import services.trips_service as T
    M = sys.modules[T.ParkingEvent.__module__]
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    M.db.init_app(app)
    ctx = app.app_context()
    ctx.push()
    M.db.create_all()
    T._feed_trust_cache.clear()
    v = M.Vehicle(name='Test car', api_brand='hyundai')
    M.db.session.add(v)
    M.db.session.commit()
    yield T, M, v
    M.db.session.remove()
    ctx.pop()


def _feed_raw(ort):
    return json.dumps({
        'data': {'Location': {'Date': '20261115122300.000', 'Offset': 1}},
        'location': [ort[0], ort[1]],
        'location_last_updated_at': '2026-11-15T12:23:00+01:00',
        'timezone': 'Europe/Berlin',
    })


def _forced_raw(local_iso, utc_compact, ort):
    return json.dumps({
        'data': {'vehicleLocation': {'time': utc_compact,
                                     'coord': {'lat': ort[0], 'lon': ort[1]}}},
        'location_last_updated_at': local_iso,
    })


def _stop(M, vid, ort, odo, at, departed, label='other', address='Somewhere 1'):
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=at, last_seen_at=at,
                        departed_at=departed, lat=ort[0], lon=ort[1], label=label,
                        address=address, odometer_arrived=odo, odometer_departed=odo,
                        soc_arrived=50, soc_departed=50)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _sync(M, vid, odo, at, ort, stored_fix=None, raw=None):
    s = M.VehicleSync(vehicle_id=vid, timestamp=at, odometer_km=odo, soc_percent=50,
                      location_lat=ort[0], location_lon=ort[1],
                      location_last_updated_at=stored_fix, raw_json=raw)
    M.db.session.add(s)
    M.db.session.commit()
    return s


def _setup_stay(M, v):
    """Stop before (home), then a short stay whose recorded departure lies
    3 min BEFORE the syncs that show the car at its odometer reading."""
    _stop(M, v.id, HOME, 359, datetime(2026, 11, 15, 10, 0), datetime(2026, 11, 15, 11, 57))
    return _stop(M, v.id, (0.0, 0.0), 361, datetime(2026, 11, 15, 12, 4, 5),
                 datetime(2026, 11, 15, 12, 20, 36), label='unknown', address=None)


def test_01_proof_after_the_recorded_departure_counts(env):
    """🔴 The failing case: feed sync at 12:23:23 and the forced refresh at
    12:23:35 — both at the stay's reading, both after its recorded departure."""
    T, M, v = env
    pe = _setup_stay(M, v)
    _sync(M, v.id, 361, datetime(2026, 11, 15, 12, 23, 23), FEED_MID, raw=_feed_raw(FEED_MID))
    # stored the way the old code stored it: the forced feed's UTC clock as local
    _sync(M, v.id, 361, datetime(2026, 11, 15, 12, 23, 35), MID,
          stored_fix=datetime(2026, 11, 15, 11, 23, 33),
          raw=_forced_raw('2026-11-15T12:23:33+01:00', '20261115112333', MID))
    r = T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))
    assert r['healed_fix'] == 1, r
    got = M.db.session.get(M.ParkingEvent, pe.id)
    assert got.label == 'other' and abs(got.lat - MID[0]) < 1e-6 and abs(got.lon - MID[1]) < 1e-6
    assert got.address is None, 'the address is the geocoder\'s job'


def test_02_a_sync_that_shows_the_car_gone_is_not_evidence(env):
    """The odometer moved on: a fix after that describes the NEXT stop, even
    when it sits right behind the recorded departure."""
    T, M, v = env
    pe = _setup_stay(M, v)
    _sync(M, v.id, 363, datetime(2026, 11, 15, 12, 22, 0), OTHER,
          stored_fix=datetime(2026, 11, 15, 12, 21, 58),
          raw=_forced_raw('2026-11-15T12:21:58+01:00', '20261115112158', OTHER))
    # same reading as the stay, but only AFTER the car was seen gone
    _sync(M, v.id, 361, datetime(2026, 11, 15, 12, 23, 35), MID,
          stored_fix=datetime(2026, 11, 15, 12, 23, 33),
          raw=_forced_raw('2026-11-15T12:23:33+01:00', '20261115112333', MID))
    r = T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))
    assert r['healed'] == 0 and r['no_proof'] == 1, r
    assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'


def test_03_the_grace_after_departure_is_bounded(env):
    T, M, v = env
    pe = _setup_stay(M, v)
    late = datetime(2026, 11, 15, 12, 20, 36) + T.HEAL_WINDOW_GRACE + timedelta(minutes=5)
    _sync(M, v.id, 361, late, MID, stored_fix=late,
          raw=_forced_raw(late.strftime('%Y-%m-%dT%H:%M:%S+01:00'),
                          (late - timedelta(hours=1)).strftime('%Y%m%d%H%M%S'), MID))
    r = T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))
    assert r['healed'] == 0, r


def test_04_without_an_odometer_nothing_past_the_departure_counts(env):
    T, M, v = env
    pe = _setup_stay(M, v)
    _sync(M, v.id, None, datetime(2026, 11, 15, 12, 23, 35), MID,
          stored_fix=datetime(2026, 11, 15, 12, 23, 33),
          raw=_forced_raw('2026-11-15T12:23:33+01:00', '20261115112333', MID))
    assert T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))['healed'] == 0


def test_05_a_stay_inside_its_window_still_heals_as_before(env):
    T, M, v = env
    pe = _setup_stay(M, v)
    _sync(M, v.id, 361, datetime(2026, 11, 15, 12, 10, 0), MID,
          stored_fix=datetime(2026, 11, 15, 12, 9, 58),
          raw=_forced_raw('2026-11-15T12:09:58+01:00', '20261115110958', MID))
    assert T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))['healed_fix'] == 1


def test_06_unknown_label_with_a_real_position_gets_label_and_address_slot(env):
    """A stay labelled ``unknown`` that already holds coordinates needs no
    proof — only its label, and an empty address for the geocoder."""
    T, M, v = env
    pe = _stop(M, v.id, MID, 361, datetime(2026, 11, 20, 12, 0),
               datetime(2026, 11, 20, 13, 0), label='unknown', address=None)
    r = T.heal_unknown_stops(now=datetime(2026, 11, 23, 9, 0))
    assert r['healed_positioned'] == 1 and r['healed'] == 1, r
    got = M.db.session.get(M.ParkingEvent, pe.id)
    assert got.label == 'other' and got.address is None
    assert T.count_missing_addresses() == 1


def test_07_geocoder_fills_healed_stops_and_skips_what_it_cannot_resolve(env, monkeypatch):
    """Retry: the window moves past a stop the service cannot resolve."""
    T, M, v = env
    import services.geocode_service as G
    pes = [_stop(M, v.id, (50.0 + i * 0.01, 10.0), 100 + i,
                 datetime(2026, 11, 20, 8 + i, 0), datetime(2026, 11, 20, 8 + i, 30),
                 address=None) for i in range(3)]
    bad = pes[2]                      # newest: sorted first, never resolvable

    def fake(lat, lon, language='de'):
        return None if abs(lat - bad.lat) < 1e-9 else f'Street {lat:.2f}'
    monkeypatch.setattr(G, 'reverse', fake)
    assert T.count_missing_addresses() == 3
    assert T.geocode_missing_events(limit=1) == 0                    # head blocks
    assert T.geocode_missing_events(limit=1, offset=1) == 1          # window moved on
    assert T.geocode_missing_events(limit=5) == 1                    # the rest
    assert T.count_missing_addresses() == 1                          # only the bad one left
    # a later pass retries the bad one once the service knows it
    monkeypatch.setattr(G, 'reverse', lambda lat, lon, language='de': 'Late 1')
    assert T.geocode_missing_events(limit=5) == 1
    assert T.count_missing_addresses() == 0


def test_08_maintenance_loop_retries_stop_addresses():
    """The retry runs in the background loop, not only when the page opens."""
    import inspect
    import services.geocode_service as G
    src = inspect.getsource(G._maintenance_loop)
    assert 'count_missing_addresses' in src and 'geocode_missing_events' in src
