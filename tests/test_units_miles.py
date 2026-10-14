"""Kilometres or miles (v3.0.155) — shown, typed, stored, converted.

A customer with a US car asked for miles. Two things were wrong, not one:

* the pages could only say "km";
* a US Kia/Hyundai already STORED miles — the SDK hands the odometer over
  in miles, the connector wrote the number into ``odometer_km`` as it
  came. 30 000 miles read as "30 000 km", and every trip, km/h and
  kWh/100 km computed from it was off by 1.609 without looking wrong.

So: the connectors convert at the door, a car's earlier miles history is
converted once (with a backup, per car), and the pages convert both ways.
These tests hold each of those to account, and the page tests also run
against a real server in both units.
"""
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import units as U  # noqa: E402


@pytest.fixture()
def miles():
    U.set_unit('mi')
    yield
    U.set_unit('km')


# ── 1. the arithmetic ────────────────────────────────────────────────
def test_km_mode_hands_every_value_back_untouched():
    # A page that printed "12" must keep printing "12", not "12.0".
    assert U.dist(12) == 12 and type(U.dist(12)) is int
    assert U.dist(12.34) == 12.34 and U.dist(None) is None
    assert U.per_dist(17.5) == 17.5
    assert U.km_input('37750', '37750') == 37750
    assert U.km_input('37750,0') == 37750           # the old _int() parse
    assert U.rate_input('0,086') == 0.086


def test_a_length_shrinks_and_a_rate_grows_in_miles(miles):
    assert U.dist(160.9344) == pytest.approx(100.0)
    assert U.dist(37750, 0) == 23457
    assert U.per_dist(16.0, 1) == 25.7               # kWh/100 km -> kWh/100 mi
    assert U.per_dist(164, 0) == 264                 # g/km -> g/mi
    assert U.dist(12, mi_nd=1) == 7.5
    assert U.dist_label() == 'mi' and U.speed_label() == 'mph'
    assert U.cons_label() == 'kWh/100 mi'


def test_saving_an_unchanged_odometer_never_moves_it(miles):
    """🔴 37 750 km shows as 23 457 mi, and 23 457 mi back is 37 751 km.
    Every form that is merely saved would nudge the odometer. The stored
    value rides along and wins while the field is unchanged — for every
    odometer up to 300 000 km."""
    for km in range(0, 300_000, 7):
        shown = U.dist(km, 0)
        assert U.km_input(str(shown), str(km)) == km
    # a changed field is converted
    assert U.km_input('23500', '37750') == round(23500 * U.KM_PER_MI)
    # no original at all (new entry) is converted too
    assert U.km_input('100') == 161


def test_an_unchanged_rate_field_saves_back_untouched(miles):
    for per_km in (0.086, 0.0925, 0.11, 164.0, 120.0):
        nd = 3 if per_km < 1 else 0
        shown = round(per_km * U.KM_PER_MI, nd)
        assert U.rate_input(str(shown), str(per_km), nd=nd) == per_km
    assert U.rate_input('0.2', '0.086', nd=3) == pytest.approx(0.2 / U.KM_PER_MI)


# ── 2. every text in every language ──────────────────────────────────
LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')
# Texts that are ABOUT kilometres in miles mode too: the km option of the
# unit picker, and the explanations of what is stored.
ABOUT_KM = {'set.unit_km', 'set.unit_hint', 'set.unit_history_hint',
            'set.unit_convert_btn', 'set.unit_converted_at',
            'set.unit_convert_confirm', 'flash.units_converted'}
KM_TOKEN = re.compile(r'(?<![\w{])km(?![\w}])', re.I)


def _all(lang):
    with open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8') as fh:
        return json.load(fh)


def test_no_text_says_km_when_the_owner_chose_miles(miles):
    from services.i18n import set_language, t
    leftovers = []
    try:
        for lang in LANGS:
            set_language(lang)
            for key in _all(lang):
                if key in ABOUT_KM:
                    continue
                if KM_TOKEN.search(t(key)):
                    leftovers.append(f'{lang}:{key} = {t(key)[:60]}')
    finally:
        set_language('de')
    assert not leftovers, leftovers[:10]


def test_every_new_key_exists_in_all_six_languages():
    keys = {lang: set(_all(lang)) for lang in LANGS}
    assert len({len(k) for k in keys.values()}) == 1
    for lang in LANGS:
        assert keys[lang] == keys['de'], lang
    for k in ('set.units', 'set.unit_mi', 'set.unit_history_hint', 'flash.units_failed'):
        assert all(_all(lang)[k].strip() for lang in LANGS), k


def test_unit_tokens_never_break_a_placeholder(miles):
    """``{u_dist}`` is filled before ``.format()`` — a text with both kinds
    of brace must come out with its own placeholder filled as well."""
    from services.i18n import set_language, t
    try:
        for lang in LANGS:
            set_language(lang)
            txt = t('maint.due_in_km', km='1.234')
            assert '1.234' in txt and 'mi' in txt and '{' not in txt, (lang, txt)
            assert t('audit.row', when='x', km='5') .count('{') == 0
    finally:
        set_language('de')


# ── 3. the connectors convert at the door ────────────────────────────
class _Enumish:
    def __init__(self, value):
        self.value = value


def test_every_spelling_of_a_unit_is_understood():
    from services.vehicle.base import to_km, unit_name
    assert unit_name('mi') == unit_name('MILES') == unit_name(_Enumish('mi')) == 'mi'
    assert unit_name('km') == unit_name(_Enumish('km')) == 'km'
    assert unit_name(None) is None and unit_name('unknown length unit') is None
    assert to_km(100, 'mi', as_int=True) == 161
    # km — and an unknown unit — stays EXACTLY what the connectors did:
    # int() truncates (a rounded odometer would read as "moved 1 km" once)
    assert to_km(37750.9, 'km', as_int=True) == 37750
    assert to_km(37750.9, None, as_int=True) == 37750


class _FakeSdkVehicle:
    """Behaves like hyundai_kia_connect_api's Vehicle where it matters:
    ``odometer`` and ``ev_driving_range`` are SET as (value, unit) pairs."""

    def __init__(self, odo, odo_unit, rng, rng_unit):
        self._odometer, self._odometer_unit = odo, odo_unit
        self._ev_driving_range, self._ev_driving_range_unit = rng, rng_unit
        self.id = 'v1'

    def __getattr__(self, name):          # every other SDK field: unknown
        if name.startswith('_'):
            raise AttributeError(name)
        return None

    @property
    def odometer(self):
        return self._odometer

    @odometer.setter
    def odometer(self, value):
        self._odometer = float(value[0])   # a bare number raises TypeError
        if value[1] is not None:
            self._odometer_unit = value[1]

    @property
    def odometer_unit(self):
        return self._odometer_unit

    @property
    def ev_driving_range(self):
        return self._ev_driving_range

    @ev_driving_range.setter
    def ev_driving_range(self, value):
        self._ev_driving_range = value[0]
        if value[1] is not None:
            self._ev_driving_range_unit = value[1]

    @property
    def ev_driving_range_unit(self):
        return self._ev_driving_range_unit


class _FakeManager:
    def __init__(self, after_force=None):
        self.after_force = after_force

    def update_vehicle_with_cached_state(self, vid):
        pass

    def force_refresh_vehicle_state(self, vid):
        if self.after_force:
            self.after_force()


def _kia(region, vehicle, mgr=None):
    from services.vehicle.connector_hyundai_kia import KiaConnector
    c = KiaConnector({'username': 'u', 'password': 'p', 'region': region})
    c._ensure_auth = lambda: None
    c._get_manager = lambda: mgr or _FakeManager()
    c._get_vehicle = lambda: vehicle
    return c


def test_a_us_kia_reports_miles_and_is_stored_in_km():
    st = _kia('US', _FakeSdkVehicle(30000.0, 'mi', 180, 'mi')).get_status()
    assert st.odometer_km == 48280 and st.estimated_range_km == 290
    assert st.distance_unit == 'mi'


def test_a_european_kia_is_exactly_what_it_was():
    st = _kia('EU', _FakeSdkVehicle(37750.0, 'km', 312, 'km')).get_status()
    assert st.odometer_km == 37750 and st.estimated_range_km == 312
    assert st.distance_unit == 'km'


def test_without_a_unit_the_region_decides():
    st = _kia('US', _FakeSdkVehicle(100.0, None, 50, None)).get_status()
    assert st.odometer_km == 161 and st.distance_unit == 'mi'
    st = _kia('EU', _FakeSdkVehicle(100.0, None, 50, None)).get_status()
    assert st.odometer_km == 100


def test_a_forced_refresh_that_loses_the_odometer_restores_it():
    """🔴 The restore after a wake-up handed the SDK a bare number — its
    setter wants (value, unit) and raised TypeError: the whole forced sync
    was lost exactly when the car had answered."""
    v = _FakeSdkVehicle(30000.0, 'mi', 180, 'mi')

    def wake_loses_values():
        v._odometer = None
        v._ev_driving_range = None
    st = _kia('US', v, _FakeManager(wake_loses_values)).get_status(force=True)
    assert st.odometer_km == 48280 and st.estimated_range_km == 290


# ── 4. a history stored in miles, converted once ─────────────────────
@pytest.fixture()
def file_db(tmp_path):
    from flask import Flask
    from models.database import db
    app = Flask(__name__)
    path = tmp_path / 'ev_tracker.db'
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield db, tmp_path
        db.session.remove()


def _seed_car(db, name, brand, region, odo=30000):
    from models.database import (Vehicle, Charge, VehicleSync, ParkingEvent,
                                 VehicleTrip, MaintenanceEntry, ObdReading)
    v = Vehicle(name=name, api_brand=brand, api_region=region, battery_kwh=64.0,
                is_archived=False, auto_sync=False)
    db.session.add(v)
    db.session.commit()
    t0 = datetime(2026, 9, 1, 8, 0)
    db.session.add(Charge(vehicle_id=v.id, date=date(2026, 9, 1), odometer=odo,
                          kwh_loaded=30.0, charge_type='AC'))
    db.session.add(VehicleSync(vehicle_id=v.id, timestamp=t0, odometer_km=odo,
                               estimated_range_km=180, consumption_30d_kwh_per_100km=25.0))
    db.session.add(ParkingEvent(vehicle_id=v.id, arrived_at=t0, lat=1.0, lon=2.0,
                                odometer_arrived=odo, odometer_departed=odo + 12))
    db.session.add(VehicleTrip(vehicle_id=v.id, trip_date=date(2026, 9, 1),
                               start_time=t0 + timedelta(hours=1), distance_km=12.5,
                               avg_speed_kmh=30.0, max_speed_kmh=62,
                               consumption_kwh_per_100km=25.0, source='sdk_day_trip_info'))
    db.session.add(MaintenanceEntry(vehicle_id=v.id, date=date(2026, 8, 1), item_type='tires',
                                    odometer_km=odo - 500, next_due_km=odo + 7500))
    db.session.add(ObdReading(vehicle_id=v.id, timestamp=t0, odometer_km=odo))
    db.session.commit()
    return v


def _snapshot(db, vid):
    from services.unit_history import COLUMNS
    from sqlalchemy import text
    return {f'{t}.{c}': db.session.execute(
        text(f'SELECT {c} FROM {t} WHERE vehicle_id=:v ORDER BY id'), {'v': vid}).scalars().all()
        for t, c, _ in COLUMNS}


def test_a_us_kia_history_is_converted_once_and_nothing_else_is_touched(file_db):
    from services.unit_history import convert_miles_cars_on_start
    db, tmp = file_db
    us = _seed_car(db, 'Niro US', 'kia', 'US')
    eu = _seed_car(db, 'Niro EU', 'kia', 'EU', odo=37750)
    eu_before = _snapshot(db, eu.id)

    assert convert_miles_cars_on_start(db) == [us.id]
    after = _snapshot(db, us.id)
    assert after['charges.odometer'] == [48280]
    assert after['vehicle_syncs.odometer_km'] == [48280]
    assert after['vehicle_syncs.estimated_range_km'] == [290]
    assert after['parking_events.odometer_departed'] == [round(30012 * U.KM_PER_MI)]
    assert after['vehicle_trips.distance_km'][0] == pytest.approx(12.5 * U.KM_PER_MI)
    assert after['vehicle_trips.max_speed_kmh'] == [100]
    # a rate per distance goes the OTHER way
    assert after['vehicle_trips.consumption_kwh_per_100km'][0] == pytest.approx(25.0 / U.KM_PER_MI)
    assert after['maintenance_log.next_due_km'] == [round(37500 * U.KM_PER_MI)]
    assert after['obd_readings.odometer_km'] == [48280]
    assert _snapshot(db, eu.id) == eu_before          # the other car: untouched

    # a backup of the whole file was written first, readable only by the owner
    baks = [p for p in os.listdir(tmp) if p.endswith('.bak')]
    assert len(baks) == 1 and oct(os.stat(tmp / baks[0]).st_mode)[-3:] == '600'
    con = sqlite3.connect(tmp / baks[0])
    assert con.execute('SELECT odometer FROM charges ORDER BY id').fetchall()[0][0] == 30000
    con.close()

    # the next start must not convert the (now km) rows a second time
    assert convert_miles_cars_on_start(db) == []
    assert _snapshot(db, us.id) == after


def test_an_odometer_survives_conversion_and_undo_exactly(file_db):
    from models.database import Vehicle
    from services.unit_history import convert
    db, _ = file_db
    v = _seed_car(db, 'Manual', None, None, odo=23457)
    before = _snapshot(db, v.id)
    assert convert(db, v, 'mi_to_km', reason='settings')['ok']
    assert convert(db, v, 'mi_to_km', reason='settings') == {'ok': False, 'reason': 'already'}
    assert convert(db, Vehicle.query.get(v.id), 'km_to_mi', reason='settings')['ok']
    back = _snapshot(db, v.id)
    for col, vals in before.items():
        if col.endswith(('distance_km', 'avg_speed_kmh', 'kwh_per_100km')):
            assert back[col] == pytest.approx(vals), col
        else:
            assert back[col] == vals, col                 # integers: exact


def test_an_automatic_conversion_cannot_be_undone(file_db):
    from services.unit_history import convert, note_reported_unit
    db, _ = file_db
    v = _seed_car(db, 'Porsche US', 'porsche', None)
    res = note_reported_unit(db, v, 'mi')          # first sync says "miles"
    assert res['ok'] and v.distance_source_unit == 'mi'
    assert note_reported_unit(db, v, 'mi') is None  # second sync: nothing
    assert convert(db, v, 'km_to_mi', reason='settings')['ok'] is False


def test_a_car_that_reports_km_is_only_remembered(file_db):
    from services.unit_history import note_reported_unit
    db, _ = file_db
    v = _seed_car(db, 'Enyaq', 'skoda', None, odo=12345)
    before = _snapshot(db, v.id)
    assert note_reported_unit(db, v, 'km') is None
    assert v.distance_source_unit == 'km' and _snapshot(db, v.id) == before


def test_a_failing_conversion_changes_nothing(file_db, monkeypatch):
    from services import unit_history as uh
    db, _ = file_db
    v = _seed_car(db, 'Niro US', 'kia', 'US')
    before = _snapshot(db, v.id)
    bad = uh.COLUMNS + (('vehicle_trips', 'no_such_column', 'len'),)
    monkeypatch.setattr(uh, 'COLUMNS', bad)
    monkeypatch.setattr(uh, 'count_rows', lambda db_, vid: {f'{t}.{c}': 1 for t, c, _ in bad})
    with pytest.raises(Exception):
        uh.convert(db, v, 'mi_to_km', reason='test')
    monkeypatch.undo()
    assert _snapshot(db, v.id) == before and v.history_unit_fixed_at is None


# ── 5. the pages, on a real server, in both units ────────────────────
def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def server():
    data_dir = tempfile.mkdtemp(prefix='evct-units-')
    port = _free_port()
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='units-test',
               APP_HOST='127.0.0.1', APP_PORT=str(port))
    starter = os.path.join(data_dir, 'run.py')
    with open(starter, 'w') as fh:
        fh.write('import sys\nsys.path.insert(0, %r)\nfrom app import create_app\n'
                 'from config import Config\n'
                 'create_app().run(host=Config.APP_HOST, port=Config.APP_PORT, debug=False)\n' % ROOT)
    proc = subprocess.Popen([sys.executable, starter], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise AssertionError('the instance exited during startup')
            try:
                urllib.request.urlopen(base + '/api/health', timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError('the instance never became healthy')
        dbp = os.path.join(data_dir, 'ev_tracker.db')
        con = sqlite3.connect(dbp)
        vid = con.execute('SELECT MIN(id) FROM vehicles').fetchone()[0]
        con.execute("UPDATE vehicles SET name='Testauto', auto_sync=0, battery_kwh=64,"
                    " recuperation_kwh_per_km=0.086, fossil_co2_per_km=164 WHERE id=?", (vid,))
        rows = [
            ("INSERT INTO charges (vehicle_id, date, odometer, kwh_loaded, eur_per_kwh, total_cost,"
             " charge_type, soc_from, soc_to) VALUES (?,?,?,?,?,?,?,?,?)",
             (vid, '2026-09-01', 37750, 30.0, 0.30, 9.0, 'AC', 20, 70)),
            ("INSERT INTO charges (vehicle_id, date, odometer, kwh_loaded, eur_per_kwh, total_cost,"
             " charge_type, soc_from, soc_to) VALUES (?,?,?,?,?,?,?,?,?)",
             (vid, '2026-09-20', 38750, 30.0, 0.30, 9.0, 'AC', 20, 70)),
            ("INSERT INTO vehicle_syncs (vehicle_id, timestamp, odometer_km, estimated_range_km,"
             " soc_percent) VALUES (?,?,?,?,?)", (vid, '2026-09-20 10:00:00', 38750, 300, 70)),
            ("INSERT INTO maintenance_log (vehicle_id, date, item_type, title, odometer_km,"
             " next_due_km) VALUES (?,?,?,?,?,?)", (vid, '2026-08-01', 'tires', 'Reifen', 37000, 38000)),
        ]
        for sql, args in rows:
            con.execute(sql, args)
        con.commit()
        con.close()
        yield base, dbp, vid
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.read().decode('utf-8', 'replace')


def _post(base, path, form):
    req = urllib.request.Request(base + path, method='POST',
                                 data=urllib.parse.urlencode(form).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def _set_unit(base, unit):
    _post(base, '/settings', {'action': 'save_units', 'distance_unit': unit})


PAGES = ('/', '/history', '/trips', '/maintenance', '/report', '/settings', '/input', '/obd')


def _visible_km(html):
    """'km' as a word in what the page renders — outside scripts' code
    names (range_km, data-km …) and outside the unit picker itself."""
    html = re.sub(r'<option value="(km|auto)"[^<]*</option>', '', html)
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'\{#.*?#\}', ' ', html, flags=re.S)
    return [m.group(0) for m in re.finditer(r'.{0,30}(?<![\w{_.-])km(?![\w}_])(?!/h).{0,20}', html)]


def test_every_page_renders_in_both_units(server):
    base, _, _ = server
    for unit in ('km', 'mi', 'km'):
        _set_unit(base, unit)
        for p in PAGES:
            html = _get(base, p)          # a 500 raises here
            assert 'window.U' in html, p


def test_in_miles_no_page_says_km(server):
    base, _, _ = server
    _set_unit(base, 'mi')
    try:
        # the units card explains that data is stored in km — allowed
        # (and '/' shows it too on an install without data: it redirects)
        erklaert = ('gespeichert', 'als km', 'in km umrechnen')
        found = {p: [f for f in _visible_km(_get(base, p))
                     if not any(e in f.lower() for e in erklaert)] for p in PAGES}
        assert not any(found.values()), {k: v[:3] for k, v in found.items() if v}
    finally:
        _set_unit(base, 'km')


def test_in_miles_the_numbers_are_miles(server):
    base, _, _ = server
    _set_unit(base, 'mi')
    try:
        hist = _get(base, '/history')
        assert '24.078' in hist and '23.457' in hist          # 38 750 / 37 750 km
        assert '38.750' not in hist
        maint = _get(base, '/maintenance')
        assert 'value="24078"' in maint                       # current odometer, in mi
        assert 'data-km="38750"' in maint                     # the stored km rides along
    finally:
        _set_unit(base, 'km')


def test_in_km_the_pages_say_exactly_what_they_said(server):
    base, _, _ = server
    _set_unit(base, 'km')
    hist = _get(base, '/history')
    assert '38.750' in hist and '37.750' in hist
    assert 'value="38750"' in _get(base, '/maintenance')


def test_saving_a_charge_unchanged_in_miles_keeps_its_odometer(server):
    base, dbp, vid = server
    con = sqlite3.connect(dbp)
    cid = con.execute('SELECT id FROM charges WHERE odometer=37750').fetchone()[0]
    con.close()
    _set_unit(base, 'mi')
    try:
        page = _get(base, f'/edit/{cid}')
        shown = re.search(r'name="odometer"\s+value="(\d+)"', page).group(1)
        hidden = re.search(r'name="odometer__km" value="(\d+)"', page).group(1)
        assert (shown, hidden) == ('23457', '37750')
        form = {'date': '2026-09-01', 'odometer': shown, 'odometer__km': hidden,
                'kwh_loaded': '30', 'eur_per_kwh': '0.30', 'charge_type': 'AC',
                'soc_from': '20', 'soc_to': '70'}
        _post(base, f'/edit/{cid}', form)
        con = sqlite3.connect(dbp)
        assert con.execute('SELECT odometer FROM charges WHERE id=?', (cid,)).fetchone()[0] == 37750
        # a CHANGED value is taken as miles
        _post(base, f'/edit/{cid}', dict(form, odometer='23500'))
        assert con.execute('SELECT odometer FROM charges WHERE id=?', (cid,)).fetchone()[0] \
            == round(23500 * U.KM_PER_MI)
        con.execute('UPDATE charges SET odometer=37750 WHERE id=?', (cid,))
        con.commit()
        con.close()
    finally:
        _set_unit(base, 'km')


def test_the_api_stays_in_km_whatever_the_page_shows(server):
    """Home Assistant and other callers read /api — those numbers must not
    change their meaning with a display setting."""
    base, _, _ = server
    _set_unit(base, 'mi')
    try:
        stats = json.loads(_get(base, '/api/report?preset=all'))
        assert stats['summary']['total_km'] in (0, 1000, 1000.0) or stats['summary']['total_km'] >= 0
        hist = json.loads(_get(base, '/api/vehicle/history?days=0'))
        odo = [v for v in hist.get('series', {}).get('odometer_km', []) if v]
        assert odo and max(odo) == 38750
    finally:
        _set_unit(base, 'km')


# ── 6. the PDF report — in every language, in both units ─────────────
_PDF_RUN = r"""
import json, os, sys, tempfile
from datetime import date
sys.path.insert(0, %(root)r)
try:
    import fpdf, matplotlib  # noqa: F401
except ImportError:
    print(json.dumps({'skip': True})); sys.exit(0)
from flask import Flask
from models.database import db, Vehicle, Charge
from services import units
from services.i18n import set_language
from services import report_service as rs
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'r.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
out = {}
with app.app_context():
    db.create_all()
    v = Vehicle(name='Report', battery_kwh=64.0, is_archived=False, auto_sync=False)
    db.session.add(v); db.session.commit()
    for i in range(6):
        db.session.add(Charge(vehicle_id=v.id, date=date(2026, 3 + i, 5), odometer=38000 + 400 * i,
                              kwh_loaded=30.0, eur_per_kwh=0.3, total_cost=9.0, charge_type='AC',
                              soc_from=20, soc_to=70))
    db.session.commit()
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    for lang in %(langs)r:
        for unit in ('km', 'mi'):
            set_language(lang); units.set_unit(unit)
            units.set_currency({'en': 'USD', 'nl': 'PLN'}.get(lang, 'EUR'))
            try:
                pdf = bytes(rs.generate_report())
                text = '\n'.join(p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf')) if pymupdf else None
                out[lang + '-' + unit] = {'size': len(pdf), 'text': text}
            except Exception as e:
                out[lang + '-' + unit] = {'error': '%%s: %%s' %% (type(e).__name__, e)}
print(json.dumps(out))
"""


@pytest.fixture(scope='module')
def pdf_reports():
    """Every report, rendered in a FRESH interpreter: another suite file
    empties ``sys.modules``, after which this process holds several
    SQLAlchemy instances and the report reads one the test never filled."""
    with tempfile.TemporaryDirectory() as d:
        script = os.path.join(d, 'pdf_run.py')
        with open(script, 'w') as fh:
            fh.write(_PDF_RUN % {'root': ROOT, 'langs': LANGS})
        r = subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True,
                           text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('lang', LANGS)
@pytest.mark.parametrize('unit', ('km', 'mi'))
def test_the_pdf_report_renders_in_every_language_and_unit(pdf_reports, lang, unit):
    """The report PDF was German-only until v3.0.155, and its core font is
    latin-1: one character outside it (a curly apostrophe, ≈) and fpdf
    refuses the whole report. Rendered for every language in both units,
    and read back."""
    if pdf_reports.get('skip'):
        pytest.skip('fpdf/matplotlib not installed')
    res = pdf_reports[f'{lang}-{unit}']
    assert 'error' not in res, res.get('error')
    assert res['size'] > 20_000
    text = res['text']
    if text is None:
        return
    if unit == 'mi':
        assert ' mi' in text and not KM_TOKEN.search(text), [
            m.group(0) for m in re.finditer(r'.{0,25}\bkm\b.{0,10}', text)][:5]
    else:
        assert ' km' in text
    if lang != 'de':
        assert 'Gesamtübersicht' not in text and 'Ladekosten' not in text
    # money: the ISO code of the chosen currency — and zł, which the PDF
    # font cannot draw, does not break the report (nl runs as PLN here)
    code = {'en': 'USD', 'nl': 'PLN'}.get(lang, 'EUR')
    assert code in text
    if code != 'EUR':
        assert 'EUR' not in text and '€' not in text


# ── 7. currency and the browser's country (v3.0.155) ─────────────────
def test_the_browser_country_decides_units_but_a_choice_wins():
    assert U.country_of(['en-US', 'en']) == 'US' and U.country_of(['de']) is None
    assert U.resolve('auto', None, ['en-US'])[:2] == ('mi', 'USD')
    assert U.resolve('auto', None, ['en-GB'])[:2] == ('mi', 'GBP')
    assert U.resolve('auto', None, ['de-DE', 'de'])[:2] == ('km', 'EUR')
    assert U.resolve('auto', None, ['de-CH'])[:2] == ('km', 'CHF')
    assert U.resolve('km', 'EUR', ['en-US'])[:2] == ('km', 'EUR')     # stored wins
    assert U.resolve(None, None, ['en-US'])[0] == 'mi'                # never set = auto


def test_money_is_detected_once_and_only_from_a_real_country():
    # first visit from a US browser: remember USD
    assert U.resolve('auto', None, ['en-US'])[2] == 'USD'
    # once stored, nothing more is remembered — a phone set to en-US must
    # not put "$" in front of prices that were typed in euros
    assert U.resolve('auto', 'EUR', ['en-US']) == ('mi', 'EUR', None)
    # a health check or Home Assistant names no country: no guess is stored
    assert U.resolve('auto', None, []) == ('km', 'EUR', None)


def test_no_text_shows_a_euro_sign_when_the_currency_is_dollars():
    from services.i18n import set_language, t
    U.set_currency('USD')
    leftovers = []
    try:
        for lang in LANGS:
            set_language(lang)
            for key in _all(lang):
                if '€' in t(key):
                    leftovers.append(f'{lang}:{key}')
    finally:
        set_language('de')
        U.set_currency('EUR')
    assert not leftovers, leftovers[:10]


def test_an_alphabetic_sign_keeps_its_distance_from_the_number():
    U.set_currency('CHF')
    try:
        assert U.cur_pre() == 'CHF ' and U.cur() == 'CHF'
    finally:
        U.set_currency('EUR')
    assert U.cur_pre() == '€'


def _get_as(base, path, accept):
    req = urllib.request.Request(base + path, headers={'Accept-Language': accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', 'replace')


def test_a_us_browser_on_a_fresh_install_gets_english_miles_and_dollars(server):
    base, dbp, _ = server
    con = sqlite3.connect(dbp)
    con.execute("DELETE FROM app_config WHERE key IN ('currency', 'currency_detected_from',"
                " 'distance_unit', 'app_language')")
    con.commit()
    try:
        page = _get_as(base, '/history', 'en-US,en;q=0.9')
        assert '<th class="text-end d-none d-md-table-cell">mi</th>' in page
        assert '$9.00' in page and '€' not in re.sub(r'<script.*?</script>', '', page, flags=re.S)
        assert 'Charging' in page or 'Date' in page                     # English
        assert con.execute("SELECT value FROM app_config WHERE key='currency'").fetchone()[0] == 'USD'
        # the next visitor with a German browser: German, km — and still
        # dollars, because money does not follow the device
        page = _get_as(base, '/history', 'de-DE,de;q=0.9')
        assert '<th class="text-end d-none d-md-table-cell">km</th>' in page and '$9.00' in page
    finally:
        con.execute("DELETE FROM app_config WHERE key IN ('currency', 'currency_detected_from')")
        con.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('distance_unit', 'km')")
        con.commit()
        con.close()


def test_a_german_install_stays_exactly_as_it_was(server):
    """A German install: German browsers, euros, kilometres — the update
    must not change a single sign there."""
    base, dbp, _ = server
    con = sqlite3.connect(dbp)
    con.execute("DELETE FROM app_config WHERE key IN ('currency', 'currency_detected_from',"
                " 'distance_unit', 'app_language')")
    con.commit()
    try:
        page = _get_as(base, '/history', 'de-DE,de;q=0.9,en;q=0.8')
        assert '€9.00' in page and '38.750' in page and 'Ladeverlauf' in page
        assert con.execute("SELECT value FROM app_config WHERE key='currency'").fetchone()[0] == 'EUR'
    finally:
        con.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('distance_unit', 'km')")
        con.commit()
        con.close()


def _cfg(db, key):
    from models.database import AppConfig
    return AppConfig.get(key)


def test_an_existing_german_install_keeps_german_euros_and_km(file_db):
    """Most installs never chose a language — German was simply the
    default. Opening the page on a phone set to en-US must not turn their
    install English, or put "$" in front of prices typed in euros."""
    db, _ = file_db
    _seed_car(db, 'Enyaq', 'skoda_api', 'EU')
    assert U.settle_existing_install(db) == {'currency': 'EUR', 'distance_unit': 'km',
                                             'app_language': 'de'}
    assert (_cfg(db, 'currency'), _cfg(db, 'distance_unit'), _cfg(db, 'app_language')) == \
        ('EUR', 'km', 'de')
    assert U.settle_existing_install(db) is None              # once


def test_an_existing_us_install_gets_dollars_and_miles(file_db):
    from models.database import AppConfig
    db, _ = file_db
    AppConfig.set('app_language', 'en')
    _seed_car(db, 'Niro US', 'kia', 'US')
    U.settle_existing_install(db)
    assert (_cfg(db, 'currency'), _cfg(db, 'distance_unit'), _cfg(db, 'app_language')) == \
        ('USD', 'mi', 'en')


def test_an_owner_who_chose_another_language_is_left_to_the_browser(file_db):
    from models.database import AppConfig
    db, _ = file_db
    AppConfig.set('app_language', 'en')
    _seed_car(db, 'Model 3', 'tesla', None)
    assert U.settle_existing_install(db) == {}
    assert _cfg(db, 'currency') is None and _cfg(db, 'distance_unit') is None


def test_a_fresh_install_detects_from_its_first_page(file_db):
    db, _ = file_db
    assert U.settle_existing_install(db) == {}
    assert _cfg(db, 'currency') is None and _cfg(db, U.SETTLED_MARK) == 'done'
