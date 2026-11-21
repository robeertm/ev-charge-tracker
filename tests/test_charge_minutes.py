# -*- coding: utf-8 -*-
"""A charge keeps the minute of its start and end, not only the hour (v3.0.200).

Reported on the Leapmotor (Mate) integration: a charge that ran 13:05-13:52
showed as 13:00 / 13:00. It is not Mate-specific — the charge row only had
whole hours (the CO2 figure is calculated per grid hour), so every source and
the hand-typed form lost the minutes. v3.0.197 only copied the real times
into the note. The row now has two more, nullable columns; the hours stay as
they were, so the CO2 and matching logic is untouched.

These tests run the real app against a real (temporary) database. All data is
invented.
"""
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date, datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')


def _boot(fresh_app_modules, prepare_db=None):
    d = tempfile.mkdtemp(prefix='evct-minutes-')
    if prepare_db:
        prepare_db(os.path.join(d, 'ev_tracker.db'))
    fresh_app_modules(('app', 'config', 'models', 'services'), EV_DATA_DIR=d,
                      SECRET_KEY=os.environ.get('SECRET_KEY') or 'minutes-test')
    from app import create_app
    return create_app()


@pytest.fixture
def app(fresh_app_modules):
    a = _boot(fresh_app_modules)
    with a.app_context():
        yield a


def _epoch(h, m, day=16):
    return int(datetime(2026, 11, day, h, m).timestamp())


def _session(start, end, **kw):
    s = dict(id=17, start=start, end=end, kwh=11.4, kwh_source='charger', type='AC',
             soc_from=30, soc_to=55, lat=None, lon=None, odometer=12000)
    s.update(kw)
    return s


def _first_vehicle_id():
    from models.database import Vehicle
    return Vehicle.query.order_by(Vehicle.id.asc()).first().id


# ── storage ───────────────────────────────────────────────────────────────

def test_a_mate_charge_keeps_its_minutes(app):
    from models.database import Charge
    from services.leapmotor_mate_import import import_sessions
    import_sessions(_first_vehicle_id(), [_session(_epoch(13, 5), _epoch(13, 52))], 67.0, 0.9)
    c = Charge.query.one()
    assert (c.charge_hour, c.charge_start_min) == (13, 5)
    assert (c.charge_end_hour, c.charge_end_min) == (13, 52)
    d = c.to_dict()
    assert d['start_clock'] == '13:05' and d['end_clock'] == '13:52'


def test_a_mate_charge_over_midnight_keeps_both_ends(app):
    from models.database import Charge
    from services.leapmotor_mate_import import import_sessions
    import_sessions(_first_vehicle_id(), [_session(_epoch(23, 40), _epoch(2, 7, day=17))], 67.0, 0.9)
    c = Charge.query.one()
    assert (c.charge_hour, c.charge_start_min, c.charge_end_hour, c.charge_end_min) == (23, 40, 2, 7)


def test_an_updated_mate_charge_takes_the_new_minutes(app):
    from models.database import db, Charge
    from services.leapmotor_mate_import import import_sessions
    vid = _first_vehicle_id()
    import_sessions(vid, [_session(_epoch(13, 5), _epoch(13, 20))], 67.0, 0.9)
    n = import_sessions(vid, [_session(_epoch(13, 5), _epoch(13, 52))], 67.0, 0.9)
    assert n['updated'] == 1
    assert Charge.query.one().charge_end_min == 52


def test_a_wallbox_reading_filed_as_a_charge_keeps_its_minutes(app):
    """The wallbox import (a home-meter reading becomes a charge) read only
    ``.hour`` off the start and end timestamps."""
    import inspect
    from services import shelly_link
    src = inspect.getsource(shelly_link)
    assert 'charge_start_min=ws.minute' in src and 'charge_end_min=we.minute' in src


# ── an install from before the columns ────────────────────────────────────

def _old_charges_table(path):
    """The charge table of v3.0.199: every column of today's model except the
    two new ones."""
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite
    from models.database import Charge
    ddl = str(CreateTable(Charge.__table__).compile(dialect=sqlite.dialect()))
    con = sqlite3.connect(path)
    con.executescript(ddl)
    con.execute('ALTER TABLE charges DROP COLUMN charge_start_min')
    con.execute('ALTER TABLE charges DROP COLUMN charge_end_min')
    s, e = _epoch(13, 5), _epoch(13, 52)
    con.execute("INSERT INTO charges (date, charge_hour, charge_end_hour, kwh_loaded, notes) "
                "VALUES ('2026-11-16', 13, 13, 11.4, ?)",
                ('leapmotor-mate:17@%d-%d · 13:05–13:52' % (s, e),))
    con.execute("INSERT INTO charges (date, charge_hour, charge_end_hour, kwh_loaded, notes) "
                "VALUES ('2026-11-17', 20, 22, 9.0, 'typed by hand')")
    con.commit()
    con.close()


def test_an_old_install_gets_the_columns_and_reads_the_minutes_from_the_note(fresh_app_modules):
    a = _boot(fresh_app_modules, prepare_db=_old_charges_table)
    with a.app_context():
        from sqlalchemy import inspect
        from models.database import db, Charge
        cols = {c['name'] for c in inspect(db.engine).get_columns('charges')}
        assert {'charge_start_min', 'charge_end_min'} <= cols
        mate = Charge.query.filter(Charge.notes.like('leapmotor-mate:%')).one()
        assert (mate.charge_start_min, mate.charge_end_min) == (5, 52)
        hand = Charge.query.filter_by(notes='typed by hand').one()
        assert hand.charge_start_min is None and hand.charge_end_min is None
        # shown as :00 — exactly what it always was
        assert hand.to_dict()['start_clock'] == '20:00'


# ── the forms ─────────────────────────────────────────────────────────────

def _login_session(client):
    from models.database import Vehicle
    with client.session_transaction() as s:
        s['vehicle_filter'] = Vehicle.query.order_by(Vehicle.id.asc()).first().id


def _post_charge(client, **over):
    data = {'date': '2026-11-16', 'charge_hour': '13', 'charge_start_min': '5',
            'charge_end_hour': '13', 'charge_end_min': '52', 'odometer': '12000',
            'kwh_loaded': '11.4', 'charge_type': 'AC', 'soc_from': '30', 'soc_to': '55',
            'eur_per_kwh': '0.30'}
    data.update(over)
    return client.post('/input', data=data, follow_redirects=False)


def test_the_input_form_stores_the_minutes(app):
    from models.database import Charge
    c = app.test_client()
    _login_session(c)
    r = _post_charge(c)
    assert r.status_code in (200, 302), r.status_code
    row = Charge.query.filter_by(date=date(2026, 11, 16)).one()
    assert (row.charge_hour, row.charge_start_min) == (13, 5)
    assert (row.charge_end_hour, row.charge_end_min) == (13, 52)


def test_a_cleared_hour_leaves_no_stale_minute(app):
    from models.database import Charge
    c = app.test_client()
    _login_session(c)
    _post_charge(c, charge_hour='', charge_start_min='45', charge_end_hour='', charge_end_min='30')
    row = Charge.query.filter_by(date=date(2026, 11, 16)).one()
    assert row.charge_hour is None and row.charge_start_min is None
    assert row.charge_end_hour is None and row.charge_end_min is None


def test_a_junk_minute_is_dropped_not_stored(app):
    from models.database import Charge
    c = app.test_client()
    _login_session(c)
    _post_charge(c, charge_start_min='75', charge_end_min='abc')
    row = Charge.query.filter_by(date=date(2026, 11, 16)).one()
    assert row.charge_hour == 13 and row.charge_start_min is None and row.charge_end_min is None


def test_the_edit_page_shows_the_stored_minute_selected(app):
    from models.database import db, Charge
    c = app.test_client()
    _login_session(c)
    _post_charge(c)
    cid = Charge.query.one().id
    html = c.get('/edit/%d' % cid).get_data(as_text=True)
    assert re.search(r'name="charge_start_min".*?<option value="5" selected>05</option>', html, re.S)
    assert re.search(r'name="charge_end_min".*?<option value="52" selected>52</option>', html, re.S)
    # edit and save: the minute moves
    r = c.post('/edit/%d' % cid, data={
        'date': '2026-11-16', 'charge_hour': '13', 'charge_start_min': '10',
        'charge_end_hour': '14', 'charge_end_min': '3', 'odometer': '12000',
        'kwh_loaded': '11.4', 'charge_type': 'AC', 'soc_from': '30', 'soc_to': '55',
        'eur_per_kwh': '0.30'})
    assert r.status_code in (200, 302)
    row = db.session.get(Charge, cid)
    assert (row.charge_start_min, row.charge_end_hour, row.charge_end_min) == (10, 14, 3)


def test_the_charge_api_and_the_pages_carry_the_clock_time(app):
    from models.database import Charge
    c = app.test_client()
    _login_session(c)
    _post_charge(c)
    cid = Charge.query.one().id
    d = c.get('/api/charges/%d' % cid).get_json()
    assert d['charge_start_min'] == 5 and d['charge_end_min'] == 52
    assert d['start_clock'] == '13:05' and d['end_clock'] == '13:52'
    # the new-charge form and the history dialog offer a minute next to every hour
    assert 'name="charge_start_min"' in c.get('/input').get_data(as_text=True)
    hist = c.get('/history').get_data(as_text=True)
    assert 'id="chgEditStartMin"' in hist and 'id="chgEditEndMin"' in hist
    assert 'F.startMin.value' in hist


def test_the_read_only_api_carries_the_minutes(app):
    from models.database import Charge
    from services import readonly_api
    c = Charge(vehicle_id=_first_vehicle_id(), date=date(2026, 11, 16), charge_hour=23,
               charge_start_min=40, charge_end_hour=2, charge_end_min=7, kwh_loaded=9.0)
    start, end = readonly_api._charge_times(c)
    assert start[11:16] == '23:40'
    assert end[:10] == '2026-11-17' and end[11:16] == '02:07'


# ── the automatic detection ───────────────────────────────────────────────

def test_an_auto_detected_charge_keeps_the_minutes_of_its_syncs(app):
    import app as A
    from models.database import db, Vehicle, VehicleSync, Charge
    v = Vehicle.query.order_by(Vehicle.id.asc()).first()
    v.battery_kwh, v.is_archived = 58.0, False
    db.session.commit()

    def sync(when, soc, odo, charging=False):
        s = VehicleSync(vehicle_id=v.id, timestamp=datetime.strptime(when, '%Y-%m-%d %H:%M'),
                        soc_percent=soc, odometer_km=odo, is_charging=charging)
        db.session.add(s)
        db.session.commit()
        return s
    sync('2026-11-16 12:58', 40, 5000)
    sync('2026-11-16 13:05', 45, 5000, True)
    sync('2026-11-16 13:30', 60, 5000, True)
    end = sync('2026-11-16 13:52', 78, 5000)
    A._detect_auto_charge(end)
    rows = Charge.query.all()
    assert len(rows) == 1, rows
    c = rows[0]
    assert (c.charge_hour, c.charge_end_hour) == (13, 13)
    assert c.charge_start_min is not None and c.charge_end_min == 52
    assert c.charge_start_min in (5, 58)       # first charging row / row before it


# ── the spreadsheet ───────────────────────────────────────────────────────

def test_the_sheet_round_trips_the_minutes(app):
    from models.database import db, Charge
    from services import charge_sheet
    assert 'charge_start_min' in charge_sheet.COLUMN_NAMES and 'charge_end_min' in charge_sheet.COLUMN_NAMES
    db.session.add(Charge(vehicle_id=_first_vehicle_id(), date=date(2026, 11, 16), charge_hour=13,
                          charge_start_min=5, charge_end_hour=13, charge_end_min=52,
                          kwh_loaded=11.4, charge_type='AC'))
    db.session.commit()
    rows = charge_sheet.query_charges()
    table = charge_sheet.export_rows(rows)
    r = dict(zip(table[0], table[1]))
    assert str(r['charge_start_min']) == '5' and str(r['charge_end_min']) == '52'


def test_the_sheet_range_check_covers_the_minutes():
    from services import charge_sheet
    assert charge_sheet._RANGES['charge_start_min'] == (0, 59)
    assert charge_sheet._RANGES['charge_end_min'] == (0, 59)


def test_the_google_sheet_import_reads_the_minute_of_a_time():
    import import_gsheet as G
    assert G._parse_minute('14:30') == 30
    assert G._parse_minute('14') is None          # no minute given: not known
    assert G._parse_minute('14:75') is None
    assert G._parse_hour('14:30') == 14


# ── all languages, no leftover ":00" labels ───────────────────────────────

def test_the_minute_label_exists_in_every_language():
    import json
    for lang in LANGS:
        tr = json.load(open(os.path.join(ROOT, 'translations', lang + '.json'), encoding='utf-8'))
        assert tr.get('input.minute'), lang


def test_no_hour_select_still_claims_a_whole_hour():
    for name in ('input.html', 'edit.html', '_history_section.html'):
        html = open(os.path.join(ROOT, 'templates', name), encoding='utf-8').read()
        assert ':00</option>' not in html, name
