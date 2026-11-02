# -*- coding: utf-8 -*-
"""A stand-in for the Renault servers — only the network, never the logic.

Shared by ``test_renault_connector.py`` and ``test_renault_code.py``.
Gigya (sign-in, e-mail code) and Kamereon (account, car data) answer here;
everything between — renault-api's session, its two-factor bookkeeping,
its schemas — runs for real, so a wrong attribute or a challenge that does
not survive between two web requests fails exactly as it would live.
"""
import json
import time
from types import SimpleNamespace

import aiohttp
import jwt as pyjwt

from renault_api import gigya, kamereon
from renault_api.gigya import exceptions as gigya_exc
from renault_api.gigya import schemas as gigya_schemas
from renault_api.gigya.models import GigyaTfaEmail
from renault_api.kamereon import schemas
from renault_api.renault_session import RenaultSession

VIN = 'VF1AG000000000001'
CODE = '123456'
DEVICE = {'gmid': 'gmid-of-this-tracker', 'ucid': 'ucid-of-this-tracker'}

PERSON = {'accounts': [
    {'accountId': 'acc-sfdc', 'accountType': 'SFDC', 'accountStatus': 'ACTIVE'},
    {'accountId': 'acc-myr', 'accountType': 'MYRENAULT', 'accountStatus': 'ACTIVE'},
]}
VEHICLES = {'accountId': 'acc-myr', 'country': 'FR', 'vehicleLinks': [
    {'vin': VIN, 'vehicleDetails': {
        'vin': VIN, 'model': {'code': 'XCB1VE', 'label': 'MEGANE E-TECH', 'group': '971'}}},
]}
BATTERY = {'timestamp': '2026-10-17T08:00:00Z', 'batteryLevel': 63, 'batteryAutonomy': 260,
           'plugStatus': 1, 'chargingStatus': 1.0}
COCKPIT = {'totalMileage': 12345.6}
LOCATION = {'gpsLatitude': 48.85, 'gpsLongitude': 2.35, 'lastUpdateTime': '2026-10-17T08:00:00Z'}


PERSON_ID = 'person-1'
JWT_KEY = 'fake-gigya-signing-key-for-tests-only'
PASSWORD = 'pw'
_seq = [0]


def _data(attrs):
    return schemas.KamereonVehicleDataResponseSchema.load(
        {'data': {'type': 'Car', 'id': VIN, 'attributes': attrs}})


def mint_jwt(ttl=900):
    """A JWT the way Gigya's getJWT hands it out (renault-api reads ``exp``)."""
    _seq[0] += 1                # two JWTs of the same second must still differ
    return pyjwt.encode({'exp': int(time.time()) + ttl, 'iat': int(time.time()),
                         'jti': 'jwt-%d' % _seq[0], 'personId': PERSON_ID},
                        JWT_KEY, algorithm='HS256')


def _gigya_error(schema, code, details):
    """Parse a Gigya error body with renault-api's own schema and raise it."""
    schema.loads(json.dumps({'errorCode': code, 'errorDetails': details,
                             'statusCode': 403})).raise_for_error_code()


def install(patch, *, tfa=False, deep=False):
    """Install the fake servers with ``patch(obj, name, value)``.

    ``tfa=True``: the account asks for an e-mail code until this device
    has been confirmed — the way Gigya behaves with 403101.
    ``deep=True``: Kamereon is answered at ``kamereon.request``, so
    renault-api's own JWT handling (expiry, renewal from the login token,
    person id) runs for real and every request must carry a JWT this fake
    issued and that has not run out.
    Any real network access fails the test.
    Returns the shared ``state`` dict (calls, switches) for assertions.
    """
    st = {'tfa': tfa, 'confirmed': set(), 'mails': 0, 'logins': [], 'finalize': [],
          'login_error': None, 'battery': dict(BATTERY), 'vehicles': VEHICLES,
          'vehicles_for': [], 'device_requests': 0,
          # Gigya tokens: login tokens it still accepts, JWTs it issued
          'login_seq': 0, 'valid_login_tokens': set(), 'jwts': [], 'jwt_calls': 0,
          'jwt_error': None, 'jwt_ttl': 900, 'account_info_calls': 0,
          'revoked_jwts': set(), 'person_calls': 0, 'kamereon': [],
          # the one password Gigya accepts; None: any (as before)
          'kamereon_error': None, 'password': None}

    async def blocked(self, method, url, *a, **k):
        raise AssertionError(f'real network access in a test: {method} {str(url).split("?")[0]}')
    patch(aiohttp.ClientSession, '_request', blocked)

    async def login(websession, root_url, api_key, *, login_id, password, device_ids=None):
        st['logins'].append({'login_id': login_id, 'device_ids': dict(device_ids or {})})
        if st['login_error']:
            err = st['login_error']
            raise err() if callable(err) and not isinstance(err, BaseException) else err
        if st['password'] is not None and password != st['password']:
            _gigya_error(gigya_schemas.GigyaLoginResponseSchema, 403042, 'invalid loginID or password')
        if st['tfa'] and (device_ids or {}).get('gmid') not in st['confirmed']:
            raise gigya_exc.PendingTwoFactorAuthenticationException(
                403101, 'Pending Two-Factor Authentication', 'reg-token-1')
        st['login_seq'] += 1
        token = 'login-token-%d' % st['login_seq']
        st['valid_login_tokens'].add(token)
        return SimpleNamespace(get_session_cookie=lambda: token)

    async def get_jwt(websession, root_url, api_key, login_token):
        st['jwt_calls'] += 1
        if st['jwt_error']:
            raise st['jwt_error']
        if login_token not in st['valid_login_tokens']:
            # what Gigya says to a login token it no longer accepts
            _gigya_error(gigya_schemas.GigyaGetJWTResponseSchema, 403005, 'Unauthorized user')
        token = mint_jwt(st['jwt_ttl'])
        st['jwts'].append(token)
        return SimpleNamespace(get_jwt=lambda: token)

    async def get_account_info(websession, root_url, api_key, login_token):
        st['account_info_calls'] += 1
        if login_token not in st['valid_login_tokens']:
            _gigya_error(gigya_schemas.GigyaGetAccountInfoResponseSchema, 403005, 'Unauthorized user')
        return SimpleNamespace(get_person_id=lambda: PERSON_ID)

    async def get_device_ids(websession, root_url, api_key):
        st['device_requests'] += 1
        return dict(DEVICE)

    async def init_tfa(websession, root_url, api_key, *, device_ids, reg_token):
        assert reg_token == 'reg-token-1'
        return SimpleNamespace(get_gigya_assertion=lambda: 'assertion-1')

    async def get_tfa_emails(websession, root_url, api_key, *, device_ids, gigya_assertion):
        return SimpleNamespace(emails=[GigyaTfaEmail(raw_data={}, id='mail-1', obfuscated='o***@example.invalid')])

    async def send_tfa_email_code(websession, root_url, api_key, *, device_ids, gigya_assertion,
                                  email_id, lang=None):
        st['mails'] += 1
        st['mail_lang'] = lang
        return SimpleNamespace(get_phv_token=lambda: 'phv-1')

    async def complete_tfa_email_verification(websession, root_url, api_key, *, device_ids,
                                              gigya_assertion, phv_token, code):
        if code != CODE or gigya_assertion != 'assertion-1' or phv_token != 'phv-1':
            raise gigya_exc.InvalidVerificationCodeException(400006, 'Invalid parameter value')
        return SimpleNamespace(get_provider_assertion=lambda: 'provider-1')

    async def finalize_tfa(websession, root_url, api_key, *, device_ids, gigya_assertion,
                           provider_assertion, reg_token, remember_device=True):
        st['finalize'].append({'device_ids': dict(device_ids), 'reg_token': reg_token,
                               'remember': remember_device})
        st['confirmed'].add(device_ids.get('gmid'))
        return SimpleNamespace()

    for name, fn in (('login', login), ('get_jwt', get_jwt), ('get_account_info', get_account_info),
                     ('get_device_ids', get_device_ids), ('init_tfa', init_tfa),
                     ('get_tfa_emails', get_tfa_emails), ('send_tfa_email_code', send_tfa_email_code),
                     ('complete_tfa_email_verification', complete_tfa_email_verification),
                     ('finalize_tfa', finalize_tfa)):
        patch(gigya, name, fn)

    async def get_person(self):
        return schemas.KamereonPersonResponseSchema.load(PERSON)

    async def get_account_vehicles(self, account_id):
        st['vehicles_for'].append(account_id)
        body = st['vehicles'] if account_id == 'acc-myr' else {
            'accountId': account_id, 'country': 'FR', 'vehicleLinks': []}
        return schemas.KamereonVehiclesResponseSchema.load(body)

    async def get_vehicle_details(self, account_id, vin):
        return schemas.KamereonVehicleDetailsResponseSchema.load(
            VEHICLES['vehicleLinks'][-1]['vehicleDetails'])

    async def http_request(self, method, endpoint, json=None, *, schema=None):
        # renault-api resolves the short name to the model's full path
        # (``…/cars/{vin}/battery-status`` …); answer by its last part.
        last = endpoint.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
        return _data({'battery-status': st['battery'], 'cockpit': COCKPIT,
                      'location': LOCATION}[last])

    if not deep:
        for name, fn in (('get_person', get_person), ('get_account_vehicles', get_account_vehicles),
                         ('get_vehicle_details', get_vehicle_details), ('http_request', http_request)):
            patch(RenaultSession, name, fn)
        return st

    _UNAUTHORIZED = json.dumps({'errors': [{'errorCode': 'err.func.wired.unauthorized',
                                            'errorMessage': 'Unauthorized'}]})

    async def request(websession, method, url, api_key, gigya_jwt, params, json=None,
                      schema=None, *, wrap_array_in=None):
        """Kamereon, read and checked by renault-api's own schemas."""
        schema = schema or schemas.KamereonResponseSchema
        path = url.split('?')[0].rstrip('/')
        st['kamereon'].append(path.rsplit('/', 1)[-1])
        ok = gigya_jwt in st['jwts'] and gigya_jwt not in st['revoked_jwts']
        if ok:
            try:
                pyjwt.decode(gigya_jwt, JWT_KEY, algorithms=['HS256'])
            except pyjwt.ExpiredSignatureError:
                ok = False
        if not ok:
            schema.loads(_UNAUTHORIZED).raise_for_error_code()
        if st['kamereon_error']:
            raise st['kamereon_error']
        if '/persons/' in path:
            assert path.endswith('/persons/' + PERSON_ID)
            st['person_calls'] += 1
            return schemas.KamereonPersonResponseSchema.load(PERSON)
        if path.endswith('/vehicles'):
            account_id = path.split('/accounts/')[1].split('/')[0]
            st['vehicles_for'].append(account_id)
            body = st['vehicles'] if account_id == 'acc-myr' else {
                'accountId': account_id, 'country': 'FR', 'vehicleLinks': []}
            return schemas.KamereonVehiclesResponseSchema.load(body)
        if path.endswith('/details'):
            return schemas.KamereonVehicleDetailsResponseSchema.load(
                VEHICLES['vehicleLinks'][-1]['vehicleDetails'])
        last = path.rsplit('/', 1)[-1]
        return schema.load({'data': {'type': 'Car', 'id': VIN, 'attributes': {
            'battery-status': st['battery'], 'cockpit': COCKPIT, 'location': LOCATION}[last]}})

    patch(kamereon, 'request', request)
    return st
