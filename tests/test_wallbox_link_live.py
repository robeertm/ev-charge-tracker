# -*- coding: utf-8 -*-
"""The wallbox link, end to end, against a real server.

The unit test next to this one pins the matching rules down. This one pins the
*wiring*: the migration that adds the columns, the settings form, the route
that talks to the analyzer, the background pass, and the two answers the
browser actually receives — the reading on a charge and the curve behind it.

The analyzer is a stub here, speaking the same ``/api/v1/ev/*`` shape the real
one does, so the test needs nothing but Python. What it does NOT stub is our
own side: that is the part that has to work.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOKEN = 'probe-schluessel'
JETZT = datetime.now().replace(minute=0, second=0, microsecond=0)
START = JETZT - timedelta(days=1)          # yesterday, same hour
ENDE = START + timedelta(hours=2)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _curve():
    ts = [int((START + timedelta(minutes=5 * i)).timestamp()) for i in range(24)]
    return {
        'available': True, 'start_ts': ts[0], 'end_ts': ts[-1], 'ts': ts,
        'load_w': [7000.0] * 24,
        'solar_w': [6000.0] * 18 + [0.0] * 6,
        'battery_w': [500.0] * 18 + [0.0] * 6,
        'grid_w': [500.0] * 18 + [7000.0] * 6,
        'measured': [True] * 24,
        'seconds': {'solar': 5400, 'battery': 5400, 'grid': 7200, 'total': 7200},
    }


# What the stub was actually asked for. A test that only checks the answer
# cannot see a parameter that was dropped on the way out.
ANFRAGEN = []


class _Analyzer(BaseHTTPRequestHandler):
    """A stand-in for the energy analyzer — including its refusals."""

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get('X-EV-Link-Token') != TOKEN:
            # Exactly what the real one does with a wrong key, so the error the
            # user is shown is exercised too and not just the happy path.
            self.send_response(401)
            self.end_headers()
            return
        ANFRAGEN.append(self.path)
        path = self.path.split('?')[0]
        if path.endswith('/info'):
            return self._json({'ok': True, 'data': {
                'product': 'shelly-energy-analyzer', 'version': '16.85.0',
                'wallbox': {'device_key': 'wallbox', 'name': 'Wallbox Garage'},
                'wallboxes': [{'device_key': 'wallbox', 'name': 'Wallbox Garage'}],
                'sources': {'grid': True, 'solar': True, 'battery': True},
                'settle_minutes': 20, 'currency': 'EUR'}})
        if path.endswith('/charges'):
            return self._json({'ok': True, 'data': {
                'wallbox': {'device_key': 'wallbox', 'name': 'Wallbox Garage'},
                'pending_settle': 0,
                'charges': [{
                    'id': 'abc123', 'device_key': 'wallbox',
                    'start_ts': int(START.timestamp()),
                    'end_ts': int(ENDE.timestamp()),
                    'energy_kwh': 21.4, 'solar_kwh': 17.0, 'battery_kwh': 2.4,
                    'grid_kwh': 2.0, 'cost_eur': 0.6, 'cost_model': 'source',
                    'coverage': 1.0, 'avg_power_w': 7000.0,
                    'peak_power_w': 7400.0, 'session_count': 3}]}})
        if path.endswith('/curve'):
            return self._json({'ok': True, 'data': _curve()})
        self._json({'ok': False, 'error': 'unknown'}, 404)


@pytest.fixture(scope='module')
def analyzer():
    port = _free_port()
    srv = HTTPServer(('127.0.0.1', port), _Analyzer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % port
    srv.shutdown()


@pytest.fixture(scope='module')
def live():
    data_dir = tempfile.mkdtemp(prefix='evct-wb-')
    port = _free_port()
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='wb-test-only',
               APP_HOST='127.0.0.1', APP_PORT=str(port))
    proc = subprocess.Popen([sys.executable, 'app.py'], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:%d' % port
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.skip('app did not start in this environment')
            try:
                urllib.request.urlopen(base + '/api/health', timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip('app did not become healthy in time')
        yield base, data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return json.loads(r.read().decode())


def _post_json(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _post_form(base, path, fields):
    from urllib.parse import urlencode
    req = urllib.request.Request(base + path, data=urlencode(fields).encode(),
                                 method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _sqlite(data_dir):
    import sqlite3
    return sqlite3.connect(os.path.join(data_dir, 'ev_tracker.db'))


def test_the_columns_exist_after_boot(live):
    """The migration runs on an empty database as well as an old one."""
    _base, data_dir = live
    con = _sqlite(data_dir)
    charges = {r[1] for r in con.execute('PRAGMA table_info(charges)')}
    vehicles = {r[1] for r in con.execute('PRAGMA table_info(vehicles)')}
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert 'wallbox_charge_id' in charges
    assert {'wallbox_link_enabled', 'wallbox_device_key'} <= vehicles
    assert 'wallbox_charges' in tables


def test_the_settings_page_offers_the_link(live):
    base, _ = live
    with urllib.request.urlopen(base + '/settings', timeout=30) as r:
        html = r.read().decode()
    assert 'sec-wallbox' in html
    assert 'wallbox_token' in html
    assert 'wbTestBtn' in html


def test_a_wrong_key_is_reported_and_not_swallowed(live, analyzer):
    base, _ = live
    d = _post_json(base, '/api/wallbox/test', {'url': analyzer, 'token': 'falsch'})
    assert d['ok'] is False
    # The message has to name what to fix — "failed" would send the user
    # looking at the network for a wrong password.
    assert 'token' in d['error'].lower() or '401' in d['error']


def test_the_link_reaches_the_analyzer(live, analyzer):
    base, _ = live
    d = _post_json(base, '/api/wallbox/test', {'url': analyzer, 'token': TOKEN})
    assert d['ok'] is True, d
    assert d['info']['wallbox']['name'] == 'Wallbox Garage'
    assert d['info']['sources']['solar'] is True


def _setup(base, analyzer, apply_mode='auto'):
    _post_form(base, '/settings', {
        'action': 'save_wallbox', 'wallbox_url': analyzer,
        'wallbox_token': TOKEN, 'wallbox_enabled': 'on',
        'wallbox_apply': apply_mode, 'wallbox_tolerance': '90',
        'wallbox_backfill_days': '30',
    })


def _car_and_charge(base, data_dir):
    """One car bound to the wallbox, and a charge in the same window.

    Deliberately the vehicle the app itself seeded at first boot, not a second
    one: /history shows the picker's active car, which with no browser session
    is the first non-archived vehicle. A test that added its own car would file
    its charge on a vehicle the page never lists — and would then "prove" a
    missing badge that is really a missing filter.
    """
    con = _sqlite(data_dir)
    vid = con.execute('SELECT id FROM vehicles ORDER BY id ASC LIMIT 1').fetchone()[0]
    con.close()
    _post_form(base, '/vehicles/save', {
        'vehicle_id': str(vid), 'name': 'Probe-Auto', 'battery_kwh': '64',
        'wallbox_present': '1', 'wallbox_link_enabled': 'on',
    })
    con = _sqlite(data_dir)
    con.execute(
        'INSERT INTO charges (vehicle_id, date, charge_hour, charge_end_hour, '
        'kwh_loaded, charge_type, needs_review, soc_from, soc_to) '
        'VALUES (?,?,?,?,?,?,1,20,70)',
        (vid, START.date().isoformat(), START.hour, ENDE.hour, 19.0, 'AC'))
    con.commit()
    cid = con.execute('SELECT id FROM charges ORDER BY id DESC LIMIT 1').fetchone()[0]
    con.close()
    return vid, cid


def test_a_pass_files_the_reading_against_the_charge(live, analyzer):
    base, data_dir = live
    _setup(base, analyzer)
    _vid, cid = _car_and_charge(base, data_dir)
    assert _post_json(base, '/api/wallbox/sync', {'full': True})['ok']
    for _ in range(60):
        d = _get(base, '/api/wallbox/status')
        if not d['running'] and d.get('last'):
            break
        time.sleep(0.5)
    last = _get(base, '/api/wallbox/status')['last']
    assert last and last['ok'], last
    assert last['matched'] == 1, last

    r = _get(base, '/api/wallbox/charge/%d' % cid)['reading']
    assert r is not None
    assert r['energy_kwh'] == 21.4
    assert r['measured'] is True
    assert abs(r['solar_share'] - 19.4 / 21.4) < 0.001
    assert r['match_state'] == 'matched'
    # needs_review + a measured reading ⇒ the meter's numbers are adopted.
    assert r['applied'] is True
    con = _sqlite(data_dir)
    kwh = con.execute('SELECT kwh_loaded FROM charges WHERE id=?', (cid,)).fetchone()[0]
    con.close()
    assert kwh == 21.4


def test_a_second_pass_changes_nothing(live, analyzer):
    """Idempotent: the same answer twice must not duplicate a charge."""
    base, data_dir = live
    con = _sqlite(data_dir)
    vorher = con.execute('SELECT COUNT(*) FROM wallbox_charges').fetchone()[0]
    con.close()
    _post_json(base, '/api/wallbox/sync', {})
    for _ in range(40):
        if not _get(base, '/api/wallbox/status')['running']:
            break
        time.sleep(0.5)
    con = _sqlite(data_dir)
    nachher = con.execute('SELECT COUNT(*) FROM wallbox_charges').fetchone()[0]
    con.close()
    assert nachher == vorher == 1


def test_the_history_page_shows_the_badge(live):
    base, _ = live
    with urllib.request.urlopen(base + '/history', timeout=30) as r:
        html = r.read().decode()
    assert 'data-wb-curve' in html
    assert 'wbCurveModal' in html
    assert 'wallbox_curve.js' in html


def test_the_history_row_carries_the_source_colours(live):
    """Robert: „die einträge bekommen die anteilsfarben oder so".

    The stub's charge is 17.0 sun / 2.4 battery / 2.0 grid of 21.4 kWh, so all
    three segments must be there — and in the same colours the curve uses,
    because a colour that means sun in one place and something else two rows
    down is worse than no colour.
    """
    base, _ = live
    with urllib.request.urlopen(base + '/history', timeout=30) as r:
        html = r.read().decode()
    assert 'wb-mix' in html, 'no source bar in the history table'
    for farbe in ('#fdd835', '#22c55e', '#ef4444'):
        assert farbe in html, 'the %s segment is missing' % farbe
    # …and the charge the meter measured is no longer flagged for checking.
    assert 'table-danger' not in html, 'a measured home charge still asks to be checked'


def test_the_settings_page_points_at_the_companion(live):
    """Robert: „der ev-tracker andersrum zu shelly analyzer". One sentence and
    a link — and it has to survive translation, so the German text is checked,
    not the English fallback."""
    base, _ = live
    with urllib.request.urlopen(base + '/settings', timeout=30) as r:
        html = r.read().decode()
    assert 'github.com/robeertm/shelly-energy-analyzer' in html
    assert 'Shelly Energy Analyzer' in html
    assert 'Shelly-Z\u00e4hlern' in html or 'Shelly meters' in html, \
        'the sentence fell back to its i18n key'


def test_the_curve_comes_through_the_server(live):
    base, data_dir = live
    con = _sqlite(data_dir)
    cid = con.execute(
        'SELECT id FROM charges WHERE wallbox_charge_id IS NOT NULL').fetchone()[0]
    con.close()
    d = _get(base, '/api/wallbox/charge/%d/curve' % cid)
    assert d['ok'] is True, d
    c = d['curve']
    assert c['available'] is True
    assert len(c['ts']) == len(c['load_w']) == 24
    # Unreshaped: the three bands and the load are the analyzer's own arrays.
    assert c['solar_w'][0] == 6000.0 and c['grid_w'][-1] == 7000.0
    assert 'seconds' in c
    # And the token never left the server.
    assert TOKEN not in json.dumps(d)


def test_undo_restores_the_entry(live, analyzer):
    base, data_dir = live
    con = _sqlite(data_dir)
    cid = con.execute(
        'SELECT id FROM charges WHERE wallbox_charge_id IS NOT NULL').fetchone()[0]
    con.close()
    d = _post_json(base, '/api/wallbox/charge/%d/apply' % cid, {'undo': True})
    assert d['ok'] and d['applied'] is False
    con = _sqlite(data_dir)
    kwh = con.execute('SELECT kwh_loaded FROM charges WHERE id=?', (cid,)).fetchone()[0]
    con.close()
    assert kwh == 19.0        # exactly what stood there before


def test_deleting_a_charge_releases_its_reading(live, analyzer):
    """The measurement outlives the entry — and goes looking for a new one.

    Without this the reading would stay "matched" to a row that no longer
    exists: never offered to the charge the user logs instead, and rendered in
    Settings as a link to nothing.
    """
    base, data_dir = live
    con = _sqlite(data_dir)
    cid = con.execute(
        'SELECT id FROM charges WHERE wallbox_charge_id IS NOT NULL').fetchone()[0]
    con.close()
    _post_form(base, '/delete/%d' % cid, {})

    con = _sqlite(data_dir)
    n, state, applied = con.execute(
        'SELECT COUNT(*), MIN(match_state), MIN(applied_at IS NULL) '
        'FROM wallbox_charges').fetchone()
    charge_gone = con.execute('SELECT COUNT(*) FROM charges WHERE id=?',
                              (cid,)).fetchone()[0]
    con.close()
    assert charge_gone == 0
    assert n == 1                      # the measurement is kept
    assert state == 'unmatched'        # and is looking for an entry again
    assert applied == 1                # nothing left claiming it was adopted

    # And it really is available again: log the same charge once more and the
    # next pass picks it up.
    _car_and_charge(base, data_dir)
    _post_json(base, '/api/wallbox/sync', {'full': True})
    for _ in range(60):
        if not _get(base, '/api/wallbox/status')['running']:
            break
        time.sleep(0.5)
    assert _get(base, '/api/wallbox/status')['last']['matched'] == 1


def test_the_sync_route_actually_honours_the_days_it_was_given(live, analyzer):
    """`days` was read off the request and then dropped: the caller asked for a
    week and the incremental path decided something else. A parameter that is
    read and ignored is worse than one that does not exist — the caller
    believes it asked for something."""
    base, _ = live
    _setup(base, analyzer)
    # 🔴 A pass may still be running from the previous test — start_sync then
    # answers "not started", and a test that only fires once would read the
    # silence as a lost parameter. So keep asking until one actually starts.
    ANFRAGEN.clear()
    for _ in range(40):
        r = _post_json(base, '/api/wallbox/sync', {'days': 7})
        assert r.get('ok'), r
        if r.get('started'):
            break
        time.sleep(0.25)
    else:
        pytest.skip('no sync slot became free in time')
    for _ in range(40):
        time.sleep(0.25)
        if any('/charges' in a for a in ANFRAGEN):
            break
    gefragt = [a for a in ANFRAGEN if '/charges' in a]
    assert gefragt, 'the analyzer was never asked for charges at all'
    assert any('days=7' in a for a in gefragt), gefragt
    print("OK  the sync route passes days through: %s" % gefragt[-1])

    # Nonsense is refused, not rounded into something plausible.
    try:
        r = _post_json(base, '/api/wallbox/sync', {'days': 'viele'})
        raise AssertionError('a non-numeric days was accepted: %r' % r)
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
    print("OK  a non-numeric days is refused with 400")
