# -*- coding: utf-8 -*-
"""Renault/Dacia: the sign-in is kept between polls, with a back-off.

Until now every poll was a full Gigya sign-in (password out, login token
back), then ``get_person``. At an hourly cadence that is 24 sign-ins a day
on the owner's account. renault-api 0.6.3 is built to avoid that: its
``RenaultSession`` takes a ``credential_store``; with the Gigya login token
in it, an expired JWT is minted again without the password.

These tests run against the REAL renault-api (session, credential store,
JWT expiry, Gigya/Kamereon schemas and error mapping). Only the network
is fake (``tests/_renault_fake.py``, ``deep=True``): Kamereon accepts only
a JWT the fake Gigya issued and that has not run out, so a connector that
does not renew correctly fails here the way it would live. Any real
network access fails the test.
"""
import json
import logging
import os
import pathlib
import stat
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

pytest.importorskip('renault_api')
import aiohttp  # noqa: E402
from renault_api.gigya import exceptions as gigya_exc  # noqa: E402
from renault_api.kamereon import exceptions as kam_exc  # noqa: E402

import _renault_fake as fake  # noqa: E402
from services.vehicle import connector_renault as cr  # noqa: E402
from services.vehicle import login_state as L  # noqa: E402

USER = 'owner@example.invalid'


def _restart():
    """What a container restart does to the connector: memory gone, files stay."""
    reset = getattr(cr, '_reset_memory', None)
    if reset:
        reset()


@pytest.fixture
def net(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, 'STATE_DIR', str(tmp_path / 'renault'))
    _restart()
    L.reset()
    st = fake.install(monkeypatch.setattr, deep=True)
    st['password'] = fake.PASSWORD          # a wrong one is refused here
    yield st
    _restart()
    L.reset()


def _conn(password=fake.PASSWORD, user=USER, cls=None):
    return (cls or cr.RenaultConnector)({'username': user, 'password': password,
                                         'locale': 'fr_FR', 'vin': ''})


def _state(user=USER):
    return cr._load_state(user)


def _expire_stored_jwt(user=USER):
    st = _state(user)
    st['session']['gigya_jwt'] = fake.mint_jwt(-10)
    cr._save_state(user, st)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith('Renault: ')
            and r.getMessage().split(': ', 1)[1] in ('session reused', 'token renewed',
                                                     'signed in (fresh)')]


# ── (a) two polls, one sign-in ────────────────────────────────────────
def test_two_polls_in_a_row_sign_in_once(net, caplog):
    caplog.set_level(logging.INFO)
    assert _conn().get_status().soc_percent == 63
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == 1
    assert net['jwt_calls'] == 1            # the JWT is still valid: not even renewed
    assert net['person_calls'] == 1         # the account list comes from the file
    assert net['account_info_calls'] == 1   # the person id too
    assert _lines(caplog) == ['Renault: signed in (fresh)', 'Renault: session reused']


def test_the_account_line_comes_only_with_a_fresh_account_list(net, caplog):
    caplog.set_level(logging.INFO)
    _conn().get_status()
    _conn().get_status()
    acc = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Renault account MYRENAULT')]
    assert len(acc) == 1


def test_a_day_old_account_list_is_asked_again(net):
    _conn().get_status()
    st = _state()
    st['accounts']['ts'] -= cr.ACCOUNTS_TTL + 1
    cr._save_state(USER, st)
    _conn().get_status()
    assert net['person_calls'] == 2 and len(net['logins']) == 1


# ── (b) a restart keeps the sign-in ───────────────────────────────────
def test_a_restart_with_a_stored_session_does_not_sign_in(net, caplog):
    caplog.set_level(logging.INFO)
    _conn().get_status()
    _restart()
    caplog.clear()
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == 1
    assert _lines(caplog) == ['Renault: session reused']


# ── (c) an expired JWT is renewed without the password ────────────────
def test_an_expired_jwt_is_renewed_from_the_login_token(net, caplog):
    caplog.set_level(logging.INFO)
    _conn().get_status()
    _expire_stored_jwt()
    _restart()
    caplog.clear()
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == 1           # no password sent
    assert net['jwt_calls'] == 2
    assert _lines(caplog) == ['Renault: token renewed']
    # the new JWT is kept for the next poll
    assert _state()['session']['gigya_jwt'] == net['jwts'][-1]


def test_a_jwt_the_data_service_refuses_is_renewed_not_signed_in(net):
    _conn().get_status()
    net['revoked_jwts'].add(_state()['session']['gigya_jwt'])
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == 1 and net['jwt_calls'] == 2


# ── (d) a refused login token: exactly one sign-in, then reuse again ──
def test_a_refused_login_token_means_one_sign_in_then_reuse(net, caplog):
    caplog.set_level(logging.INFO)
    _conn().get_status()
    net['valid_login_tokens'].clear()        # Gigya no longer accepts it (403005)
    _expire_stored_jwt()
    caplog.clear()
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == 2
    assert _lines(caplog) == ['Renault: signed in (fresh)']
    caplog.clear()
    _conn().get_status()
    assert len(net['logins']) == 2
    assert _lines(caplog) == ['Renault: session reused']


def test_a_data_error_with_a_valid_session_never_signs_in(net):
    _conn().get_status()
    net['kamereon_error'] = kam_exc.InvalidUpstreamException('err.tech.500', 'upstream')
    with pytest.raises(Exception):
        _conn().get_status()
    net['kamereon_error'] = aiohttp.ClientResponseError(None, (), status=503)
    with pytest.raises(Exception):
        _conn().get_status()
    net['kamereon_error'] = None
    _conn().get_status()
    assert len(net['logins']) == 1 and net['jwt_calls'] == 1
    assert not cr._backoff


# ── (e) wrong password: the sign-in pause, recorded in login_state ────
def _app_with_car(monkeypatch, password):
    from flask import Flask
    from models.database import db, Vehicle
    import services.vehicle as vehicle_pkg
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        v = Vehicle(name='Testauto', api_brand='renault', api_username=USER,
                    api_password=password, auto_sync=True, is_archived=False)
        db.session.add(v)
        db.session.commit()
        vid = v.id
    # the REAL connector, built from the car's credentials
    monkeypatch.setattr(vehicle_pkg, 'get_connector',
                        lambda brand, creds: cr.RenaultConnector(creds))
    fake_app = types.ModuleType('app')
    fake_app._save_vehicle_sync = lambda status, kwh, raw_json='', vehicle_id=None: object()
    monkeypatch.setitem(sys.modules, 'app', fake_app)
    import services.co2_backfill as bf
    monkeypatch.setattr(bf, 'start_backfill', lambda app: None)
    return app, vid


def test_a_wrong_password_engages_the_sign_in_pause(net, monkeypatch):
    from services.vehicle import sync_service as S
    app, vid = _app_with_car(monkeypatch, 'falsch')
    S._do_sync(app)
    S._do_sync(app)                          # within the pause: Renault is not asked
    assert len(net['logins']) == 1
    with app.app_context():
        rec = L.read(vid)
    assert rec['state'] == 'credentials_rejected' and rec['brand'] == 'renault'
    assert rec['paused_until'] is not None
    assert 'falsch' not in json.dumps(rec)
    # the owner's text is unchanged
    assert rec['detail'] == L.scrub(cr._t('renault.err_login'))
    # Test lifts the pause (route) — the brake itself steps 1/3/6/... like Kia
    L.lift_pause(vid)
    S._do_sync(app)
    assert len(net['logins']) == 2


def test_the_wrong_password_text_and_kind_come_from_the_connector(net):
    with pytest.raises(RuntimeError) as e:
        _conn('falsch').get_status()
    assert getattr(e.value, 'kind', None) == 'credentials_rejected'
    assert 'My Renault' in str(e.value)


def test_a_plain_401_text_from_renault_still_does_not_brake():
    """Only a refusal the connector names counts; a data error saying 401
    must not stop the sync for hours."""
    assert 'renault' not in L.BRAKE_BRANDS
    assert 'renault' in L.KIND_BRAKE_BRANDS


# ── (f) passing errors at sign-in: a growing back-off ─────────────────
@pytest.mark.parametrize('error', [
    lambda: gigya_exc.GigyaResponseException(500001, 'General Server Error'),
    lambda: gigya_exc.GigyaException('Gigya responded with invalid JSON'),
    lambda: aiohttp.ClientConnectorError(types.SimpleNamespace(ssl=None, host='x', port=443),
                                         OSError(111, 'refused')),
    lambda: TimeoutError(),
])
def test_a_passing_sign_in_error_backs_off_and_a_success_resets(net, error):
    key = cr._account_key(USER)
    net['login_error'] = error
    seen = []
    for _ in range(5):
        with L.background_sync():
            with pytest.raises(Exception) as e:
                _conn().get_status()
        assert getattr(e.value, 'kind', None) != 'credentials_rejected'
        b = cr._backoff[key]
        seen.append(round((b['until'] - time.time()) / 60))
        # within the back-off the loop does not ask Gigya again
        n = len(net['logins'])
        with L.background_sync():
            with pytest.raises(RuntimeError) as held:
                _conn().get_status()
        assert len(net['logins']) == n
        assert getattr(held.value, 'kind', None) == 'vendor_error'
        assert 'renault.err_signin_wait' not in str(held.value)    # translated
        b['until'] = time.time() - 1         # the wait is over
    assert seen == [5, 15, 60, 240, 240]
    assert len(net['logins']) == 5
    net['login_error'] = None
    with L.background_sync():
        assert _conn().get_status().soc_percent == 63
    assert key not in cr._backoff


def test_sync_now_is_never_held_back(net):
    net['login_error'] = lambda: gigya_exc.GigyaResponseException(503001, 'Service unavailable')
    with L.background_sync():
        with pytest.raises(Exception):
            _conn().get_status()
    net['login_error'] = None
    assert _conn().get_status().soc_percent == 63    # outside the loop: straight through
    assert len(net['logins']) == 2


def test_a_passing_error_while_renewing_backs_off_without_the_password(net):
    _conn().get_status()
    _expire_stored_jwt()
    net['jwt_error'] = gigya_exc.GigyaResponseException(500001, 'General Server Error')
    with L.background_sync():
        with pytest.raises(Exception):
            _conn().get_status()
    assert len(net['logins']) == 1           # no full sign-in for a server error
    assert cr._backoff[cr._account_key(USER)]['streak'] == 1
    assert _state()['session'].get('gigya_login_token')   # the token is kept


# ── (g) new credentials drop the stored sign-in ───────────────────────
def test_new_credentials_drop_the_stored_session(net, caplog):
    caplog.set_level(logging.INFO)
    _conn().get_status()
    old = _state()['session']['gigya_login_token']
    net['password'] = 'neues-passwort'
    caplog.clear()
    _conn('neues-passwort').get_status()
    assert len(net['logins']) == 2
    assert _lines(caplog) == ['Renault: signed in (fresh)']
    assert _state()['session']['gigya_login_token'] != old
    # and the old password does not get the new session either
    with pytest.raises(RuntimeError):
        _conn(fake.PASSWORD).get_status()


def test_the_test_button_checks_the_password_itself(net):
    _conn().get_status()
    _conn().verify_credentials()
    assert len(net['logins']) == 2
    net['password'] = 'geaendert'            # changed on Renault's side
    with pytest.raises(RuntimeError):
        _conn().verify_credentials()         # the kept session must not hide it


def test_another_user_gets_their_own_file(net):
    _conn().get_status()
    _conn(user='other@example.invalid').get_status()
    assert len(net['logins']) == 2
    assert len(os.listdir(cr.STATE_DIR)) == 2


# ── (h) the file: 0600, no password ───────────────────────────────────
def test_the_session_file_is_private_and_holds_no_password(net):
    net['password'] = 'Sehr-Geheimes-Passwort-42'
    _conn('Sehr-Geheimes-Passwort-42').get_status()
    files = os.listdir(cr.STATE_DIR)
    assert len(files) == 1 and files[0].endswith('.json')       # no .tmp left
    path = os.path.join(cr.STATE_DIR, files[0])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = open(path, encoding='utf-8').read()
    assert 'Sehr-Geheimes-Passwort-42' not in raw
    assert USER not in raw and 'example' not in files[0]
    sess = json.loads(raw)['session']
    # only Gigya's keys and the fingerprint — no locale, no API keys
    assert set(sess) == {'salt', 'fp', 'gigya_login_token', 'gigya_person_id', 'gigya_jwt'}


def test_a_stale_tmp_file_does_not_leak_its_mode(net):
    os.makedirs(cr.STATE_DIR, exist_ok=True)
    tmp = cr._state_path(USER) + '.tmp'
    open(tmp, 'w').close()
    os.chmod(tmp, 0o644)
    _conn().get_status()
    assert stat.S_IMODE(os.stat(cr._state_path(USER)).st_mode) == 0o600


# ── (i) the e-mail code path, and what follows it ─────────────────────
def test_the_code_path_works_and_its_sign_in_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, 'STATE_DIR', str(tmp_path / 'renault'))
    _restart()
    net = fake.install(monkeypatch.setattr, tfa=True, deep=True)
    with pytest.raises(RuntimeError) as bg:
        _conn().get_status()                 # background: no mail, a clear text
    assert net['mails'] == 0 and getattr(bg.value, 'kind', None) == 'otp_required'
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    assert net['mails'] == 1
    _conn().submit_code(fake.CODE)
    logins = len(net['logins'])
    assert _conn().get_status().soc_percent == 63
    assert len(net['logins']) == logins      # the login token from the code is reused
    assert _state()['device_ids'] == fake.DEVICE
    cr.forget_session(USER)                  # dropping the session keeps the device
    assert _state()['device_ids'] == fake.DEVICE
    _conn().get_status()
    assert net['logins'][-1]['device_ids'] == fake.DEVICE and net['mails'] == 1


# ── (j) the country fallback still runs over the kept session ─────────
def test_the_country_retry_reuses_the_session(net, monkeypatch):
    calls = []
    real = cr._RenaultBase._read

    async def read(self, client, asked, request_code, refresh=False):
        calls.append(asked)
        if asked == 'DE':
            raise cr._Refused('renault.err_data_denied', RuntimeError('x'), 'DE', 'FR')
        return await real(self, client, asked, request_code, refresh)
    monkeypatch.setattr(cr._RenaultBase, '_read', read)
    c = cr.RenaultConnector({'username': USER, 'password': fake.PASSWORD, 'locale': '', 'vin': ''})
    assert c.get_status().soc_percent == 63
    assert calls == ['DE', 'FR'] and len(net['logins']) == 1
    assert _state()['country'] == 'FR'


def test_dacia_keeps_its_session_too(net):
    _conn(cls=cr.DaciaConnector).get_status()
    _conn(cls=cr.DaciaConnector).get_status()
    assert len(net['logins']) == 1
