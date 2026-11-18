# -*- coding: utf-8 -*-
"""Leapmotor read directly from a Leapmotor Mate install (v3.0.195, experimental).

A fake Mate runs on 127.0.0.1 for these tests: it answers the four paths the
connector uses (``/healthz``, ``/login``, the two CSV exports and the
charge-in-progress panel) the way Mate's source does — including the
password cookie, the 401 for a missing session, the 403 for a refused host
name and the 429 login brake. All data is invented; no real Mate, no real car.
"""
import csv
import io
import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import connector_leapmotor_mate as M  # noqa: E402

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')
TZ = timezone(timedelta(hours=1))


def _iso(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).isoformat()


# Columns as Mate's export writes them (charges table + the fields its list adds).
CHARGE_COLS = ['id', 'vehicle_id', 'started_at', 'ended_at', 'start_soc', 'end_soc',
               'energy_added_kwh', 'duration_min', 'latitude', 'longitude', 'charge_type',
               'location_type', 'max_power_kw', 'cost', 'ac_energy_kwh', 'gross_kwh',
               'odometer_km', 'place', 'prev_charge_id', 'can_merge_prev']
TRIP_COLS = ['id', 'vehicle_id', 'started_at', 'ended_at', 'distance_km', 'start_soc',
             'end_soc', 'start_odometer_km', 'end_odometer_km', 'duration_min', 'start_place',
             'end_place']


def _csv(cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, '') for c in cols})
    return buf.getvalue()


CHARGES = [   # newest first, like Mate
    dict(id=12, started_at=_iso(2026, 11, 16, 18), ended_at=_iso(2026, 11, 16, 19, 10),
         start_soc=31, end_soc=78, energy_added_kwh=31.2, duration_min=70, latitude=48.1,
         longitude=11.5, charge_type='DC', max_power_kw=118.4, place="'=Fast charger"),
    # V2L: the pack gave energy away — no charge
    dict(id=11, started_at=_iso(2026, 11, 15, 12), ended_at=_iso(2026, 11, 15, 14),
         start_soc=70, end_soc=64, energy_added_kwh=3.1, charge_type='AC'),
    dict(id=10, started_at=_iso(2026, 11, 14, 22), ended_at=_iso(2026, 11, 15, 5, 30),
         start_soc=22, end_soc=80, energy_added_kwh=38.0, ac_energy_kwh=41.9,
         duration_min=450, latitude=0, longitude=0, charge_type='AC', max_power_kw=11.0,
         location_type='HOME', place='Home'),
    # still open in Mate's view: no end
    dict(id=13, started_at=_iso(2026, 11, 17, 8), ended_at='', start_soc=40, end_soc=''),
    # the 255 sentinel and an impossible peak
    dict(id=9, started_at=_iso(2026, 11, 10, 9), ended_at=_iso(2026, 11, 10, 10),
         start_soc=255, end_soc=60, energy_added_kwh=12.0, max_power_kw=9999),
]
TRIPS = [
    dict(id=5, started_at=_iso(2026, 11, 16, 20), ended_at=_iso(2026, 11, 16, 20, 40),
         distance_km=31.5, start_soc=78, end_soc=71, start_odometer_km=10230,
         end_odometer_km=10261.5),
    dict(id=4, started_at=_iso(2026, 11, 16, 16), ended_at=_iso(2026, 11, 16, 17, 30),
         distance_km=120, start_soc=60, end_soc=31, start_odometer_km=10110,
         end_odometer_km=10230),
    dict(id=3, started_at=_iso(2026, 11, 14, 17), ended_at=_iso(2026, 11, 14, 18),
         distance_km=40, start_soc=40, end_soc=22, start_odometer_km=0, end_odometer_km=0),
]

LIVE_CARD = """<div class="card order-3">
  <span class="font-semibold text-white">Charge profile</span>
<script>
  var series = {
    power:     { name: "Power", color: '#fbbf24', data: col([7.1, 11.0, null, 10.8]), unit: 'kW', dec: 2 },
    wallbox:   { name: "Wallbox", color: '#34d399', data: col([]), unit: 'kW', dec: 2 },
    soc:       { name: "SoC", color: '#22c55e', data: col([40.0, 41.5, 43.2]), unit: '%', dec: 1 },
  };
</script></div>"""
IDLE_PANEL = "{# comment #}\n\n  \n"


class FakeMate:
    """Mate's behaviour, as far as the connector sees it."""

    def __init__(self):
        self.password = ''
        self.state = 'ok'
        self.refuse_host = False
        self.throttle = False
        self.charging = False
        self.live_html = LIVE_CARD
        self.charges = _csv(CHARGE_COLS, CHARGES)
        self.trips = _csv(TRIP_COLS, TRIPS)
        self.logins = 0
        self.valid = set()
        self.healthz_html = False
        handler = self._handler()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        mate = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body='', ctype='text/plain', headers=None):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                if not mate.password:
                    return True
                m = re.search(r'mate_session=([^;]+)', self.headers.get('Cookie', ''))
                return bool(m and m.group(1) in mate.valid)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if mate.refuse_host and not mate.password and path != '/healthz':
                    return self._send(403, M_HOST_REFUSED)
                if path == '/healthz':
                    if mate.healthz_html:
                        return self._send(404, '<html>Not Found</html>', 'text/html')
                    return self._send(200, json.dumps({'status': mate.state}), 'application/json')
                if not self._authed():
                    if path.startswith('/api/'):
                        return self._send(401, 'authentication required')
                    return self._send(307, '', headers={'Location': '/login'})
                if path == '/api/export/charges.csv':
                    return self._send(200, mate.charges, 'text/csv; charset=utf-8')
                if path == '/api/export/trips.csv':
                    return self._send(200, mate.trips, 'text/csv; charset=utf-8')
                if path == '/api/charging-chart':
                    return self._send(200, mate.live_html if mate.charging else IDLE_PANEL,
                                      'text/html')
                return self._send(404, 'not found')

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if urllib.parse.urlsplit(self.path).path != '/login':
                    return self._send(404)
                if mate.throttle:
                    return self._send(429, '<html>wait</html>', 'text/html')
                if not mate.password:
                    return self._send(303, '', headers={'Location': '/'})
                if form.get('password', [''])[0] == mate.password:
                    mate.logins += 1
                    token = f'tok{mate.logins}'
                    mate.valid.add(token)
                    return self._send(303, '', headers={
                        'Location': '/',
                        'Set-Cookie': f'mate_session={token}; HttpOnly; Max-Age=2592000; '
                                      f'Path=/; SameSite=strict'})
                return self._send(401, '<html>wrong</html>', 'text/html')
        return H


M_HOST_REFUSED = ("Mate answers on this name only with a password. Open Mate by its address on "
                  "your network and set one in Settings → Access, or name this host in "
                  "MATE_ALLOWED_HOSTS.")


@pytest.fixture
def mate():
    M._SESSIONS.clear()
    m = FakeMate()
    yield m
    m.close()
    M._SESSIONS.clear()


def _conn(url, password=''):
    return M.LeapmotorMateConnector({'username': url, 'password': password})


# ── reading Mate's files ──────────────────────────────────────────────
def test_charges_are_read_with_guards():
    rows = list(csv.DictReader(io.StringIO(_csv(CHARGE_COLS, CHARGES))))
    sessions, skipped = M.parse_charges(rows)
    assert [s['id'] for s in sessions] == [9, 10, 12]          # oldest first
    assert skipped == 2                                        # V2L + still open
    by = {s['id']: s for s in sessions}
    home = by[10]
    assert home['kwh'] == 41.9 and home['kwh_source'] == 'wallbox'   # the meter wins
    assert home['type'] == 'AC' and home['peak_kw'] == 11.0
    assert home['lat'] is None and home['lon'] is None              # (0, 0) is no position
    fast = by[12]
    assert fast['kwh'] == 31.2 and fast['kwh_source'] == 'battery'
    assert fast['type'] == 'DC' and fast['place'] == '=Fast charger'  # export escaping undone
    assert fast['duration'] == 70 * 60
    odd = by[9]
    assert odd['soc_from'] is None and odd['soc_to'] == 60          # 255 is no reading
    assert odd['peak_kw'] is None                                    # 9999 kW is no reading


def test_charger_figure_beats_meter_and_battery():
    rows = [dict(id='1', started_at=_iso(2026, 1, 1, 1), ended_at=_iso(2026, 1, 1, 2),
                 start_soc='10', end_soc='50', energy_added_kwh='20', ac_energy_kwh='22',
                 gross_kwh='23.5')]
    s, _ = M.parse_charges(rows)
    assert s[0]['kwh'] == 23.5 and s[0]['kwh_source'] == 'charger'


def test_a_renamed_column_is_a_missing_value_not_a_crash():
    rows = [dict(id='1', begin='x', ended_at=_iso(2026, 1, 1, 2))]
    assert M.parse_charges(rows) == ([], 1)
    rows = [dict(id='2', started_at=_iso(2026, 1, 1, 1), ended_at=_iso(2026, 1, 1, 2),
                 start_soc='10', end_soc='50')]                      # no energy columns at all
    s, _ = M.parse_charges(rows)
    assert s[0]['kwh'] is None and s[0]['soc_to'] == 50
    assert M.latest_reading([], [dict(foo='1')]) == {
        'soc': None, 'at': None, 'odometer': None, 'odometer_at': None}


def test_latest_reading_takes_newest_soc_and_highest_odometer():
    sessions, _ = M.parse_charges(list(csv.DictReader(io.StringIO(_csv(CHARGE_COLS, CHARGES)))))
    trips = list(csv.DictReader(io.StringIO(_csv(TRIP_COLS, TRIPS))))
    r = M.latest_reading(sessions, trips)
    assert r['soc'] == 71                       # the drive after the DC charge ended last
    assert r['odometer'] == 10261.5             # 0 is no reading
    assert r['at'] == int(datetime(2026, 11, 16, 20, 40, tzinfo=TZ).timestamp())


def test_live_panel():
    assert M.parse_live_chart(IDLE_PANEL) == {'charging': False, 'soc': None, 'power_kw': None}
    assert M.parse_live_chart('') == {'charging': False, 'soc': None, 'power_kw': None}
    live = M.parse_live_chart(LIVE_CARD)
    assert live == {'charging': True, 'soc': 43.2, 'power_kw': 10.8}
    # a future template the numbers cannot be read from: still "charging", no invented values
    assert M.parse_live_chart('<div class="card">new layout</div>') == {
        'charging': True, 'soc': None, 'power_kw': None}


# ── talking to Mate ───────────────────────────────────────────────────
def test_status_without_password(mate):
    st = _conn(mate.url).get_status()
    assert st.soc_percent == 71 and st.odometer_km == 10261
    assert st.is_charging is False and st.charge_power_kw is None
    assert st.raw_data['source'] == 'leapmotor_mate' and st.raw_data['charges_usable'] == 3
    assert st.captured_at == datetime.fromtimestamp(
        datetime(2026, 11, 16, 20, 40, tzinfo=TZ).timestamp())


def test_charging_now_uses_live_soc_and_power(mate):
    mate.charging = True
    c = _conn(mate.url)
    st = c.get_status()
    assert st.is_charging and st.is_plugged_in
    assert st.soc_percent == 43 and st.charge_power_kw == 10.8
    # each charge knows the odometer of the drive before it
    assert {s['id']: s['odometer'] for s in c.charge_sessions} == {9: None, 10: None, 12: 10230}


def test_odometer_written_at_the_charge_wins(mate):
    rows = [dict(r) for r in CHARGES]
    rows[0]['odometer_km'] = 10235
    rows[2]['odometer_km'] = 10111
    mate.charges = _csv(CHARGE_COLS, rows)
    c = _conn(mate.url)
    c.get_status()
    assert {s['id']: s['odometer'] for s in c.charge_sessions} == {9: None, 10: 10111, 12: 10235}


def test_test_button_says_what_mate_holds(mate):
    mate.state = 'stale'
    msg = _conn(mate.url + '/settings').verify_credentials()    # a pasted page address works
    assert '3' in msg and '71' in msg and '10' in msg


def test_password_flow_and_session_reuse(mate):
    mate.password = 'correct horse'
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).get_status()
    assert e.value.kind == 'auth'
    with pytest.raises(M.MateError) as e:
        _conn(mate.url, 'wrong').get_status()
    assert e.value.kind == 'auth'
    c = _conn(mate.url, 'correct horse')
    assert c.get_status().soc_percent == 71
    _conn(mate.url, 'correct horse').get_status()
    assert mate.logins == 1                          # the session is kept, not a login per poll
    mate.valid.clear()                               # Mate restarted with a new key
    assert _conn(mate.url, 'correct horse').get_status().odometer_km == 10261
    assert mate.logins == 2


def test_login_brake_is_explained(mate):
    mate.password, mate.throttle = 'pw-12345', True
    with pytest.raises(M.MateError) as e:
        _conn(mate.url, 'pw-12345').get_status()
    assert e.value.kind == 'throttled'


def test_refused_host_name_is_explained(mate):
    mate.refuse_host = True
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).get_status()
    assert e.value.kind == 'host'


def test_setup_and_demo_and_not_mate(mate):
    mate.state = 'awaiting_setup'
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).verify_credentials()
    assert e.value.kind == 'setup'
    mate.state = 'demo'
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).verify_credentials()
    assert e.value.kind == 'demo'
    with pytest.raises(M.MateError) as e:          # the sync refuses a sample car as well
        _conn(mate.url).get_status()
    assert e.value.kind == 'demo'
    mate.healthz_html = True
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).verify_credentials()
    assert e.value.kind == 'not_mate'


def test_unreachable_and_missing_address():
    with pytest.raises(M.MateError) as e:
        _conn('http://127.0.0.1:9').get_status()          # nothing listens on port 9
    assert e.value.kind == 'network'
    with pytest.raises(M.MateError) as e:
        _conn('').get_status()
    assert e.value.kind == 'no_url'
    assert _conn('http://127.0.0.1:9').test_connection() is False


def test_empty_mate_says_so(mate):
    mate.charges, mate.trips = _csv(CHARGE_COLS, []), ''
    with pytest.raises(M.MateError) as e:
        _conn(mate.url).verify_credentials()
    assert e.value.kind == 'nothing'


# ── registry, catalog, maker ──────────────────────────────────────────
def test_brand_is_offered_and_needs_only_the_address():
    from services.vehicle.registry import is_installed, credential_fields_of
    from services.vehicle.catalog import BRANDS, credentials_present
    from services.vehicle.maker import display_maker
    assert is_installed('leapmotor_mate')
    assert any(b.key == 'leapmotor_mate' for b in BRANDS)
    assert [f['key'] for f in credential_fields_of('leapmotor_mate')] == ['username', 'password']
    assert credentials_present('leapmotor_mate', username='http://10.0.0.5:4000')
    assert not credentials_present('leapmotor_mate', password='x')
    assert M.LeapmotorMateConnector.records_own_charges is True
    from types import SimpleNamespace
    assert display_maker(SimpleNamespace(api_brand='leapmotor_mate', brand='', model='B10',
                                         name='')) == 'Leapmotor'


# ── filing the charges ────────────────────────────────────────────────
@pytest.fixture
def app():
    from flask import Flask
    from models.database import db, Vehicle
    a = Flask(__name__)
    a.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    a.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(a)
    with a.app_context():
        db.create_all()
        v = Vehicle()
        v.name, v.is_archived, v.api_brand = 'B10', False, 'leapmotor_mate'
        db.session.add(v)
        db.session.commit()
        yield a


def _sessions():
    s, _ = M.parse_charges(list(csv.DictReader(io.StringIO(_csv(CHARGE_COLS, CHARGES)))))
    return s


def test_import_files_once(app):
    from models.database import Charge
    from services.leapmotor_mate_import import import_sessions
    n = import_sessions(1, _sessions(), 67.0, 0.9)
    assert n['new'] == 3
    rows = Charge.query.order_by(Charge.date, Charge.charge_hour).all()
    assert all(r.needs_review for r in rows)
    home = [r for r in rows if r.kwh_loaded == 41.9][0]
    assert home.charge_type == 'AC' and home.soc_from == 22 and home.soc_to == 80
    assert home.notes.startswith('leapmotor-mate:10@') and 'wallbox meter' in home.notes
    n2 = import_sessions(1, _sessions(), 67.0, 0.9)
    assert n2['new'] == 0 and n2['dup'] == 3 and Charge.query.count() == 3


def test_import_leaves_the_owners_entry_alone(app):
    from datetime import date
    from models.database import db, Charge
    from services.leapmotor_mate_import import import_sessions
    start = datetime.fromtimestamp(datetime(2026, 11, 16, 18, tzinfo=TZ).timestamp())
    db.session.add(Charge(vehicle_id=1, date=start.date(), charge_hour=start.hour,
                          kwh_loaded=33.0, charge_type='DC', notes='typed by hand'))
    db.session.commit()
    n = import_sessions(1, _sessions(), 67.0, 0.9)
    assert n['overlap'] == 1 and n['new'] == 2
    assert Charge.query.filter(Charge.notes == 'typed by hand').one().kwh_loaded == 33.0
    assert isinstance(date.today(), date)


def test_a_merge_in_mate_updates_an_unreviewed_row_only(app):
    from models.database import db, Charge
    from services.leapmotor_mate_import import import_sessions
    s = _sessions()
    import_sessions(1, s, 67.0, 0.9)
    longer = [dict(x) for x in s]
    for x in longer:
        if x['id'] == 10:
            x['end'] += 3600
            x['soc_to'] = 90.0
    n = import_sessions(1, longer, 67.0, 0.9)
    assert n['updated'] == 1
    row = Charge.query.filter(Charge.notes.like('leapmotor-mate:10@%')).one()
    assert row.soc_to == 90
    row.needs_review = False
    db.session.commit()
    for x in longer:
        if x['id'] == 10:
            x['soc_to'] = 95.0
            x['end'] += 60
    n = import_sessions(1, longer, 67.0, 0.9)
    assert n['updated'] == 0 and n['dup'] == 3
    assert Charge.query.filter(Charge.notes.like('leapmotor-mate:10@%')).one().soc_to == 90


def test_implausible_energy_is_not_filed(app):
    from models.database import Charge
    from services.leapmotor_mate_import import import_sessions
    s = _sessions()
    s[0]['kwh'] = 500.0
    n = import_sessions(1, s, 67.0, 0.9)
    assert n['implausible'] == 1 and Charge.query.count() == 2


# ── the Home Assistant route keeps V2L out of the charging power ──────
def test_home_assistant_route_never_books_v2l_as_charging_power():
    from services.vehicle import connector_homeassistant as H

    def st(eid, state, unit=None, dc=None):
        a = {}
        if unit:
            a['unit_of_measurement'] = unit
        if dc:
            a['device_class'] = dc
        return {'entity_id': eid, 'state': state, 'attributes': a}
    c = H.HomeAssistantConnector({'username': 'http://ha:8123', 'password': 't',
                                  'vin': 'leapmotor_mate_123456'})
    p = 'sensor.leapmotor_mate_123456_'
    without_charge_power = [st(p + 'soc', '64', '%', 'battery'),
                            st(p + 'v2l_power', '1800', 'W', 'power'),
                            st(p + 'climate_power', '900', 'W', 'power')]
    roles = c.map_entities(without_charge_power)
    assert 'power' not in roles
    status = c._to_status(roles)
    assert status.is_charging is False and status.charge_power_kw is None
    roles = c.map_entities(without_charge_power + [st(p + 'charge_power', '7.2', 'kW', 'power')])
    assert roles['power']['entity_id'] == p + 'charge_power'


# ── texts ─────────────────────────────────────────────────────────────
def test_every_text_exists_in_every_language_with_the_same_placeholders():
    base = None
    for lang in LANGS:
        d = json.load(open(os.path.join(ROOT, 'translations', lang + '.json'), encoding='utf-8'))
        keys = {k: v for k, v in d.items()
                if k.startswith('mate.') or k.startswith('cred.mate') or k.startswith('cred.help_mate')}
        assert len(keys) >= 19, lang
        if base is None:
            base = keys
        assert set(keys) == set(base), lang
        for k, v in keys.items():
            assert sorted(re.findall(r'\{(\w+)\}', v)) == sorted(re.findall(r'\{(\w+)\}', base[k])), (lang, k)
    import ast
    src = open(os.path.join(ROOT, 'services', 'vehicle', 'connector_leapmotor_mate.py'),
               encoding='utf-8').read()
    used = set(re.findall(r"_t\('((?:mate|cred)\.[a-z_]+)'", src))
    used |= {f[k] for f in M.CREDENTIAL_FIELDS for k in ('label_key', 'help_key')}
    assert used and used <= set(base)
    assert ast.parse(src)
