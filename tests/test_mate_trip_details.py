# -*- coding: utf-8 -*-
"""A trip from Leapmotor Mate shows its end, its SoC and its places (v3.0.199).

The first real B10 trip arrived with the right start time and distance, but
the trip details had no end time (so the duration could not be checked), no
SoC and no start/destination. The end was never lost — the tracker stores
start + duration — it was just printed nowhere: the list showed only the
departure, and the details dialog loaded both ends from parking events a
Mate trip does not have. SoC was read from Mate's export and dropped when
filing, the place names were not read at all, and the tooltip over the trip
said it came "direct from the Kia/Hyundai server".

These tests render the real page from a real app with a real (temporary)
database, and boot an install whose trip table predates the new columns.
All data is invented.
"""
import csv
import io
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TZ = timezone(timedelta(hours=1))
TRIP_COLS = ['id', 'vehicle_id', 'started_at', 'ended_at', 'distance_km', 'start_soc',
             'end_soc', 'start_odometer_km', 'end_odometer_km', 'duration_min', 'start_place',
             'end_place']


def _iso(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).isoformat()


def _trips(parse_trips, places=True):
    rows = [dict(id=7, started_at=_iso(2026, 11, 12, 14, 24), ended_at=_iso(2026, 11, 12, 14, 51),
                 distance_km=16, start_soc=82, end_soc=77,
                 start_place='Market Street 3' if places else '',
                 end_place='<b>Lake</b> Road' if places else ''),
            dict(id=9, started_at=_iso(2026, 11, 10, 9, 0), ended_at=_iso(2026, 11, 10, 9, 30),
                 distance_km=20, start_soc=50, end_soc=45, start_place="'=HYPERLINK(1)"),
            # over midnight: the end shows with its date
            dict(id=8, started_at=_iso(2026, 11, 13, 23, 40), ended_at=_iso(2026, 11, 14, 0, 25),
                 distance_km=52.3, start_soc=70, end_soc=61.5)]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRIP_COLS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, '') for c in TRIP_COLS})
    got, _ = parse_trips(list(csv.DictReader(io.StringIO(buf.getvalue()))))
    return got


def _boot(fresh_app_modules, prepare_db=None):
    d = tempfile.mkdtemp(prefix='evct-matetrip-')
    if prepare_db:
        prepare_db(os.path.join(d, 'ev_tracker.db'))
    fresh_app_modules(('app', 'config', 'models', 'services'), EV_DATA_DIR=d,
                      SECRET_KEY=os.environ.get('SECRET_KEY') or 'matetrip-test')
    from app import create_app
    return create_app()


@pytest.fixture
def client(fresh_app_modules):
    app = _boot(fresh_app_modules)
    with app.app_context():
        from models.database import db, Vehicle
        from services.vehicle.connector_leapmotor_mate import parse_trips
        from services.leapmotor_mate_import import import_trips
        v = Vehicle()
        v.name, v.is_archived, v.api_brand = 'B10', False, 'leapmotor_mate'
        db.session.add(v)
        db.session.commit()
        n = import_trips(v.id, _trips(parse_trips))
        assert n['new'] == 3
        vid = v.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['vehicle_filter'] = vid      # the fleet picker, set to this car
    return c


@pytest.fixture
def page(client):
    r = client.get('/trips')
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_the_trip_export_never_hands_a_spreadsheet_a_formula(client):
    from services.charge_sheet import mask_formula
    assert mask_formula('=Home') == "'=Home" and mask_formula('Home') == 'Home'
    out = client.get('/api/trips/export.csv').get_data(as_text=True)
    assert 'Market Street 3' in out
    for line in out.splitlines()[1:]:
        for cell in next(csv.reader([line])):
            assert not cell[:1] in ('=', '+', '@'), line


def _row(html, start):
    m = re.search(r'<tr[^>]*data-start-at="%s[^"]*"[^>]*>(.*?)</tr>' % re.escape(start), html, re.S)
    assert m, start
    return m.group(0)


def test_the_list_shows_end_time_and_duration(page):
    row = _row(page, '2026-11-12T14:24')
    assert 'data-end-at="2026-11-12T14:51' in row
    assert re.search(r'→\s*14:51\s*·\s*27 min', row), row
    # over midnight: the arrival carries its date
    night = _row(page, '2026-11-13T23:40')
    assert re.search(r'→\s*2026-11-14 00:25\s*·\s*45 min', night), night


def test_the_list_shows_soc_and_mates_place_names(page):
    row = _row(page, '2026-11-12T14:24')
    assert '−5%' in row
    assert 'Market Street 3' in row
    # Mate's text is foreign input: escaped, never markup
    assert '&lt;b&gt;Lake&lt;/b&gt; Road' in row and '<b>Lake</b>' not in row
    assert 'data-soc-from="82' in row and 'data-soc-to="77' in row
    # a trip without place names still says so honestly
    night = _row(page, '2026-11-13T23:40')
    assert '−8.5%' in night and 'bi-geo-alt-slash' in night


def test_no_tooltip_over_a_mate_trip_names_another_brand(page):
    for start in ('2026-11-12T14:24', '2026-11-13T23:40'):
        row = _row(page, start)
        assert 'Leapmotor Mate' in row and 'Kia' not in row and 'Hyundai' not in row


def test_the_details_dialog_is_filled_from_the_row():
    html = open(os.path.join(ROOT, 'templates', 'trips.html'), encoding='utf-8').read()
    js = html[html.index('async function openTripEditor'):html.index('function collectSide')]
    # a side without a parking event gets the row's times, place and SoC
    for key in ('rowInfo.startAt', 'rowInfo.endAt', 'rowInfo.fromPlace', 'rowInfo.toPlace',
                'rowInfo.socFrom', 'rowInfo.socTo'):
        assert key in js, key
    for attr in ('startAt', 'endAt', 'fromPlace', 'toPlace', 'socFrom', 'socTo'):
        assert 'row.dataset.' + attr in js, attr
    # sides WITH an event still load it from the server
    assert "fetch('/api/parking_event/'" in js


def _old_trip_table(path):
    """An install from before v3.0.199 — and before the v3.0.109 rebuild,
    the worst case: that rebuild recreates vehicle_trips with a fixed column
    list, so new columns added before it would be gone again."""
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE vehicle_trips (
            id INTEGER NOT NULL PRIMARY KEY, vehicle_id INTEGER,
            trip_date DATE NOT NULL, start_time DATETIME NOT NULL UNIQUE,
            drive_minutes INTEGER, idle_minutes INTEGER, distance_km FLOAT,
            avg_speed_kmh FLOAT, max_speed_kmh INTEGER, source VARCHAR(32),
            fetched_at DATETIME);
        INSERT INTO vehicle_trips (vehicle_id, trip_date, start_time, drive_minutes,
            idle_minutes, distance_km, source)
            VALUES (1, '2026-11-01', '2026-11-01 08:00:00', 20, 0, 12.5, 'sdk_day_trip_info');
    ''')
    con.commit()
    con.close()


def test_an_old_install_gets_the_new_columns_and_keeps_its_trips(fresh_app_modules):
    app = _boot(fresh_app_modules, prepare_db=_old_trip_table)
    with app.app_context():
        from sqlalchemy import inspect
        from models.database import db, VehicleTrip
        cols = {c['name'] for c in inspect(db.engine).get_columns('vehicle_trips')}
        assert {'start_soc', 'end_soc', 'start_place', 'end_place'} <= cols
        rows = VehicleTrip.query.all()
        assert len(rows) == 1 and rows[0].distance_km == 12.5 and rows[0].start_soc is None



def test_a_gps_trip_shows_no_poll_time_as_its_arrival(client):
    """On a trip built from GPS stops the arrival is the first sync at the
    destination — hours late with a sparse sync. It must not read as the end
    of the drive."""
    app = client.application
    with app.app_context():
        from models.database import db, ParkingEvent, Vehicle
        vid = Vehicle.query.filter_by(api_brand='leapmotor_mate').one().id
        db.session.add_all([
            ParkingEvent(vehicle_id=vid, arrived_at=datetime(2026, 11, 5, 7, 0),
                         departed_at=datetime(2026, 11, 5, 13, 0), lat=50.0, lon=10.0,
                         label='other', address='Depot', odometer_departed=1000),
            ParkingEvent(vehicle_id=vid, arrived_at=datetime(2026, 11, 6, 4, 0),
                         lat=50.1, lon=10.1, label='other', address='Harbour',
                         odometer_arrived=1040)])
        db.session.commit()
    html = client.get('/trips').get_data(as_text=True)
    row = _row(html, '2026-11-05T13:00')
    assert 'Depot' in row and 'Harbour' in row
    cells = row.split('>', 1)[1]          # what is printed, not the data attributes
    assert 'trip-arrival' not in cells and '04:00' not in cells
