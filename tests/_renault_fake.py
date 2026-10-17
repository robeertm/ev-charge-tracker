# -*- coding: utf-8 -*-
"""A stand-in for the Renault servers — only the network, never the logic.

Shared by ``test_renault_connector.py`` and ``test_renault_code.py``.
Gigya (sign-in, e-mail code) and Kamereon (account, car data) answer here;
everything between — renault-api's session, its two-factor bookkeeping,
its schemas — runs for real, so a wrong attribute or a challenge that does
not survive between two web requests fails exactly as it would live.
"""
from types import SimpleNamespace

from renault_api import gigya
from renault_api.gigya import exceptions as gigya_exc
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


def _data(attrs):
    return schemas.KamereonVehicleDataResponseSchema.load(
        {'data': {'type': 'Car', 'id': VIN, 'attributes': attrs}})


def install(patch, *, tfa=False):
    """Install the fake servers with ``patch(obj, name, value)``.

    ``tfa=True``: the account asks for an e-mail code until this device
    has been confirmed — the way Gigya behaves with 403101.
    Returns the shared ``state`` dict (calls, switches) for assertions.
    """
    st = {'tfa': tfa, 'confirmed': set(), 'mails': 0, 'logins': [], 'finalize': [],
          'login_error': None, 'battery': dict(BATTERY), 'vehicles': VEHICLES,
          'vehicles_for': [], 'device_requests': 0}

    async def login(websession, root_url, api_key, *, login_id, password, device_ids=None):
        st['logins'].append({'login_id': login_id, 'device_ids': dict(device_ids or {})})
        if st['login_error']:
            raise st['login_error']
        if st['tfa'] and (device_ids or {}).get('gmid') not in st['confirmed']:
            raise gigya_exc.PendingTwoFactorAuthenticationException(
                403101, 'Pending Two-Factor Authentication', 'reg-token-1')
        return SimpleNamespace(get_session_cookie=lambda: 'login-token-1')

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

    for name, fn in (('login', login), ('get_device_ids', get_device_ids), ('init_tfa', init_tfa),
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

    for name, fn in (('get_person', get_person), ('get_account_vehicles', get_account_vehicles),
                     ('get_vehicle_details', get_vehicle_details), ('http_request', http_request)):
        patch(RenaultSession, name, fn)
    return st
