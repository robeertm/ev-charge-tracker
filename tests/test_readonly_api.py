# -*- coding: utf-8 -*-
"""The read-only vehicle endpoint: its own token, nothing else opens.

``GET /api/readonly/vehicle`` lets a program read the battery level without
the web login. That makes two promises, and both are tested here against
the real app on a throwaway database:

* **It reads and it tells only what it should.** No coordinates, no VIN, no
  credential, no place name — even when the database holds all of them.
* **It opens exactly one door.** The login exemption is an exact path; a
  route that merely starts the same way stays behind the login, and the
  read-only token does not unlock it either.

The token is created with the shell command a container user runs, so the
command's own promise — the token on stdout and nothing else — is tested
on the same path.

Runs the app in a subprocess because ``config.DATA_DIR`` is fixed at import
time (same pattern as test_legacy_config_mirror.py).
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VIN = 'TESTVIN0000000042'
SECRET_PW = 'very-secret-cloud-password'
LAT, LON = 51.123456, 13.654321
PLACE = 'Probestrasse 7'


def _env(data_dir):
    return dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='ro-test')


def _run(data_dir, script: str) -> dict:
    proc = subprocess.run([sys.executable, '-c', _PREAMBLE + script], cwd=ROOT,
                          env=_env(data_dir), capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    last = [ln for ln in proc.stdout.strip().split('\n') if ln.startswith('{')]
    assert last, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(last[-1])


def _cli(data_dir, *args):
    return subprocess.run([sys.executable, '-m', 'services.readonly_token', *args],
                          cwd=ROOT, env=_env(data_dir), capture_output=True,
                          text=True, timeout=60)


_PREAMBLE = """
import json, sys
sys.path.insert(0, %r)
from app import create_app
from models.database import db, AppConfig, Vehicle, VehicleSync, Charge
from services import readonly_api as ro
app = create_app()
client = app.test_client()

def get(path, token=None, scheme='Bearer'):
    h = {}
    if token is not None:
        h['Authorization'] = scheme + ' ' + token
    r = client.get(path, headers=h)
    return {'code': r.status_code, 'body': r.get_data(as_text=True),
            'location': r.headers.get('Location', ''),
            'retry_after': r.headers.get('Retry-After'),
            'cache': r.headers.get('Cache-Control')}
""" % ROOT


_SEED = """
from datetime import datetime, date, timedelta
from services.auth_service import set_credentials
from services.vehicle.base import VehicleStatus
from app import _save_vehicle_sync
with app.app_context():
    set_credentials('owner', 'owner-password-123')   # web login ON
    # The fake credentials below must never reach a real vendor: the
    # background sync stays off in every later run on this database.
    AppConfig.set('vehicle_sync_enabled', 'false')
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.name = 'Daily car'; v.brand = 'Kia'; v.model = 'Niro EV'
    v.api_brand = 'kia'; v.api_username = 'someone@example.com'
    v.api_password = %(pw)r; v.api_vin = %(vin)r; v.api_pin = '1234'
    v.auto_sync = False
    old = Vehicle(name='Old car', brand='Renault', model='Zoe', is_archived=True)
    db.session.add(old)
    db.session.commit()
    _save_vehicle_sync(VehicleStatus(
        soc_percent=67, odometer_km=40123, estimated_range_km=251,
        is_charging=True, is_plugged_in=True, charge_power_kw=7.4,
        charge_limit_ac=80, location_lat=%(lat)r, location_lon=%(lon)r,
        last_updated='2026-10-25T17:58:00+02:00'), 64.0, raw_json='{}',
        vehicle_id=v.id)
    db.session.add(Charge(vehicle_id=v.id, date=date(2026, 10, 24),
                          charge_hour=22, charge_end_hour=3, kwh_loaded=21.5,
                          soc_from=40, soc_to=75, operator='Zuhause / privat',
                          location_lat=%(lat)r, location_lon=%(lon)r,
                          location_name=%(place)r, eur_per_kwh=0.31))
    db.session.add(Charge(vehicle_id=old.id, date=date(2026, 9, 1),
                          charge_hour=12, kwh_loaded=30.0, soc_from=20,
                          soc_to=80, operator='IONITY', charge_type='DC'))
    db.session.commit()
""" % {'pw': SECRET_PW, 'vin': VIN, 'lat': LAT, 'lon': LON, 'place': PLACE}


def _new_token(data_dir):
    proc = _cli(data_dir, 'new')
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.split('\n')
    assert len(lines) == 2 and lines[1] == '', (
        'stdout must carry the token and nothing else: %r' % proc.stdout)
    token = lines[0]
    assert len(token) >= 40
    assert token not in proc.stderr
    return token


def _fresh():
    d = tempfile.mkdtemp(prefix='evct-ro-')
    _run(d, _SEED + "print(json.dumps({'ok': True}))")
    return d


# ── switched off ──────────────────────────────────────────────────────
def test_without_a_token_the_endpoint_does_not_exist():
    d = _fresh()
    out = _run(d, "print(json.dumps({'a': get('/api/readonly/vehicle'),"
                  " 'b': get('/api/readonly/vehicle', 'anything')}))")
    assert out['a']['code'] == 404
    assert out['b']['code'] == 404
    assert _cli(d, 'status').stdout.strip() == 'off'


# ── the token ─────────────────────────────────────────────────────────
def test_missing_or_wrong_token_is_refused():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, """
print(json.dumps({
  'none': get('/api/readonly/vehicle'),
  'wrong': get('/api/readonly/vehicle', 'x' * 43),
  'basic': get('/api/readonly/vehicle', %r, scheme='Basic'),
  'empty': get('/api/readonly/vehicle', ''),
}))""" % token)
    for k in ('none', 'wrong', 'basic', 'empty'):
        assert out[k]['code'] == 401, (k, out[k])


def test_the_right_token_reads_with_the_web_login_switched_on():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, "print(json.dumps({'r': get('/api/readonly/vehicle', %r)}))" % token)
    assert out['r']['code'] == 200, out['r']
    assert out['r']['cache'] == 'no-store'
    body = json.loads(out['r']['body'])
    assert body['ok'] is True
    import re
    with open(os.path.join(ROOT, 'config.py'), encoding='utf-8') as f:
        version = re.search(r"APP_VERSION = '([^']+)'", f.read()).group(1)
    assert body['version'] == version
    names = [v['name'] for v in body['vehicles']]
    assert names == ['Daily car', 'Old car']
    car, old = body['vehicles']
    assert car['active'] is True and car['archived'] is False
    assert old['active'] is False and old['archived'] is True
    assert car['brand'] == 'Kia' and car['model'] == 'Niro EV'
    assert car['soc_pct'] == 67
    assert car['range_km'] == 251
    assert car['charging'] is True
    assert car['plugged_in'] is True
    assert car['charge_power_kw'] == 7.4
    assert car['target_soc_pct'] == 80
    assert car['odometer_km'] == 40123
    assert car['data_ts'] == '2026-10-25T17:58:00+02:00'
    assert car['last_sync_ok'] is True and car['last_sync_ts']
    lc = car['last_charge']
    assert lc['kwh'] == 21.5 and lc['soc_from'] == 40 and lc['soc_to'] == 75
    assert lc['location_kind'] == 'home'
    assert lc['start'].startswith('2026-10-24T22:00')
    assert lc['end'].startswith('2026-10-25T03:00'), 'end past midnight'
    assert set(lc) == {'start', 'end', 'kwh', 'soc_from', 'soc_to', 'location_kind'}
    assert old['last_charge']['location_kind'] == 'public'
    # A car the app never polled has nothing to report — null, not a guess.
    assert old['soc_pct'] is None and old['last_sync_ok'] is None
    assert old['last_sync_ts'] is None and old['plugged_in'] is None


def test_the_answer_carries_no_position_identity_or_secret():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, "print(json.dumps({'r': get('/api/readonly/vehicle', %r)}))" % token)
    text = out['r']['body']
    body = json.loads(text)

    keys = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.add(k.lower())
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(body)
    for bad in ('gps', 'lat', 'lon', 'vin', 'password', 'token', 'pin',
                'username', 'address', 'location_name', 'operator', 'eur',
                'cost', 'plate'):
        hit = [k for k in keys if bad in k]
        assert not hit, f'forbidden field {bad!r} in answer: {hit}'
    for value in (VIN, SECRET_PW, 'someone@example.com', PLACE, token,
                  str(LAT), str(LON), '51.12', '13.65'):
        assert value not in text, f'{value!r} leaked into the answer'


def test_a_new_token_replaces_the_old_and_off_switches_it_off():
    d = _fresh()
    first = _new_token(d)
    second = _new_token(d)
    assert first != second
    status = _cli(d, 'status')
    assert status.stdout.startswith('on')
    assert second not in status.stdout and second not in status.stderr
    out = _run(d, "print(json.dumps({'old': get('/api/readonly/vehicle', %r),"
                  " 'new': get('/api/readonly/vehicle', %r)}))" % (first, second))
    assert out['old']['code'] == 401
    assert out['new']['code'] == 200
    assert _cli(d, 'off').returncode == 0
    out = _run(d, "print(json.dumps({'r': get('/api/readonly/vehicle', %r)}))" % second)
    assert out['r']['code'] == 404
    assert _cli(d, 'status').stdout.strip() == 'off'


def test_only_the_hash_is_stored():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, """
with app.app_context():
    vals = [r.value or '' for r in AppConfig.query.all()]
print(json.dumps({'plain': any(%r in v for v in vals),
                  'hashed': ro.hash_token(%r) in vals}))""" % (token, token))
    assert out['plain'] is False
    assert out['hashed'] is True


def test_the_cli_never_creates_a_database():
    d = tempfile.mkdtemp(prefix='evct-ro-empty-')
    proc = _cli(d, 'new')
    assert proc.returncode != 0
    assert proc.stdout == ''
    assert not os.path.exists(os.path.join(d, 'ev_tracker.db'))


# ── the brake ─────────────────────────────────────────────────────────
def test_the_brake_stops_guessing_and_also_holds_the_right_token():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, """
codes = [get('/api/readonly/vehicle', 'guess-%%d' %% i)['code'] for i in range(10)]
braked = get('/api/readonly/vehicle', %r)
ro.reset_brake()
after = get('/api/readonly/vehicle', %r)
print(json.dumps({'codes': codes, 'braked': braked, 'after': after}))""" % (token, token))
    assert out['codes'] == [401] * 10
    assert out['braked']['code'] == 429
    assert int(out['braked']['retry_after']) >= 1
    assert out['after']['code'] == 200


# ── the exemption is one path, not a prefix ───────────────────────────
def test_the_login_exemption_covers_exactly_one_path():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, """
res = {}
for p in ('/api/readonly/vehicleX', '/api/readonly/other',
          '/api/readonly/vehicle/', '/api/readonly/', '/api/readonly'):
    res[p] = {'anon': get(p), 'token': get(p, %r)}
res['post'] = client.post('/api/readonly/vehicleX').status_code
res['dash'] = get('/', %r)
print(json.dumps(res))""" % (token, token))
    for p in ('/api/readonly/vehicleX', '/api/readonly/other',
              '/api/readonly/vehicle/', '/api/readonly/', '/api/readonly'):
        for who in ('anon', 'token'):
            r = out[p][who]
            assert r['code'] == 302 and '/login' in r['location'], (p, who, r)
    assert out['post'] == 401
    assert out['dash']['code'] == 302 and '/login' in out['dash']['location']


# ── the last-poll record ──────────────────────────────────────────────
def test_a_failed_poll_is_reported_and_keeps_the_last_good_values():
    d = _fresh()
    token = _new_token(d)
    out = _run(d, """
with app.app_context():
    vid = Vehicle.query.order_by(Vehicle.id).first().id
    ro.note_poll_failed(vid)
r = get('/api/readonly/vehicle', %r)
print(json.dumps({'r': r}))""" % token)
    car = json.loads(out['r']['body'])['vehicles'][0]
    assert car['last_sync_ok'] is False
    assert car['soc_pct'] == 67 and car['plugged_in'] is True


def test_an_unchanged_poll_still_counts_as_a_poll():
    """A row is only written when a value changes; the poll record is
    written every time, so last_sync_ts follows the polls, not the rows."""
    d = _fresh()
    out = _run(d, """
from services.vehicle.base import VehicleStatus
from app import _save_vehicle_sync
with app.app_context():
    vid = Vehicle.query.order_by(Vehicle.id).first().id
    AppConfig.set(ro.LAST_POLL_KEY.format(vid=vid),
                  json.dumps({'at': '2000-01-01T00:00:00', 'ok': False}))
    n0 = VehicleSync.query.filter_by(vehicle_id=vid).count()
    _save_vehicle_sync(VehicleStatus(
        soc_percent=67, odometer_km=40123, estimated_range_km=251,
        is_charging=False, is_plugged_in=False, location_lat=%r,
        location_lon=%r), 64.0, raw_json='{}', vehicle_id=vid)
    n1 = VehicleSync.query.filter_by(vehicle_id=vid).count()
    rec = json.loads(AppConfig.get(ro.LAST_POLL_KEY.format(vid=vid)))
print(json.dumps({'n0': n0, 'n1': n1, 'rec': rec}))""" % (LAT, LON))
    assert out['n0'] == out['n1'], 'test premise: identical values write no row'
    assert out['rec']['ok'] is True
    assert not out['rec']['at'].startswith('2000')
    assert out['rec']['charging'] is False and out['rec']['plugged_in'] is False
    assert 'location_lat' not in out['rec'] and 'lat' not in json.dumps(out['rec'])
