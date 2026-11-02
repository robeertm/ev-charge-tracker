# -*- coding: utf-8 -*-
"""Sign-in state per vehicle, and the brake after a rejected credential.

Measured on a real install: a stored refresh token had run out, and the
background loop signed in anyway on every tick — 96 refused sign-ins in
24 hours. "Not retried" only held within one tick. Every refusal nudges the
manufacturer account toward a captcha or a lockout.

What is pinned down here:

* every failure gets a machine-readable state (``classify``), and an
  explicit ``kind`` on the exception wins over the message;
* the stored detail text is scrubbed of addresses, URLs, IDs and tokens;
* after ``token_expired`` / ``credentials_rejected`` (Kia/Hyundai only)
  the loop pauses 1, 3, 6, 12, then 24 h — without asking the SDK and
  without spending the daily budget — and logs exactly one line per pause;
* saving changed credentials and the "Test" button lift the pause;
* the 9035 vendor block behaves exactly as before;
* nothing in the state module ever raises.

No SDK and no network: connectors are stubs, the database is in memory
(route tests run the real app in a subprocess on a throwaway directory,
same pattern as test_readonly_api.py).

Run with:  python3 -m pytest tests/test_login_state.py
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                                              # noqa: E402
from models.database import db, AppConfig, Vehicle                   # noqa: E402
import services.vehicle as vehicle_pkg                               # noqa: E402
import services.vehicle.connector_hyundai_kia as K                   # noqa: E402
from services.vehicle import login_state as L                        # noqa: E402
from services.vehicle import sync_service as S                       # noqa: E402

# Meaningless values, only their shape matters.
TOKEN = 'k' * 48
PASSWORT = 'ein-echtes-passwort'


# ── helpers ───────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_memory():
    L.reset()
    K._vendor_blocked_until.clear()
    K._managers.clear()
    yield
    L.reset()
    K._vendor_blocked_until.clear()
    K._managers.clear()


def _app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    return app


def _car(app, brand='kia', password=TOKEN):
    with app.app_context():
        v = Vehicle(name='Testauto', api_brand=brand,
                    api_username='wer@example.invalid', api_password=password,
                    auto_sync=True, is_archived=False)
        db.session.add(v)
        db.session.commit()
        return v.id


def _sdk_exc(name, text):
    cls = type(name, (Exception,), {})
    cls.__module__ = 'hyundai_kia_connect_api.exceptions'
    return cls(text)


class _FakeConnector:
    """Counts how often the sync loop asked the vendor."""

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def get_status(self, force=False):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(
            soc_percent=50, odometer_km=1000, location_lat=None,
            is_charging=False, raw_data={})


def _install(monkeypatch, conn):
    monkeypatch.setattr(vehicle_pkg, 'get_connector', lambda brand, creds: conn)
    # The success path stores a row through app._save_vehicle_sync. Importing
    # the real app module would pin config.DATA_DIR for the whole session,
    # so a stand-in module answers that one import.
    fake_app = types.ModuleType('app')
    fake_app._save_vehicle_sync = lambda status, kwh, raw_json='', vehicle_id=None: object()
    monkeypatch.setitem(sys.modules, 'app', fake_app)
    import services.co2_backfill as bf
    monkeypatch.setattr(bf, 'start_backfill', lambda app: None)


def _counter(app, vid):
    with app.app_context():
        return int(AppConfig.get(S.counter_keys(vid)[1], '0') or 0)


# ── classification ────────────────────────────────────────────────────
def test_explicit_kinds_are_taken_as_they_are():
    for kind in ('token_expired', 'credentials_rejected', 'rate_limit',
                 'consent_required', 'otp_required'):
        assert L.classify(K.SignInRefused('irgendein Text', kind)) == kind
    assert L.classify(K.VendorBlocked('blockiert')) == 'vendor_block'


def test_kind_wins_over_the_message():
    assert L.classify(K.SignInRefused('HTTP 503 Service Unavailable',
                                      'credentials_rejected')) == 'credentials_rejected'
    e = RuntimeError('401 Unauthorized')
    e.kind = 'vendor_error'
    assert L.classify(e) == 'vendor_error'


@pytest.mark.parametrize('text', [
    'HTTP 500 Internal Server Error', '502 Bad Gateway', 'status 503',
    'Error 504 gateway timeout', 'read timed out', 'Zeitüberschreitung beim Lesen',
    'Verbindungsfehler', 'Connection refused', 'connection reset by peer'])
def test_vendor_side_problems(text):
    assert L.classify(RuntimeError(text)) == 'vendor_error'


def test_vendor_side_problems_by_type():
    assert L.classify(TimeoutError('x')) == 'vendor_error'
    assert L.classify(ConnectionError('x')) == 'vendor_error'


@pytest.mark.parametrize('text', [
    '401 Client Error', 'HTTP 403', 'Unauthorized', 'Authentication failed',
    'invalid_grant: refresh token expired'])
def test_refused_credentials(text):
    assert L.classify(RuntimeError(text)) == 'credentials_rejected'


def test_anything_else_is_a_plain_error():
    assert L.classify(ValueError('Kein Fahrzeug im Account gefunden')) == 'error'
    assert L.classify(None) == 'error'


# ── the connector tags its refusals, texts unchanged ──────────────────
class _Zaehler:
    def __init__(self, fehler):
        self.fehler = fehler
        self.versuche = 0
        self.password = TOKEN

    def check_and_refresh_token(self):
        self.versuche += 1
        raise self.fehler


def _verbinder(fehler, password=TOKEN):
    v = K.KiaConnector({'username': 'wer@example.invalid', 'password': password,
                        'pin': '', 'region': 'EU'})
    z = _Zaehler(fehler)
    z.password = password
    v._get_manager = lambda: z
    v._check_sdk_supports_credential = lambda: None
    return v, z


@pytest.mark.parametrize('fehler,password,kind', [
    (_sdk_exc('AuthenticationError', 'Received unexpected statusCode'), TOKEN, 'token_expired'),
    (_sdk_exc('AuthenticationError', 'Received unexpected statusCode'), PASSWORT, 'credentials_rejected'),
    (_sdk_exc('RateLimitingError', 'Exceeds number of requests'), PASSWORT, 'rate_limit'),
    (_sdk_exc('ConsentRequiredError', 'nothing quotable'), PASSWORT, 'consent_required'),
    (RuntimeError('otp required for this account'), PASSWORT, 'otp_required'),
])
def test_the_connector_says_which_refusal(fehler, password, kind):
    v, z = _verbinder(fehler, password)
    with pytest.raises(RuntimeError) as ex:
        v._ensure_auth()
    assert isinstance(ex.value, K.SignInRefused)
    assert ex.value.kind == kind
    assert str(ex.value) == K._friendly_auth_message(fehler, password)
    assert z.versuche == 1


# ── scrubbing ─────────────────────────────────────────────────────────
def test_detail_is_scrubbed():
    raw = ('Login for jemand@example.com failed at '
           'https://idp.example.invalid/auth/realms/eu?code=abc&state=xyz '
           'device 123e4567-e89b-12d3-a456-426614174000 vin KNAXX81ABC5012345 '
           'token ' + 'Ab9' * 12 + ' ref 1234567890')
    s = L.scrub(raw)
    assert 'jemand@example.com' not in s and '<email>' in s
    assert 'https://idp.example.invalid' in s
    assert '/auth' not in s and 'code=abc' not in s and 'state=xyz' not in s
    assert '123e4567' not in s and '<uuid>' in s
    assert 'KNAXX81ABC5012345' not in s and '<vin>' in s
    assert 'Ab9Ab9' not in s and '<token>' in s
    assert '1234567890' not in s and '<n>' in s


def test_detail_drops_user_info_in_urls_and_is_short():
    s = L.scrub('see https://name:secret@host.example.invalid/x ' + 'wort ' * 80)
    assert 'secret' not in s and 'name:' not in s
    assert 'https://host.example.invalid' in s
    assert len(s) <= 160


def test_a_long_token_without_digits_is_scrubbed_too():
    assert TOKEN not in L.scrub('password field holds ' + TOKEN)


def test_the_stored_detail_is_scrubbed():
    app = _app()
    vid = _car(app)
    with app.app_context():
        L.note_failure(vid, 'renault', RuntimeError(
            'HTTP 401 for jemand@example.com token ' + 'z9' * 20))
        rec = json.loads(AppConfig.get(L.KEY.format(vid=vid)))
    assert rec['state'] == 'credentials_rejected'
    assert 'jemand@example.com' not in rec['detail']
    assert 'z9z9' not in rec['detail']


# ── the record ────────────────────────────────────────────────────────
def test_ok_resets_attempts_and_moves_last_ok():
    app = _app()
    vid = _car(app)
    with app.app_context():
        L.note_ok(vid, 'kia')
        first_ok = L.read(vid)['last_ok_at']
        AppConfig.set(L.KEY.format(vid=vid), json.dumps(dict(
            L.read(vid), last_ok_at='2026-01-01T00:00:00')))
        L.note_failure(vid, 'kia', RuntimeError('HTTP 503'))
        L.note_failure(vid, 'kia', RuntimeError('HTTP 503'))
        failed = L.read(vid)
        L.note_ok(vid, 'kia')
        ok = L.read(vid)
    assert first_ok
    assert failed['state'] == 'vendor_error' and failed['attempts'] == 2
    assert failed['last_ok_at'] == '2026-01-01T00:00:00', 'kept through failures'
    assert ok['state'] == 'ok' and ok['attempts'] == 0
    assert ok['last_ok_at'] != '2026-01-01T00:00:00'
    assert ok['paused_until'] is None and ok['detail'] == ''
    assert set(ok) == {'state', 'kind', 'since', 'last_attempt_at', 'last_ok_at',
                       'attempts', 'paused_until', 'brand', 'detail'}
    assert ok['kind'] == 'ok'


def test_since_stays_while_the_state_stays():
    app = _app()
    vid = _car(app)
    key = L.KEY.format(vid=vid)
    with app.app_context():
        L.note_failure(vid, 'renault', RuntimeError('HTTP 503'))
        rec = json.loads(AppConfig.get(key))
        rec['since'] = '2026-10-01T08:00:00'
        AppConfig.set(key, json.dumps(rec))
        L.note_failure(vid, 'renault', RuntimeError('read timed out'))
        same = L.read(vid)
        L.note_failure(vid, 'renault', RuntimeError('401'))
        changed = L.read(vid)
    assert same['since'] == '2026-10-01T08:00:00'
    assert same['last_attempt_at'] != '2026-10-01T08:00:00'
    assert changed['state'] == 'credentials_rejected'
    assert changed['since'] != '2026-10-01T08:00:00'
    assert changed['attempts'] == 3


# ── nothing escapes ───────────────────────────────────────────────────
def test_broken_json_is_survived():
    app = _app()
    vid = _car(app)
    with app.app_context():
        AppConfig.set(L.KEY.format(vid=vid), '{not json')
        assert L.read(vid) is None
        L.note_failure(vid, 'kia', RuntimeError('boom'))
        assert L.read(vid)['state'] == 'error'
        AppConfig.set(L.KEY.format(vid=vid), '[1, 2]')
        L.note_ok(vid, 'kia')
        assert L.read(vid)['state'] == 'ok'


def test_database_errors_never_escape(monkeypatch):
    app = _app()
    vid = _car(app)

    def broken(*a, **k):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(AppConfig, 'get', staticmethod(broken))
    monkeypatch.setattr(AppConfig, 'set', staticmethod(broken))
    with app.app_context():
        L.note_ok(vid, 'kia')
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
        assert L.read(vid) is None
        L.lift_pause(vid)
    # and without any app context at all
    L.note_ok(vid, 'kia')
    L.note_failure(vid, 'kia', RuntimeError('x'))
    assert L.read(vid) is None
    assert L.skip_paused(vid, 'Testauto') in (True, False)


def test_a_broken_exception_is_survived():
    class Nasty(Exception):
        def __str__(self):
            raise ValueError('no text')
    assert L.classify(Nasty()) == 'error'
    assert L.scrub(Nasty()) == ''


# ── the brake ─────────────────────────────────────────────────────────
def test_the_steps_are_1_3_6_12_24_24_hours():
    app = _app()
    vid = _car(app)
    seen = []
    with app.app_context():
        for _ in range(6):
            before = datetime.now()
            L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
            until = L.paused_until(vid)
            seen.append(round((until - before).total_seconds() / 3600))
            assert L.read(vid)['paused_until'] == until.isoformat(timespec='seconds')
    assert seen == [1, 3, 6, 12, 24, 24]


def test_only_rejections_of_the_kia_hyundai_connector_brake():
    app = _app()
    vid = _car(app)
    with app.app_context():
        for kind in ('rate_limit', 'consent_required', 'otp_required', 'vendor_block'):
            L.note_failure(vid, 'kia', K.SignInRefused('x', kind))
            assert L.paused_until(vid) is None, kind
        L.note_failure(vid, 'kia', RuntimeError('HTTP 503'))
        assert L.paused_until(vid) is None
        L.note_failure(vid, 'renault', RuntimeError('401 Unauthorized'))
        assert L.paused_until(vid) is None
        L.note_failure(vid, 'hyundai', K.SignInRefused('x', 'credentials_rejected'))
        assert L.paused_until(vid) is not None


def test_an_expired_pause_lets_the_next_attempt_through_and_steps_up():
    app = _app()
    vid = _car(app)
    with app.app_context():
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
    L._paused_until[vid] = datetime.now() - timedelta(seconds=1)
    assert L.paused_until(vid) is None
    assert L.skip_paused(vid) is False
    with app.app_context():
        before = datetime.now()
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
    assert round((L.paused_until(vid) - before).total_seconds() / 3600) == 3


def test_ok_clears_the_brake_and_starts_over():
    app = _app()
    vid = _car(app)
    with app.app_context():
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
        L.note_ok(vid, 'kia')
        assert L.paused_until(vid) is None
        before = datetime.now()
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
    assert round((L.paused_until(vid) - before).total_seconds() / 3600) == 1


def test_a_restart_forgets_the_pause_but_not_the_record():
    app = _app()
    vid = _car(app)
    with app.app_context():
        L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
        assert json.loads(AppConfig.get(L.KEY.format(vid=vid)))['paused_until']
        L.reset()                                # what a restart does
        rec = L.read(vid)
    assert rec['state'] == 'token_expired'
    assert rec['paused_until'] is None, 'the shown pause must be the live one'


def test_a_paused_tick_neither_asks_the_vendor_nor_spends_budget(monkeypatch, caplog):
    app = _app()
    vid = _car(app)
    conn = _FakeConnector(K.SignInRefused('abgelaufen', 'token_expired'))
    _install(monkeypatch, conn)
    caplog.set_level(logging.INFO, logger=L.__name__)

    S._do_sync(app)                              # refused -> pause
    assert conn.calls == 1 and _counter(app, vid) == 1
    with app.app_context():
        assert L.read(vid)['state'] == 'token_expired'
        assert L.read(vid)['paused_until'] is not None

    for _ in range(3):                           # three paused ticks
        S._do_sync(app)
    assert conn.calls == 1, 'a paused tick must not call the connector'
    assert _counter(app, vid) == 1, 'a paused tick must not count against the budget'
    lines = [r for r in caplog.records
             if 'sign-in paused until' in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    with app.app_context():
        assert L.read(vid)['attempts'] == 1, 'skipped ticks are no attempts'


def test_lifting_lets_the_next_tick_through(monkeypatch):
    app = _app()
    vid = _car(app)
    conn = _FakeConnector(K.SignInRefused('abgelaufen', 'token_expired'))
    _install(monkeypatch, conn)
    S._do_sync(app)
    S._do_sync(app)
    assert conn.calls == 1
    with app.app_context():
        L.lift_pause(vid)
        assert L.read(vid)['paused_until'] is None
    conn.error = None                            # new credentials work
    S._do_sync(app)
    assert conn.calls == 2
    with app.app_context():
        rec = L.read(vid)
    assert rec['state'] == 'ok' and rec['attempts'] == 0 and rec['last_ok_at']


def test_other_brands_keep_their_cadence(monkeypatch):
    app = _app()
    _car(app, brand='renault', password='pw')
    conn = _FakeConnector(RuntimeError('401 Unauthorized'))
    _install(monkeypatch, conn)
    S._do_sync(app)
    S._do_sync(app)
    assert conn.calls == 2


def test_the_vendor_block_is_unchanged(monkeypatch):
    """9035: marker set, no login_state pause — the connector's own
    in-memory block keeps working exactly as before."""
    app = _app()
    vid = _car(app)
    conn = _FakeConnector(K.VendorBlocked('blockiert'))
    _install(monkeypatch, conn)
    S._do_sync(app)
    S._do_sync(app)
    assert conn.calls == 2
    with app.app_context():
        assert AppConfig.get(S.VENDOR_BLOCK_KEY.format(vid=vid), '')
        rec = L.read(vid)
    assert rec['state'] == 'vendor_block' and rec['paused_until'] is None
    assert L.paused_until(vid) is None


def test_the_connector_vendor_block_still_stops_by_itself():
    v, z = _verbinder(RuntimeError('HTTP 400 {"code": 9035, "message": "x"}'), PASSWORT)
    with pytest.raises(K.VendorBlocked):
        v._ensure_auth()
    with pytest.raises(K.VendorBlocked):
        v._ensure_auth()
    assert z.versuche == 1
    assert K._vendor_blocked_until[v._cache_key] == float('inf')
    with pytest.raises(K.VendorBlocked):
        v.verify_credentials()                   # the Test button still asks
    assert z.versuche == 2


# ── save and Test lift the pause (real app, subprocess) ───────────────
_PREAMBLE = """
import json, sys
sys.path.insert(0, %r)
from app import create_app
from models.database import db, AppConfig, Vehicle
from services.vehicle import login_state as L
import services.vehicle.connector_hyundai_kia as K
app = create_app()
client = app.test_client()
with app.app_context():
    AppConfig.set('vehicle_sync_enabled', 'false')
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.name = 'Testauto'; v.api_brand = 'kia'
    v.api_username = 'wer@example.invalid'; v.api_password = %r
    v.auto_sync = False
    db.session.commit()
    vid = v.id
    L.note_failure(vid, 'kia', K.SignInRefused('x', 'token_expired'))
    assert L.paused_until(vid) is not None
form = {'vehicle_id': str(vid), 'name': 'Testauto', 'api_brand': 'kia',
        'api_username': 'wer@example.invalid', 'api_password': ''}
""" % (ROOT, TOKEN)


def _run(script):
    d = tempfile.mkdtemp(prefix='evct-login-')
    env = dict(os.environ, EV_DATA_DIR=d, SECRET_KEY='login-state-test')
    proc = subprocess.run([sys.executable, '-c', _PREAMBLE + script], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    last = [ln for ln in proc.stdout.strip().split('\n') if ln.startswith('{')]
    assert last, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(last[-1])


def test_saving_unchanged_credentials_keeps_the_pause_new_ones_lift_it():
    out = _run("""
r1 = client.post('/vehicles/save', data=dict(form, ajax='1'))
kept = L.paused_until(vid) is not None
r2 = client.post('/vehicles/save', data=dict(form, ajax='1', api_password='ein-neues-passwort'))
with app.app_context():
    shown = L.read(vid)['paused_until']
print(json.dumps({'c1': r1.status_code, 'c2': r2.status_code, 'kept': kept,
                  'lifted': L.paused_until(vid) is None, 'shown': shown}))""")
    assert out['c1'] == 200 and out['c2'] == 200
    assert out['kept'] is True, 'saving the same credentials must not lift the pause'
    assert out['lifted'] is True and out['shown'] is None


def test_changing_brand_or_user_lifts_the_pause():
    out = _run("""
client.post('/vehicles/save', data=dict(form, ajax='1', api_brand='hyundai'))
a = L.paused_until(vid) is None
with app.app_context():
    L.note_failure(vid, 'hyundai', K.SignInRefused('x', 'credentials_rejected'))
client.post('/vehicles/save', data=dict(form, ajax='1', api_brand='hyundai',
                                        api_username='anders@example.invalid'))
b = L.paused_until(vid) is None
print(json.dumps({'a': a, 'b': b}))""")
    assert out['a'] is True and out['b'] is True


def test_the_test_button_lifts_the_pause():
    out = _run("""
import services.vehicle as P
class Fake:
    def verify_credentials(self):
        return None
P.get_connector = lambda brand, creds: Fake()
r = client.post('/vehicles/%d/test' % vid, data={'ajax': '1'})
print(json.dumps({'code': r.status_code, 'body': r.get_json(),
                  'lifted': L.paused_until(vid) is None}))""")
    assert out['code'] == 200 and out['body']['ok'] is True
    assert out['lifted'] is True


def test_sync_health_lists_the_login_state_without_detail():
    out = _run("""
r = client.get('/api/sync/health')
print(json.dumps({'code': r.status_code, 'body': r.get_json()}))""")
    assert out['code'] == 200
    cars = out['body']['vehicles']
    assert len(cars) == 1
    car = cars[0]
    assert set(car) == {'id', 'name', 'api_brand', 'login'}
    assert car['api_brand'] == 'kia' and car['name'] == 'Testauto'
    assert set(car['login']) == {'state', 'kind', 'since', 'last_ok_at', 'paused_until',
                                 'attempts', 'key_expires'}
    assert car['login']['state'] == 'token_expired'
    assert car['login']['kind'] == 'token_expired'
    assert car['login']['key_expires'] is None, 'not a Pleos car: no key end date'
    assert car['login']['paused_until'] and car['login']['attempts'] == 1
    text = json.dumps(out['body'])
    assert 'detail' not in text and TOKEN not in text
    assert 'wer@example.invalid' not in text
