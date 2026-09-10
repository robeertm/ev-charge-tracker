# -*- coding: utf-8 -*-
"""With two cars, the picker has to decide which one is asked.

A customer added a second vehicle — an Enyaq next to a Kona — and
reported that switching between them on the dashboard kept showing the
Kona. Reproduced on two dummy cars before anything was changed:

    picker = Kona   → /api/vehicle/status → 12 V 56 %, SoH 90.7, 37 750 km
    picker = Enyaq  → /api/vehicle/status → 12 V 56 %, SoH 90.7, 37 750 km

The reason was worse than a wrong label. Every live-data route read its
brand and credentials from the flat ``vehicle_api_*`` AppConfig keys,
which mirror the FIRST vehicle only — and then stamped the row it got
back with the id of the car the picker pointed at. So the Kona's
odometer was written into the Enyaq's history, where the trip
derivation reads it.

These tests drive a real server against a throwaway database with two
vehicles on two distinguishable connectors, so "which car answered" is
readable straight off the response.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# Two connectors that need no network and no SDK, each answering with
# numbers that could only have come from itself.
STUB = '''
import sys
sys.path.insert(0, %(root)r)
from services.vehicle.base import VehicleConnector, VehicleStatus
from services.vehicle.registry import register
try:                       # only exists once brands can describe themselves
    from services.vehicle.registry import describe
except ImportError:        # older tree: the test must still RUN and fail
    def describe(*_):
        pass


class _Stub(VehicleConnector):
    NAME = 'STUB'
    SOC = 0
    ODO = 0
    V12 = 0

    def authenticate(self):
        return True

    def test_connection(self):
        return True

    def get_status(self, force=False):
        return VehicleStatus(soc_percent=self.SOC, odometer_km=self.ODO,
                             battery_12v_percent=self.V12,
                             vehicle_name=self.NAME, vehicle_model=self.NAME,
                             raw_data={'stub': self.NAME})

    @staticmethod
    def credential_fields():
        return [{"key": "username", "label": "u", "type": "text",
                 "label_key": "cred.email"},
                {"key": "password", "label": "p", "type": "password",
                 "label_key": "cred.password"}]

    @classmethod
    def brand_name(cls):
        return cls.NAME


class StubA(_Stub):
    NAME = 'KONA'
    SOC = 61
    ODO = 37750
    V12 = 56


class StubB(_Stub):
    NAME = 'ENYAQ'
    SOC = 47
    ODO = 12345
    V12 = 88


for _k, _c in (('stub_a', StubA), ('stub_b', StubB)):
    describe(_k, _c)
    register(_k, _c)

from app import create_app
from config import Config

create_app().run(host=Config.APP_HOST, port=Config.APP_PORT, debug=False)
'''


@pytest.fixture(scope='module')
def zwei_autos():
    data_dir = tempfile.mkdtemp(prefix='evct-zweiautos-')
    port = _free_port()
    stub_path = os.path.join(data_dir, 'stub_server.py')
    with open(stub_path, 'w') as fh:
        fh.write(STUB % {'root': ROOT})
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='two-car-test',
               APP_HOST='127.0.0.1', APP_PORT=str(port))
    proc = subprocess.Popen([sys.executable, stub_path], cwd=ROOT, env=env,
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
            raise AssertionError(
                'the instance never became healthy — a skip here would '
                'hide exactly the defect these tests exist for')

        db = os.path.join(data_dir, 'ev_tracker.db')
        con = sqlite3.connect(db)
        con.execute("UPDATE vehicles SET name='Kona', api_brand='stub_a',"
                    " api_username='kona@example.com', api_password='x',"
                    " is_archived=0 WHERE id=(SELECT MIN(id) FROM vehicles)")
        con.execute("INSERT INTO vehicles (name, api_brand, api_username,"
                    " api_password, auto_sync, is_archived, remote_control_enabled)"
                    " VALUES ('Enyaq', 'stub_b', 'enyaq@example.com', 'x', 0, 0, 0)")
        # The legacy AppConfig mirror, as a real install has it: it
        # describes the FIRST vehicle. Without these rows the old code
        # would fail here for the wrong reason ("not configured") instead
        # of showing what it really did — answer with the first car.
        for key, wert in (('vehicle_api_brand', 'stub_a'),
                          ('vehicle_api_username', 'kona@example.com'),
                          ('vehicle_api_password', 'x'),
                          ('vehicle_api_region', 'EU'),
                          ('car_model', 'Kona')):
            con.execute('INSERT OR REPLACE INTO app_config (key, value) '
                        'VALUES (?, ?)', (key, wert))
        con.commit()
        ids = [r[0] for r in con.execute(
            'SELECT id FROM vehicles ORDER BY id').fetchall()]
        con.close()
        yield base, ids, db
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


class _Sitzung:
    """A browser-ish client: keeps the session cookie the picker writes."""

    def __init__(self, base):
        import http.cookiejar
        self.base = base
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.open(base + '/')

    def waehle(self, vid):
        req = urllib.request.Request(
            self.base + '/api/vehicles/select', method='POST',
            data=json.dumps({'vehicle_id': vid}).encode(),
            headers={'Content-Type': 'application/json'})
        with self.op.open(req) as r:
            return json.loads(r.read())

    def status(self):
        try:
            with self.op.open(self.base + '/api/vehicle/status') as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def seite(self, pfad='/'):
        with self.op.open(self.base + pfad) as r:
            return r.read().decode('utf-8', 'replace')


def test_the_picked_vehicle_is_the_one_that_gets_asked(zwei_autos):
    base, ids, _ = zwei_autos
    s = _Sitzung(base)

    s.waehle(ids[0])
    code_a, kona = s.status()
    s.waehle(ids[1])
    code_b, enyaq = s.status()

    assert (code_a, code_b) == (200, 200), (kona, enyaq)
    assert kona['odometer'] == 37750 and kona['battery_12v'] == 56
    assert enyaq['odometer'] == 12345 and enyaq['battery_12v'] == 88, (
        'the second car answered with the first cars numbers — the '
        'picker is choosing the stamp but not the connector')


def test_a_sync_row_is_never_credited_to_a_car_it_did_not_come_from(zwei_autos):
    """The damaging half: one car's mileage in the other car's history."""
    base, ids, db = zwei_autos
    s = _Sitzung(base)
    s.waehle(ids[1])
    s.status()

    con = sqlite3.connect(db)
    zeilen = con.execute(
        'SELECT vehicle_id, raw_json FROM vehicle_syncs '
        'WHERE raw_json LIKE "%stub%"').fetchall()
    con.close()
    assert zeilen
    erwartet = {ids[0]: 'KONA', ids[1]: 'ENYAQ'}
    for vid, raw in zeilen:
        assert erwartet[vid] in raw, (
            f'sync row credited to vehicle {vid} but fetched from {raw}')


def test_each_vehicle_has_its_own_daily_api_budget(zwei_autos):
    """Refreshing one car must not spend the other car's 200 calls."""
    base, ids, db = zwei_autos
    s = _Sitzung(base)
    s.waehle(ids[1])
    s.status()

    # Read the counters, not the returned number: with one shared
    # counter the returned value also goes up, so it proves nothing.
    # The background sync has kept per-vehicle keys since v2.29; the
    # dashboard's manual refresh has to use the same ones.
    con = sqlite3.connect(db)
    schluessel = {r[0] for r in con.execute(
        "SELECT key FROM app_config WHERE key LIKE '%api_counter'").fetchall()}
    con.close()
    assert f'vehicle_{ids[1]}_api_counter' in schluessel, (
        'refreshing the second car counted into the first car\u2019s '
        f'budget \u2014 counters present: {sorted(schluessel)}')


def test_the_dashboard_cache_key_names_the_vehicle(zwei_autos):
    """One shared localStorage slot painted the other car's values from
    cache on load, before any request went out."""
    base, ids, _ = zwei_autos
    s = _Sitzung(base)
    s.waehle(ids[0])
    seite_a = s.seite('/')
    s.waehle(ids[1])
    seite_b = s.seite('/')

    def schluessel(html):
        import re
        m = re.search(r"CACHE_VEHICLE = '([^']*)'", html)
        return m.group(1) if m else None

    assert schluessel(seite_a) == str(ids[0])
    assert schluessel(seite_b) == str(ids[1])
    assert schluessel(seite_a) != schluessel(seite_b)
