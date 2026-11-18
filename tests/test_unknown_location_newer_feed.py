# -*- coding: utf-8 -*-
"""Trips must keep their places when the vendor library switches feeds.

In 2026-11 the Kia/Hyundai library (5.x) started reading every car's cached
status from its newer feed. That payload has no ``data.vehicleLocation``
block; the fix time sits in ``Location`` and the library's parsed value at
the top level of the connector's dump. The connector never handed that
parsed value over, and the app's raw fallback only knew the old block — so
every sync was stored without a GPS time. A car that normally sends one and
then does not is, to the parking hook, a cache echo: every drive ended at an
``unknown`` placeholder and the trip log read "Unknown location" from that
day on, while the coordinates sat intact in the very same rows.

The forced refresh that follows each drive did carry a time, but the old
fallback read the forced GPS feed's UTC clock as local time, so it looked one
UTC offset old — stale as well.

Figures and coordinates are invented; only the shape of the payloads is real.

Run with:  python3 -m pytest tests/test_unknown_location_newer_feed.py
"""
import ast
import inspect
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evunk-'))

from flask import Flask                                              # noqa: E402

ORT_A = (50.1010, 10.2020)
ORT_B = (50.3030, 10.4040)
ORT_C = (50.5050, 10.6060)


class _Zone:
    """Set the time zone for one block and restore it afterwards."""

    def __init__(self, name):
        self.name = name
        self.before = os.environ.get('TZ')

    def __enter__(self):
        os.environ['TZ'] = self.name
        time.tzset()
        return self

    def __exit__(self, *_):
        if self.before is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.before
        time.tzset()


def _newer_feed_raw(fix_local_iso, ort):
    """The connector's dump of a sync read from the NEWER cached feed: no
    ``vehicleLocation`` block, the library's parsed fix time on top level."""
    return json.dumps({
        'data': {'Location': {'Date': '20261111063112.000', 'Offset': 1,
                              'TimeStamp': {'Year': 2026, 'Mon': 11, 'Day': 11,
                                            'Hour': 6, 'Min': 31, 'Sec': 12}},
                 'Date': '20261111063200.000'},
        'location': [ort[0], ort[1]],
        'location_last_updated_at': fix_local_iso,
        'timezone': 'Europe/Berlin',
    })


def _forced_feed_raw(fix_local_iso, utc_compact, ort):
    """A forced refresh: the old block is present, but its ``time`` is the
    forced GPS feed's UTC clock. The library already converted it."""
    return json.dumps({
        'data': {'vehicleLocation': {'time': utc_compact,
                                     'coord': {'lat': ort[0], 'lon': ort[1]}}},
        'location_last_updated_at': fix_local_iso,
    })


class _Status:
    """The part of VehicleStatus the extractor reads — as a connector that
    does NOT hand over the fix time builds it."""
    location_last_updated_at = None


def _extract():
    from app import _extract_location_last_updated
    return _extract_location_last_updated


# ── 1. Reading the fix time ───────────────────────────────────────────

def _label_local(dt_naive):
    """A naive Berlin wall-clock time written the way the library labels it."""
    return dt_naive.strftime('%Y-%m-%dT%H:%M:%S') + '+01:00'


def test_01_newer_feed_carries_no_fix_time():
    """The newer feed's position comes with a time that is the cloud's
    contact time (it keeps moving while the car stands) under a wrong zone
    label. It must never pass for a fresh fix — explicitly None, so a
    future library fix of the label cannot turn lagging positions into
    "fresh" ones."""
    with _Zone('Europe/Berlin'):
        now = datetime.now().replace(microsecond=0)
        raw = _newer_feed_raw(_label_local(now - timedelta(minutes=1)), ORT_B)
        assert _extract()(_Status(), raw) is None
        from services.vehicle.gps_time import location_time_from_raw, raw_is_newer_feed
        assert raw_is_newer_feed(raw)
        assert location_time_from_raw(raw) is None


def test_02_forced_feed_is_not_one_utc_offset_old():
    """🔴 The forced refresh after a drive: the old block's ``time`` is UTC.
    Read as local it is one hour old in winter — past the 30-min gate."""
    with _Zone('Europe/Berlin'):
        raw = _forced_feed_raw('2026-11-11T08:32:52+01:00', '20261111073252', ORT_B)
        got = _extract()(_Status(), raw)
        assert got == datetime(2026, 11, 11, 8, 32, 52), got


def test_03_legacy_rows_without_the_dump_keep_working():
    with _Zone('Europe/Berlin'):
        raw = json.dumps({'data': {'vehicleLocation': {'time': '20261110201904'}}})
        assert _extract()(_Status(), raw) == datetime(2026, 11, 10, 20, 19, 4)


def test_04_connector_hands_over_the_library_fix_time():
    """The connector must pass the library's parsed fix time on, so the app
    never depends on digging it out of a payload whose shape it does not
    control."""
    from services.vehicle import connector_hyundai_kia as C
    tree = ast.parse(inspect.getsource(C))
    kws = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'VehicleStatus'):
            kws |= {k.arg for k in node.keywords}
    assert 'location_last_updated_at' in kws, sorted(kws)
    from services.vehicle.base import VehicleStatus
    assert 'location_last_updated_at' in VehicleStatus.__dataclass_fields__
    src = inspect.getsource(C)
    assert 'is_newer_feed(' in src, 'the newer feed must not hand over its clock'


# ── 2. The trip log end to end ────────────────────────────────────────

def _m():
    import services.trips_service as T
    return sys.modules[T.ParkingEvent.__module__]


def _app():
    sys.modules.pop('services.trips_service', None)
    M = _m()
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    M.db.init_app(app)
    ctx = app.app_context()
    ctx.push()
    M.db.create_all()
    return app, ctx


def _car():
    M = _m()
    v = M.Vehicle(name='Test car', api_brand='hyundai')
    M.db.session.add(v)
    M.db.session.commit()
    return v


def _stop(vid, ort, odo, at, label='other', departed=None):
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=at, last_seen_at=at,
                        departed_at=departed, lat=ort[0], lon=ort[1],
                        label=label, odometer_arrived=odo,
                        odometer_departed=odo, soc_arrived=50, soc_departed=50)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _placeholder(vid, odo, at, departed=None):
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=at, last_seen_at=at,
                        departed_at=departed, lat=0.0, lon=0.0, label='unknown',
                        odometer_arrived=odo, odometer_departed=odo,
                        soc_arrived=50, soc_departed=50)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _sync(vid, odo, at, ort, stored_fix=None, raw=None):
    M = _m()
    s = M.VehicleSync(vehicle_id=vid, timestamp=at, odometer_km=odo,
                      soc_percent=50, location_lat=ort[0], location_lon=ort[1],
                      location_last_updated_at=stored_fix, raw_json=raw)
    M.db.session.add(s)
    M.db.session.commit()
    return s


def test_05_a_drive_on_the_newer_feed_ends_at_a_real_place():
    """🔴 End to end, the way the live poll sees it: the first sync after a
    drive comes from the newer feed (no usable time → placeholder), the
    forced refresh 20 s later carries the real fix. Stored the way the sync
    path stores it, that fix must put the place on the stop — before
    v3.0.196 it was read one UTC offset old and the stop stayed unknown."""
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import update_parking_from_sync
            M = _m()
            v = _car()
            now = datetime.now().replace(microsecond=0)
            t0 = now - timedelta(hours=12)
            _stop(v.id, ORT_A, 1000, t0)
            _sync(v.id, 1000, t0 + timedelta(minutes=8), ORT_A, stored_fix=t0)
            at = now - timedelta(seconds=20)
            raw1 = _newer_feed_raw(_label_local(at - timedelta(minutes=64)), ORT_B)
            update_parking_from_sync(_sync(v.id, 1005, at, ORT_B,
                                           stored_fix=_extract()(_Status(), raw1),
                                           raw=raw1))
            fix = now - timedelta(seconds=2)
            utc = (fix - timedelta(hours=1)).strftime('%Y%m%d%H%M%S')
            raw2 = _forced_feed_raw(_label_local(fix), utc, ORT_B)
            update_parking_from_sync(_sync(v.id, 1005, now, ORT_B,
                                           stored_fix=_extract()(_Status(), raw2),
                                           raw=raw2))
            newest = M.ParkingEvent.query.order_by(M.ParkingEvent.arrived_at.desc()).first()
            assert newest.label != 'unknown', 'the drive ended at "Unknown location" again'
            assert abs(newest.lat - ORT_B[0]) < 1e-6
        finally:
            ctx.pop()


# ── 3. Healing what the regression already filed ──────────────────────

def test_06_heal_puts_the_proven_place_on_a_placeholder():
    """🔴 The rows written during the regression: placeholder + a forced
    refresh in the same stay whose stored time is one UTC offset old, while
    the raw dump holds the true time. The heal must label it, once."""
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            prev = _stop(v.id, ORT_A, 1000, datetime(2026, 11, 11, 7, 0),
                         departed=datetime(2026, 11, 11, 8, 15))
            at = datetime(2026, 11, 11, 8, 32, 36)
            pe = _placeholder(v.id, 1005, at, departed=datetime(2026, 11, 11, 14, 5))
            forced = at + timedelta(seconds=18)
            _sync(v.id, 1005, forced, ORT_B,
                  stored_fix=forced - timedelta(hours=1),
                  raw=_forced_feed_raw('2026-11-11T08:32:52+01:00',
                                       '20261111073252', ORT_B))
            now = datetime(2026, 11, 18, 12, 0)
            r = heal_unknown_stops(now=now)
            assert r['healed'] == 1, r
            got = M.db.session.get(M.ParkingEvent, pe.id)
            assert got.label == 'other' and abs(got.lat - ORT_B[0]) < 1e-6
            assert got.address is None, 'the geocoder fills the address'
            assert got.arrived_at == at, 'the arrival anchor must not move'
            assert M.db.session.get(M.ParkingEvent, prev.id).label == 'other'
            again = heal_unknown_stops(now=now)
            assert again['healed'] == 0 and again['checked'] == 0, again
        finally:
            ctx.pop()


def _feed_history(vid, start, n, agree=True):
    """``n`` drives, each: a newer-feed poll right after arrival, the forced
    refresh 20 s later. ``agree`` decides whether both report one place."""
    for i in range(n):
        t = start + timedelta(hours=3 * i)
        odo = 9000 + 10 * i
        here = (49.0 + i * 0.01, 9.0)
        feed_pos = here if agree else (here[0] + 0.05, here[1])
        _sync(vid, odo, t, feed_pos, raw=_newer_feed_raw('2026-11-01T00:00:00+01:00', feed_pos))
        f = t + timedelta(seconds=20)
        _sync(vid, odo, f, here,
              raw=_forced_feed_raw(_label_local(f), '20261101000000', here))


def test_06b_newer_feed_alone_is_no_proof_for_an_unproven_car():
    """A stay with only newer-feed polls whose position never moves: for a
    car whose feed has not proven itself, that may be the previous spot."""
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            _feed_history(v.id, datetime(2026, 11, 2, 8, 0), 12, agree=False)
            at = datetime(2026, 11, 12, 10, 1, 59)
            pe = _placeholder(v.id, 6212, at, departed=datetime(2026, 11, 12, 10, 15))
            _sync(v.id, 6212, at, ORT_C, raw=_newer_feed_raw('2026-11-12T08:59:40+01:00', ORT_C))
            r = heal_unknown_stops(now=datetime(2026, 11, 18, 12, 0))
            assert r['healed'] == 0 and r['no_proof'] == 1, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_06c_a_proven_feed_heals_the_stay():
    """The same stay on a car whose feed matched its fresh fixes: healed."""
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            import services.trips_service as T
            T._feed_trust_cache.clear()
            M = _m()
            v = _car()
            _feed_history(v.id, datetime(2026, 11, 2, 8, 0), 12, agree=True)
            assert T.newer_feed_trusted(v.id, datetime(2026, 11, 18, 12, 0))[0]
            _stop(v.id, ORT_A, 6180, datetime(2026, 11, 12, 8, 0),
                  departed=datetime(2026, 11, 12, 9, 30))
            at = datetime(2026, 11, 12, 10, 1, 59)
            pe = _placeholder(v.id, 6212, at, departed=datetime(2026, 11, 12, 10, 15))
            _sync(v.id, 6212, at, ORT_C, raw=_newer_feed_raw('2026-11-12T08:59:40+01:00', ORT_C))
            r = T.heal_unknown_stops(now=datetime(2026, 11, 18, 12, 0))
            assert r['healed_trusted_feed'] == 1, r
            got = M.db.session.get(M.ParkingEvent, pe.id)
            assert got.label == 'other' and abs(got.lat - ORT_C[0]) < 1e-6
        finally:
            ctx.pop()


def test_06d_a_proven_feed_echoing_the_stop_before_is_refused():
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            import services.trips_service as T
            T._feed_trust_cache.clear()
            M = _m()
            v = _car()
            _feed_history(v.id, datetime(2026, 11, 2, 8, 0), 12, agree=True)
            _stop(v.id, ORT_A, 6180, datetime(2026, 11, 12, 8, 0),
                  departed=datetime(2026, 11, 12, 9, 30))
            at = datetime(2026, 11, 12, 10, 1, 59)
            pe = _placeholder(v.id, 6212, at, departed=datetime(2026, 11, 12, 10, 15))
            _sync(v.id, 6212, at, ORT_A, raw=_newer_feed_raw('2026-11-12T08:59:40+01:00', ORT_A))
            r = T.heal_unknown_stops(now=datetime(2026, 11, 18, 12, 0))
            assert r['healed'] == 0 and r['echo'] == 1, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_06e_an_update_observed_during_the_stay_is_proof():
    """The feed lagged at the previous spot, then moved while the odometer
    stood still: the cloud caught up with this stay. Any car."""
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            _stop(v.id, ORT_A, 7000, datetime(2026, 11, 12, 8, 0),
                  departed=datetime(2026, 11, 12, 9, 30))
            at = datetime(2026, 11, 12, 9, 52)
            pe = _placeholder(v.id, 7020, at, departed=datetime(2026, 11, 12, 18, 0))
            for i, ort in enumerate((ORT_A, ORT_A, ORT_B, ORT_B)):
                t = at + timedelta(minutes=40 * i)
                _sync(v.id, 7020, t, ort, raw=_newer_feed_raw('2026-11-12T00:00:00+01:00', ort))
            r = heal_unknown_stops(now=datetime(2026, 11, 18, 12, 0))
            assert r['healed_update'] == 1, r
            got = M.db.session.get(M.ParkingEvent, pe.id)
            assert abs(got.lat - ORT_B[0]) < 1e-6
        finally:
            ctx.pop()


def test_07_a_fix_after_the_car_moved_on_does_not_count():
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            at = datetime(2026, 11, 12, 9, 0)
            pe = _placeholder(v.id, 2000, at)
            later = at + timedelta(hours=2)
            _sync(v.id, 2012, later, ORT_C,
                  raw=_forced_feed_raw('2026-11-12T11:00:00+01:00',
                                       '20261112100000', ORT_C))
            r = heal_unknown_stops(now=datetime(2026, 11, 12, 12, 0))
            assert r['healed'] == 0 and r['no_proof'] == 1, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_08_a_stale_fix_does_not_count():
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            at = datetime(2026, 11, 13, 7, 41)
            pe = _placeholder(v.id, 3000, at)
            t = at + timedelta(minutes=10)
            _sync(v.id, 3000, t, ORT_B,
                  raw=_forced_feed_raw('2026-11-13T05:00:00+01:00', '20261113040000', ORT_B))
            r = heal_unknown_stops(now=datetime(2026, 11, 13, 12, 0))
            assert r['healed'] == 0, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_09_disagreeing_fresh_fixes_keep_the_placeholder():
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            at = datetime(2026, 11, 14, 10, 0)
            pe = _placeholder(v.id, 4000, at)
            for i, ort in enumerate((ORT_B, ORT_C)):
                t = at + timedelta(minutes=5 + 10 * i)
                _sync(v.id, 4000, t, ort,
                      raw=_forced_feed_raw(t.strftime('%Y-%m-%dT%H:%M:%S+01:00'),
                                           '20261114000000', ort))
            r = heal_unknown_stops(now=datetime(2026, 11, 14, 12, 0))
            assert r['disagree'] == 1 and r['healed'] == 0, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_10_an_echo_of_the_previous_stop_keeps_the_placeholder():
    with _Zone('Europe/Berlin'):
        app, ctx = _app()
        try:
            from services.trips_service import heal_unknown_stops
            M = _m()
            v = _car()
            _stop(v.id, ORT_A, 5000, datetime(2026, 11, 15, 7, 0),
                  departed=datetime(2026, 11, 15, 8, 0))
            at = datetime(2026, 11, 15, 8, 30)
            pe = _placeholder(v.id, 5020, at)
            t = at + timedelta(minutes=1)
            _sync(v.id, 5020, t, ORT_A,
                  raw=_forced_feed_raw(t.strftime('%Y-%m-%dT%H:%M:%S+01:00'),
                                       '20261115073100', ORT_A))
            r = heal_unknown_stops(now=datetime(2026, 11, 15, 12, 0))
            assert r['echo'] == 1 and r['healed'] == 0, r
            assert M.db.session.get(M.ParkingEvent, pe.id).label == 'unknown'
        finally:
            ctx.pop()


def test_11_heal_runs_on_boot_and_after_every_sync():
    """Self-heal, not a one-off: wired into boot and into the sync path."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert src.count('heal_unknown_stops(') >= 2, 'boot + sync path'
    assert "heal_unknown_stops(vehicle_id=vehicle_id, days=3)" in src
