# -*- coding: utf-8 -*-
"""Kia/Hyundai/Genesis via the official Pleos API, and the 9035 block (v3.0.157).

On 14 Oct 2026 Kia and Hyundai Europe started refusing every third-party
sign-in: the password check passes, the token exchange right after it
answers ``HTTP 400 {"code": 9035, "message": "Unusual activity has been
detected..."}``. The app then showed a five-point *password* checklist, so
an owner with a correct password doubted it, and the loop kept knocking
every ten minutes. These tests pin:

  * the honest message and the pause for that block;
  * the mapping of the documented Pleos responses (shapes copied from
    document.pleos.ai) to ours — units, sentinels, positions, locks;
  * the client's token renewal, back-off and one-part-failing behaviour;
  * the switch-over route, which must prove the key BEFORE it writes.

No network: the transport is replaced.
"""
import io
import json
import os
import subprocess
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _api():
    from services.vehicle import pleos_api
    return pleos_api


# ── documented payloads ───────────────────────────────────────────────
VIN = 'KNATESTVIN0000001'
TS = '2026-10-14T09:10:00Z'

DOCS = {
    'batteries': {'charge': {'plugin': 'connected', 'status': 'fastCharging', 'charging': True,
                             'stateOfCharge': 30.5, 'batteryCapacity': 273960,
                             'targetStateOfCharge': {'standard': 80, 'quick': 90},
                             'remainTime': 54}, 'timestamp': TS},
    'powertrains': {'engine': {'fuelLevel': 0}, 'batteries': [{'type': 'main', 'remainRatio': 30.5}],
                    'distanceToEmpties': [{'type': 'EV', 'value': '106', 'unit': 'km'}],
                    'timestamp': TS},
    'driving': {'ignition': {'status': 'off', 'ignitionDetails': []}, 'sleepMode': 'on',
                'odometer': {'value': 6549.9, 'unit': 'km'},
                'transmission': {'parkingPosition': True}, 'timestamp': TS},
    'locations': {'location': {'latitude': 49.9749, 'longitude': 8.4412, 'altitude': 10,
                               'heading': 180, 'speed': {'value': 0, 'unit': 'kph'}, 'type': 0},
                  'timestamp': '2026-10-14T09:11:00Z'},
    'status': {'doors': [{'left': 'locked', 'right': 'locked'}, {'left': 'locked', 'right': 'locked'}],
               'hood': 'closed', 'trunk': 'closed', 'sunroof': 'closed',
               'warnings': ['washFluid'],
               'climateControl': {'mode': 'auto', 'status': 'off', 'speed': -1,
                                  'temperature': {'value': 21, 'unit': 'C'}},
               'windshield': {'defrost': 'off'}, 'steeringWheelHeat': 'off',
               'outsideTemperature': {'value': 12, 'unit': 'C'}, 'timestamp': TS},
}
CARS = [{'vin': VIN, 'nickname': 'Niro', 'name': 'DE', 'type': 'EV', 'sellname': 'Niro EV'}]


class _Resp(io.BytesIO):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(status, payload, headers=None):
    return urllib.error.HTTPError('https://api.pleos.ai/x', status, 'err', headers or {},
                                  io.BytesIO(json.dumps(payload).encode()))


class _Server:
    """A fake Pleos: token endpoint, consent list, snapshot endpoints."""

    def __init__(self, docs=None, cars=None, token_error=None):
        self.docs = dict(DOCS if docs is None else docs)
        self.cars = CARS if cars is None else cars
        self.token_error = token_error
        self.calls = []
        self.tokens = 0
        self.expire_next = 0       # how many GETs answer 403 "expired"
        self.errors = {}           # endpoint -> (status, payload)

    def __call__(self, req, timeout=None):
        url = req.full_url
        path = url.split('api.pleos.ai', 1)[1]
        self.calls.append((req.get_method(), path, dict(req.header_items())))
        assert req.get_header('Brand') in ('kia', 'hyundai', 'genesis')
        if path == '/v1/auth/personal-token':
            if self.token_error:
                raise _http_error(*self.token_error)
            self.tokens += 1
            return _Resp({'data': {'accessToken': f'tok{self.tokens}'}, 'meta': {'code': 200}})
        assert req.get_header('Authorization', '').startswith('Bearer tok')
        if self.expire_next:
            self.expire_next -= 1
            raise _http_error(403, {'error': {'code': 403, 'message': 'Forbidden'}})
        if path == '/v1/vehicles/consent':
            return _Resp({'data': {'vehicles': self.cars}, 'meta': {'code': 200}})
        ep = path.split(f'/v1/vehicles/{VIN}/', 1)[1]
        if ep in self.errors:
            raise _http_error(*self.errors[ep])
        if ep not in self.docs:
            raise _http_error(404, {'error': {'code': '4045', 'message': 'No data'}})
        return _Resp({'data': self.docs[ep], 'meta': {'code': 200, 'success': 'true'}})


@pytest.fixture
def server(monkeypatch):
    api = _api()
    api.reset_state()
    s = _Server()
    monkeypatch.setattr(api.urllib.request, 'urlopen', s)
    yield s
    api.reset_state()


def _kia(vin=''):
    from services.vehicle.connector_pleos import KiaPleosConnector
    return KiaPleosConnector({'username': 'client-id', 'password': 'client-secret', 'vin': vin})


# ── mapping ───────────────────────────────────────────────────────────

def test_the_documented_responses_map_to_our_status(server):
    s = _kia().get_status()
    assert s.soc_percent == 30                     # 30.5 rounds half-to-even
    assert s.is_charging is True and s.is_plugged_in is True
    assert s.charge_limit_ac == 80 and s.charge_limit_dc == 90
    assert s.est_charge_duration_min == 54
    assert s.estimated_range_km == 106
    assert s.odometer_km == 6549                   # truncated like every connector
    assert (s.location_lat, s.location_lon) == (49.9749, 8.4412)
    assert s.is_locked is True and s.raw_data['lock_status_known'] is True
    assert s.last_updated == '2026-10-14T09:11:00Z'
    assert s.vehicle_name == 'Niro' and s.vehicle_model == 'Niro EV'
    assert s.raw_data['outside_temperature_c'] == 12
    assert s.raw_data['battery_capacity_wh'] == 273960
    assert s.distance_unit == 'km'


def test_secrets_never_travel_in_a_url_and_the_brand_header_is_set(server):
    _kia().get_status()
    for method, path, headers in server.calls:
        assert 'client-secret' not in path and 'client-id' not in path
        assert headers.get('Brand') == 'kia'
    token_call = [c for c in server.calls if c[1] == '/v1/auth/personal-token']
    assert len(token_call) == 1 and token_call[0][0] == 'POST'


def test_miles_metres_and_feet_arrive_as_kilometres(server):
    server.docs['driving'] = dict(DOCS['driving'], odometer={'value': 10000, 'unit': 'miles'})
    server.docs['powertrains'] = dict(DOCS['powertrains'],
                                      distanceToEmpties=[{'type': 'EV', 'value': '160934', 'unit': 'meter'}])
    s = _kia().get_status()
    assert s.odometer_km == 16093
    assert s.estimated_range_km == 161
    assert s.distance_unit == 'mi'


def test_an_unlocked_or_open_door_is_not_reported_as_locked(server):
    server.docs['status'] = dict(DOCS['status'], doors=[{'left': 'open', 'right': 'closed'},
                                                        {'left': 'locked', 'right': 'locked'}])
    s = _kia().get_status()
    assert s.is_locked is False
    assert s.front_left_door_open is True and s.front_right_door_open is False


def test_no_door_answer_keeps_the_safe_default_and_says_unknown(server):
    server.docs['status'] = dict(DOCS['status'], doors=[{'left': 'invalid', 'right': 'invalid'}])
    s = _kia().get_status()
    assert s.is_locked is True and s.raw_data['lock_status_known'] is False


@pytest.mark.parametrize('loc', [
    {'latitude': 0, 'longitude': 0, 'type': 0},           # Gulf of Guinea
    {'latitude': 49.9, 'longitude': 8.4, 'type': 1},      # Bessel, not WGS84
    {'latitude': 123, 'longitude': 8.4, 'type': 0},       # out of range
])
def test_a_position_we_cannot_draw_is_dropped_not_invented(server, loc):
    server.docs['locations'] = {'location': loc, 'timestamp': TS}
    s = _kia().get_status()
    assert s.location_lat is None and s.location_lon is None


def test_sentinels_are_not_readings(server):
    """remainTime 65535 is 'no value'; a SoC above 100 is not a SoC."""
    server.docs['batteries'] = {'charge': {'plugin': 'connected', 'charging': True,
                                           'stateOfCharge': 255, 'remainTime': 65535},
                                'timestamp': TS}
    s = _kia().get_status()
    assert s.est_charge_duration_min is None
    assert s.soc_percent == 30                     # falls back to remainRatio


def test_one_missing_part_does_not_cost_the_others(server):
    del server.docs['locations']                   # 4045 for this car
    server.errors['status'] = (500, {'error': {'code': '5000'}})
    s = _kia().get_status()
    assert s.soc_percent == 30 and s.odometer_km == 6549
    assert s.location_lat is None
    assert s.raw_data['api_errors'] == {'locations': '4045', 'status': '5000'}


def test_a_car_without_any_data_yet_says_so(server):
    from services.vehicle.pleos_api import PleosError
    server.docs = {}
    with pytest.raises(PleosError) as ex:
        _kia().get_status()
    assert ex.value.kind == 'no_data'


# ── client ────────────────────────────────────────────────────────────

def test_an_expired_token_answers_403_and_is_renewed_once(server):
    _kia().get_status()
    server.expire_next = 1
    s = _kia().get_status()
    assert s.soc_percent == 30
    assert server.tokens == 2


def test_a_lasting_403_is_not_authorized_not_an_endless_loop(server):
    from services.vehicle.pleos_api import PleosError
    server.expire_next = 99
    with pytest.raises(PleosError) as ex:
        _kia().verify_credentials()
    assert ex.value.kind == 'not_authorized'
    assert server.tokens == 2


@pytest.mark.parametrize('status,payload', [
    (400, {'error': {'code': '4002', 'message': 'Invalid Request Parameters'}}),  # observed live
    (401, {'error': {'code': '401'}}),
])
def test_a_rejected_key_says_key_not_network(monkeypatch, status, payload):
    api = _api()
    api.reset_state()
    monkeypatch.setattr(api.urllib.request, 'urlopen', _Server(token_error=(status, payload)))
    with pytest.raises(api.PleosError) as ex:
        _kia().verify_credentials()
    assert ex.value.kind == 'key_invalid'
    assert 'client-secret' not in str(ex.value)


def test_a_429_keeps_us_away_without_calling_again(server):
    from services.vehicle.pleos_api import PleosError
    _kia().get_status()
    n = len(server.calls)
    server.errors['batteries'] = (429, {'error': {'code': '429'}})
    with pytest.raises(PleosError) as ex:
        _kia().get_status()
    assert ex.value.kind == 'rate_limited' and ex.value.retry_after >= 300
    after_429 = len(server.calls)
    with pytest.raises(PleosError):
        _kia().get_status()
    assert len(server.calls) == after_429, 'asked again during the back-off'
    assert after_429 == n + 1


def test_several_cars_need_a_choice_and_a_foreign_vin_is_refused(server):
    from services.vehicle.pleos_api import PleosError
    server.cars = CARS + [{'vin': 'KNAOTHER000000002', 'nickname': 'EV6', 'sellname': 'EV6'}]
    with pytest.raises(PleosError) as ex:
        _kia().get_status()
    assert ex.value.kind == 'pick_vin' and len(ex.value.vehicles) == 2
    assert _kia(vin=VIN.lower()).get_status().soc_percent == 30
    _api().reset_state()
    with pytest.raises(PleosError) as ex:
        _kia(vin='WRONGVIN000000000').verify_credentials()
    assert ex.value.kind == 'vin_not_in_key'


def test_the_vehicle_list_is_not_fetched_on_every_sync(server):
    for _ in range(3):
        _kia().get_status()
    assert sum(1 for c in server.calls if c[1] == '/v1/vehicles/consent') == 1


def test_errors_do_not_leak_the_vin(monkeypatch):
    api = _api()
    api.reset_state()

    def boom(req, timeout=None):
        raise urllib.error.URLError(f'connection refused for {req.full_url}')
    monkeypatch.setattr(api.urllib.request, 'urlopen', boom)
    with pytest.raises(api.PleosError) as ex:
        _kia(vin=VIN).get_status()
    assert ex.value.kind == 'network'
    # The reason text is the library's; our own wording adds no URL. What
    # matters is that the token request — the first call — has no VIN.
    assert '/v1/vehicles/' not in str(ex.value)


# ── registry / catalog ────────────────────────────────────────────────

def test_the_brands_are_registered_without_any_package():
    from services.vehicle import registry
    from services.vehicle.catalog import by_key, required_columns
    for key in ('kia_pleos', 'hyundai_pleos', 'genesis_pleos'):
        assert registry.is_installed(key)
        assert by_key(key) is not None and by_key(key).pkg is None
        assert required_columns(key) == ['api_username', 'api_password']


# ── the 9035 block on the old access ─────────────────────────────────
BLOCK = ('CCI token exchange failed: HTTP 400 — {"code":9035,"message":"Unusual activity '
         'has been detected so youve been logged out. Please restart the app.","errors":[]}. '
         'This may indicate a Hyundai API change.')


def _sdk_exc(name, text):
    cls = type(name, (Exception,), {})
    cls.__module__ = 'hyundai_kia_connect_api.exceptions'
    return cls(text)


class _Mgr:
    def __init__(self, err):
        self.err, self.calls, self.password = err, 0, 'ein-echtes-passwort'

    def check_and_refresh_token(self):
        self.calls += 1
        if self.err:
            raise self.err
        return True


def _hk(err):
    import services.vehicle.connector_hyundai_kia as K
    K._vendor_blocked_until.clear()
    v = K.KiaConnector({'username': 'wer@example.invalid', 'password': 'ein-echtes-passwort',
                        'pin': '', 'region': 'EU'})
    m = _Mgr(err)
    v._get_manager = lambda: m
    v._check_sdk_supports_credential = lambda: None
    return K, v, m


def test_9035_is_the_manufacturers_block_not_a_password_problem():
    K, v, m = _hk(_sdk_exc('AuthenticationError', BLOCK))
    with pytest.raises(K.VendorBlocked) as ex:
        v._ensure_auth()
    text = str(ex.value)
    assert '9035' in text and 'Pleos' in text
    assert 'Groß-/Kleinschreibung' not in text, 'the password checklist again'
    assert m.calls == 1
    assert getattr(ex.value, 'kind', '') == 'vendor_block'


def test_after_9035_the_loop_stops_signing_in_by_itself():
    K, v, m = _hk(_sdk_exc('AuthenticationError', BLOCK))
    with pytest.raises(K.VendorBlocked):
        v._ensure_auth()
    for _ in range(5):
        with pytest.raises(K.VendorBlocked):
            v._ensure_auth()
    assert m.calls == 1, 'every sign-in during the block is one more "unusual activity"'


def test_the_test_button_still_asks_and_a_success_lifts_the_pause():
    K, v, m = _hk(_sdk_exc('AuthenticationError', BLOCK))
    with pytest.raises(K.VendorBlocked):
        v._ensure_auth()
    m.err = None
    v.verify_credentials()
    assert m.calls == 2
    v._ensure_auth()
    assert m.calls == 3 and not K._vendor_blocked_until


def test_the_german_variant_of_the_message_is_recognised_too():
    import services.vehicle.connector_hyundai_kia as K
    err = Exception('HTTP 400: {"code":9035,"message":"Es wurde eine ungewöhnliche Aktivität '
                    'festgestellt, daher wurden Sie abgemeldet."}')
    assert K._is_vendor_block(err)
    assert not K._is_vendor_block(Exception('Received unexpected statusCode'))


def test_every_new_string_exists_in_every_language():
    keys = set()
    import re
    for p in ('services/vehicle/pleos_api.py', 'services/vehicle/connector_hyundai_kia.py',
              'services/vehicle/connector_pleos.py', 'templates/settings.html', 'app.py'):
        src = open(os.path.join(ROOT, p), encoding='utf-8').read()
        keys |= set(re.findall(r"""['"]((?:pleos|hk)\.[a-z0-9_]+)['"]""", src))
        keys |= set(re.findall(r"""['"](cred\.help_pleos_key)['"]""", src))
    assert {'pleos.err_key_invalid', 'pleos.switch_title', 'hk.err_vendor_block'} <= keys
    for lang in ('de', 'en', 'fr', 'es', 'it', 'nl'):
        d = json.load(open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8'))
        missing = sorted(k for k in keys if k not in d)
        assert not missing, (lang, missing)


# ── the switch-over route (own interpreter: needs create_app) ─────────
SWITCH = r'''
import io, json, os, sys, tempfile, urllib.error
sys.path.insert(0, %(root)r)
os.environ['EV_DATA_DIR'] = tempfile.mkdtemp(prefix='evct-pleos-')
os.environ.setdefault('SECRET_KEY', 'pleos-test')
from app import create_app
from services.vehicle import pleos_api as api
GOOD = %(good)r
class R(io.BytesIO):
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake(req, timeout=None):
    path = req.full_url.split('api.pleos.ai', 1)[1]
    if path == '/v1/auth/personal-token':
        body = json.loads(req.data)
        if body['clientSecret'] != GOOD:
            raise urllib.error.HTTPError(req.full_url, 400, 'x', {}, io.BytesIO(b'{"error":{"code":"4002"}}'))
        return R(b'{"data":{"accessToken":"t"}}')
    return R(json.dumps({'data': {'vehicles': [{'vin': 'KNATESTVIN0000001', 'nickname': 'Niro'}]}}).encode())
api.urllib.request.urlopen = fake
app = create_app()
with app.app_context():
    from models.database import db, Vehicle, AppConfig
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.api_brand, v.api_username, v.api_password, v.api_region = 'kia', 'a@b.c', 'pw', 'EU'
    db.session.commit()
    AppConfig.set('vendor_block_at_%%d' %% v.id, '2026-10-14T09:05:03')
    vid = v.id
c = app.test_client()
page = c.get('/settings').get_data(as_text=True)
r = c.post('/api/vehicle/%%d/pleos/switch' %% vid, json={'client_id': 'cid', 'client_secret': %(sent)r})
with app.app_context():
    v = Vehicle.query.get(vid)
    print(json.dumps({'answer': r.get_json(), 'brand': v.api_brand, 'user': v.api_username,
                      'pw': v.api_password, 'vin': v.api_vin,
                      'cfg_brand': AppConfig.get('vehicle_api_brand', ''),
                      'prev': AppConfig.get('pleos_previous_%%d' %% vid, ''),
                      'block': AppConfig.get('vendor_block_at_%%d' %% vid, ''),
                      'card': 'pleosSwitchBox' in page}))
'''


def _switch(sent):
    code = SWITCH % {'root': ROOT, 'good': 'right-secret', 'sent': sent}
    r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                       timeout=180, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_rejected_key_changes_nothing():
    out = _switch('wrong-secret')
    assert out['card'] is True, 'the blocked car must be offered the switch'
    assert out['answer']['ok'] is False and out['answer']['kind'] == 'key_invalid'
    assert (out['brand'], out['user'], out['pw']) == ('kia', 'a@b.c', 'pw')
    assert out['block'] and not out['prev']


def test_a_proved_key_switches_the_car_and_both_config_places():
    out = _switch('right-secret')
    assert out['answer']['ok'] is True
    assert out['brand'] == 'kia_pleos' and out['cfg_brand'] == 'kia_pleos'
    assert (out['user'], out['pw'], out['vin']) == ('cid', 'right-secret', 'KNATESTVIN0000001')
    prev = json.loads(out['prev'])
    assert prev['brand'] == 'kia' and prev['username'] == 'a@b.c'
    assert 'pw' not in json.dumps(prev).replace('"', ' ').split(), 'old password copied'
    assert out['block'] == ''
