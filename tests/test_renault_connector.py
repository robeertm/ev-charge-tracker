# -*- coding: utf-8 -*-
"""Renault / Dacia connector against the REAL renault-api models (v3.0.161).

The connector had never signed anyone in: it handed the aiohttp session to
``RenaultClient(session=...)``, which expects renault-api's own
RenaultSession, so every sync ended in "'ClientSession' object has no
attribute 'login'" and the Test button said "credentials rejected". Behind
that waited three more misreadings of the models (snake_case account fields
that do not exist, ``model`` being an object, ``chargingStatus`` being a
state code).

So these tests do not hand-build look-alike objects. Only the network is
replaced: ``RenaultSession``'s HTTP calls return what Kamereon sends,
parsed by renault-api's own schemas, so a wrong attribute name fails here
exactly as it failed for the owner.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

renault_api = pytest.importorskip('renault_api')
from renault_api.renault_session import RenaultSession  # noqa: E402
from renault_api.kamereon import schemas  # noqa: E402
from renault_api.gigya import exceptions as gigya_exc  # noqa: E402

from services.vehicle import connector_renault as cr  # noqa: E402

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')
VIN = 'VF1AG000000000001'

PERSON = {'accounts': [
    {'accountId': 'acc-sfdc', 'accountType': 'SFDC', 'accountStatus': 'ACTIVE'},
    {'accountId': 'acc-myr', 'accountType': 'MYRENAULT', 'accountStatus': 'ACTIVE'},
]}
VEHICLES = {'accountId': 'acc-myr', 'country': 'FR', 'vehicleLinks': [
    {'vin': VIN, 'vehicleDetails': {
        'vin': VIN, 'model': {'code': 'XCB1VE', 'label': 'MEGANE E-TECH', 'group': '971'}}},
]}


def _data(attrs):
    return schemas.KamereonVehicleDataResponseSchema.load(
        {'data': {'type': 'Car', 'id': VIN, 'attributes': attrs}})


BATTERY = {'timestamp': '2026-10-17T08:00:00Z', 'batteryLevel': 63, 'batteryAutonomy': 260,
           'plugStatus': 1, 'chargingStatus': 1.0}
COCKPIT = {'totalMileage': 12345.6}
LOCATION = {'gpsLatitude': 48.85, 'gpsLongitude': 2.35, 'lastUpdateTime': '2026-10-17T08:00:00Z'}


@pytest.fixture
def fake_net(monkeypatch):
    """Replace only the network; keep renault-api's own parsing."""
    calls = {'login': [], 'vehicles_for': []}
    state = {'battery': dict(BATTERY), 'login_error': None, 'vehicles': VEHICLES}

    async def login(self, login_id, password):
        calls['login'].append((login_id, password, self._locale if hasattr(self, '_locale') else None))
        if state['login_error']:
            raise state['login_error']

    async def get_person(self):
        return schemas.KamereonPersonResponseSchema.load(PERSON)

    async def get_account_vehicles(self, account_id):
        calls['vehicles_for'].append(account_id)
        body = state['vehicles'] if account_id == 'acc-myr' else {
            'accountId': account_id, 'country': 'FR', 'vehicleLinks': []}
        return schemas.KamereonVehiclesResponseSchema.load(body)

    async def get_vehicle_details(self, account_id, vin):
        return schemas.KamereonVehicleDetailsResponseSchema.load(
            VEHICLES['vehicleLinks'][-1]['vehicleDetails'])

    async def http_request(self, method, endpoint, json=None, *, schema=None):
        # renault-api resolves the short name to the model's full path
        # (``…/cars/{vin}/battery-status`` …); answer by its last part.
        last = endpoint.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
        return _data({'battery-status': state['battery'], 'cockpit': COCKPIT,
                      'location': LOCATION}[last])

    monkeypatch.setattr(RenaultSession, 'login', login)
    monkeypatch.setattr(RenaultSession, 'get_person', get_person)
    monkeypatch.setattr(RenaultSession, 'get_account_vehicles', get_account_vehicles)
    monkeypatch.setattr(RenaultSession, 'get_vehicle_details', get_vehicle_details)
    monkeypatch.setattr(RenaultSession, 'http_request', http_request)
    return calls, state


def _conn(**extra):
    creds = {'username': 'owner@example.invalid', 'password': 'pw', 'locale': 'fr_FR', 'vin': ''}
    creds.update(extra)
    return cr.RenaultConnector(creds)


def test_sync_signs_in_and_reads_the_car(fake_net):
    calls, _ = fake_net
    st = _conn().get_status()
    assert calls['login'] and calls['login'][0][:2] == ('owner@example.invalid', 'pw')
    assert st.soc_percent == 63
    assert st.estimated_range_km == 260
    assert st.odometer_km == 12345
    assert st.is_plugged_in is True and st.is_charging is True
    assert (st.location_lat, st.location_lon) == (48.85, 2.35)
    assert st.vehicle_model == 'MEGANE E-TECH'
    assert st.raw_data['vin'] == VIN


def test_the_myrenault_account_is_asked_first(fake_net):
    calls, _ = fake_net
    _conn().get_status()
    assert calls['vehicles_for'][0] == 'acc-myr'


def test_a_car_on_a_later_account_is_still_found(fake_net, monkeypatch):
    calls, state = fake_net
    # Dacia prefers MYDACIA, which this person does not have: the SFDC
    # account comes first and is empty — the car must still be found.
    st = cr.DaciaConnector({'username': 'u@example.invalid', 'password': 'pw',
                            'locale': 'fr_FR', 'vin': ''}).get_status()
    assert calls['vehicles_for'] == ['acc-sfdc', 'acc-myr']
    assert st.raw_data['vin'] == VIN


@pytest.mark.parametrize('code, charging', [
    (0.0, False), (0.1, False), (0.2, False), (0.3, False), (0.4, False), (1.0, True), (-1.0, False)])
def test_only_charge_in_progress_counts_as_charging(fake_net, code, charging):
    _, state = fake_net
    state['battery']['chargingStatus'] = code
    assert _conn().get_status().is_charging is charging


def test_an_entered_vin_picks_that_car(fake_net):
    _, state = fake_net
    other = 'VF1AG000000000002'
    state['vehicles'] = {'accountId': 'acc-myr', 'country': 'FR', 'vehicleLinks': [
        {'vin': other, 'vehicleDetails': None}, VEHICLES['vehicleLinks'][0]]}
    assert _conn(vin=VIN.lower()).get_status().raw_data['vin'] == VIN
    with pytest.raises(RuntimeError):
        _conn(vin='VF1XXXXXXXXXXXXXX').get_status()


def test_an_empty_region_falls_back_instead_of_breaking(fake_net):
    assert _conn(locale='').get_status().soc_percent == 63


def test_wrong_password_says_so(fake_net):
    _, state = fake_net
    state['login_error'] = gigya_exc.InvalidCredentialsException(403042, 'invalid loginID or password')
    c = _conn()
    with pytest.raises(RuntimeError) as e:
        c.verify_credentials()
    assert 'My Renault' in str(e.value)
    assert c.test_connection() is False


def test_an_email_code_request_explains_the_way_out(fake_net):
    _, state = fake_net
    state['login_error'] = gigya_exc.PendingTwoFactorAuthenticationException(
        403101, 'Pending Two-Factor Authentication', 'reg-token')
    with pytest.raises(RuntimeError) as e:
        _conn().verify_credentials()
    assert 'renault.err_2fa' not in str(e.value)       # translated, not the raw key
    assert 'browser' in str(e.value).lower() or 'Browser' in str(e.value)


def test_verify_lets_the_real_reason_through(fake_net, monkeypatch):
    """The generic "rejected" hid the session bug for every owner."""
    async def boom(self):
        raise RuntimeError('kamereon said no')
    monkeypatch.setattr(RenaultSession, 'get_person', boom)
    with pytest.raises(RuntimeError, match='kamereon said no'):
        _conn().verify_credentials()


def test_the_client_gets_the_aiohttp_session_as_websession():
    src = (ROOT / 'services' / 'vehicle' / 'connector_renault.py').read_text(encoding='utf-8')
    assert 'RenaultClient(session=' not in src
    assert 'RenaultClient(websession=' in src


def test_new_messages_exist_in_every_language():
    keys = ('renault.err_login', 'renault.err_2fa', 'renault.err_no_account',
            'renault.err_no_vehicle', 'vehicle.err_credentials_rejected')
    for lang in LANGS:
        d = json.loads((ROOT / 'translations' / f'{lang}.json').read_text(encoding='utf-8'))
        for k in keys:
            assert d.get(k), f'{k} missing in {lang}'
    # the base message is no longer German-only in the code
    base = (ROOT / 'services' / 'vehicle' / 'base.py').read_text(encoding='utf-8')
    assert "RuntimeError('Die hinterlegten Zugangsdaten wurden abgelehnt.')" not in base
