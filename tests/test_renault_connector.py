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
def fake_net(monkeypatch, tmp_path):
    """Replace only the network; keep renault-api's own parsing."""
    monkeypatch.setattr(cr, 'STATE_DIR', str(tmp_path / 'renault'))
    calls = {'login': [], 'vehicles_for': [], 'details': [], 'country': []}
    state = {'battery': dict(BATTERY), 'login_error': None, 'vehicles': VEHICLES,
             'battery_error': None, 'sfdc_links': [],
             # which endpoints the error in battery_error applies to
             'denied_endpoints': ('battery-status',),
             # refuse only when asked with this country (None: always)
             'refuse_country': None,
             # v3.0.171: cars Renault refuses everything for, and cars it
             # sends no details for (a sold or half-linked car)
             'denied_vins': set(), 'no_details': set()}

    async def login(self, login_id, password):
        calls['login'].append((login_id, password, self._locale if hasattr(self, '_locale') else None))
        if state['login_error']:
            raise state['login_error']

    async def get_person(self):
        return schemas.KamereonPersonResponseSchema.load(PERSON)

    async def get_account_vehicles(self, account_id):
        calls['vehicles_for'].append(account_id)
        body = state['vehicles'] if account_id == 'acc-myr' else {
            'accountId': account_id, 'country': 'FR', 'vehicleLinks': state['sfdc_links']}
        return schemas.KamereonVehiclesResponseSchema.load(body)

    async def get_vehicle_details(self, account_id, vin):
        calls['details'].append(vin)
        if vin in state['no_details']:
            schemas.KamereonResponseSchema.loads(PRIVACY_403).raise_for_error_code()
        return schemas.KamereonVehicleDetailsResponseSchema.load(
            VEHICLES['vehicleLinks'][-1]['vehicleDetails'])

    async def http_request(self, method, endpoint, json=None, *, schema=None):
        # renault-api resolves the short name to the model's full path
        # (``…/accounts/{id}/…/cars/{vin}/battery-status`` …); answer by its last part.
        calls.setdefault('data', []).append(endpoint)
        last = endpoint.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
        if any('/%s/' % v in endpoint for v in state['denied_vins']):
            schemas.KamereonResponseSchema.loads(PRIVACY_403).raise_for_error_code()
        err = state['battery_error']
        if isinstance(err, dict):           # refused on some accounts only
            err = next((b for acc, b in err.items() if f'/accounts/{acc}/' in endpoint), None)
        country = await self._get_country()
        calls['country'].append(country)
        if state['refuse_country'] and country != state['refuse_country']:
            err = None
        if last in state['denied_endpoints'] and err:
            # A Kamereon error body, read the way renault-api reads it.
            schemas.KamereonResponseSchema.loads(err).raise_for_error_code()
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


def test_an_email_code_request_in_the_background_points_to_the_test_button(fake_net):
    """The sync cannot ask the owner for a code — it says where to get one.
    (The code flow itself: tests/test_renault_code.py.)"""
    _, state = fake_net
    state['login_error'] = gigya_exc.PendingTwoFactorAuthenticationException(
        403101, 'Pending Two-Factor Authentication', 'reg-token')
    with pytest.raises(RuntimeError) as e:
        _conn().get_status()
    assert 'renault.err_2fa' not in str(e.value)       # translated, not the raw key
    assert not getattr(e.value, 'code_needed', False)


def test_verify_lets_the_real_reason_through(fake_net, monkeypatch):
    """The generic "rejected" hid the session bug for every owner."""
    async def boom(self):
        raise RuntimeError('kamereon said no')
    monkeypatch.setattr(RenaultSession, 'get_person', boom)
    with pytest.raises(RuntimeError, match='kamereon said no'):
        _conn().verify_credentials()


# What Kamereon sends while the car does not share its data (issue #3, seen
# the same way in home-assistant/core#115701): the second message carries the
# reason, renault-api keeps only the first.
PRIVACY_403 = json.dumps({
    'type': 'FUNCTIONAL',
    'messages': [
        {'code': 'err.func.wired.lkcd-authorization.failure', 'message': 'Access is denied for this resource'},
        {'code': 'err.func.wired.lkcd-authorization.failure', 'message': 'Privacy mode currently ON'}],
    'errors': [
        {'errorCode': 'err.func.wired.lkcd-authorization.failure', 'errorMessage': 'Access is denied for this resource'},
        {'errorCode': 'err.func.wired.lkcd-authorization.failure', 'errorMessage': 'Privacy mode currently ON'}],
    'error_reference': 'FUNCTIONAL'})


ALL_DATA = ('battery-status', 'cockpit', 'location')
PRIVACY_ON = json.dumps({'errors': [{'errorCode': 'err.func.privacy.on', 'errorMessage': 'Privacy mode ON'}]})


def test_privacy_mode_in_the_car_says_what_to_switch_on(fake_net):
    # Only err.func.privacy.on names the privacy mode for sure (v3.0.166).
    _, state = fake_net
    state['battery_error'], state['denied_endpoints'] = PRIVACY_ON, ALL_DATA
    with pytest.raises(RuntimeError) as ei:
        _conn().verify_credentials()
    assert str(ei.value) == cr._t('renault.err_privacy')


def test_a_refusal_without_reason_does_not_claim_privacy_mode(fake_net):
    """Issue #4: v3.0.165 told an owner whose privacy mode was OFF to switch it
    off. lkcd only says "denied" — the message must not invent a reason."""
    _, state = fake_net
    state['battery_error'], state['denied_endpoints'] = PRIVACY_403, ALL_DATA
    with pytest.raises(RuntimeError) as ei:
        _conn().verify_credentials()
    assert str(ei.value) == cr._t('renault.err_data_denied')
    assert 'lkcd' not in str(ei.value)
    assert str(ei.value) != cr._t('renault.err_privacy')


def test_only_the_battery_refused_still_gives_odometer_and_position(fake_net):
    """Like Home Assistant: keep what Renault hands out instead of failing."""
    _, state = fake_net
    state['battery_error'] = PRIVACY_403
    state['sfdc_links'] = VEHICLES['vehicleLinks']          # refused on both accounts
    st = _conn().get_status()
    assert st.soc_percent is None and st.is_charging is False
    assert st.odometer_km == 12345 and st.location_lat == 48.85
    assert st.raw_data['vin'] == VIN


def test_the_test_button_says_when_only_part_of_the_data_comes(fake_net):
    _, state = fake_net
    state['battery_error'] = PRIVACY_403
    assert _conn().verify_credentials() == cr._t('renault.partial_no_soc')
    state['battery_error'] = None
    assert _conn().verify_credentials() is None


def test_no_extra_details_request(fake_net):
    """The details come with the vehicle list; fetching them again is one more
    request Renault can refuse (and Home Assistant never sends it)."""
    calls, _ = fake_net
    _conn().get_status()
    assert calls['details'] == []


def test_a_refusal_for_the_wrong_country_is_retried_with_the_accounts_country(fake_net):
    """An empty region means Germany; the account here is French. Renault
    refuses with country=DE — ask again as FR, and remember it."""
    calls, state = fake_net
    state['battery_error'], state['denied_endpoints'] = PRIVACY_403, ALL_DATA
    state['refuse_country'] = 'DE'
    st = _conn(locale='').get_status()
    assert st.soc_percent == 63
    assert 'DE' in calls['country'] and calls['country'][-1] == 'FR'
    # next time: asked as FR straight away, no refusal first
    calls['country'].clear()
    assert _conn(locale='').get_status().soc_percent == 63
    assert set(calls['country']) == {'FR'}


def test_no_retry_when_the_country_already_matches(fake_net):
    calls, state = fake_net
    state['battery_error'], state['denied_endpoints'] = PRIVACY_403, ALL_DATA
    with pytest.raises(RuntimeError):
        _conn().get_status()                              # fr_FR, account FR
    assert len(calls['login']) == 1


def test_a_refused_account_hands_over_to_the_next_one_with_the_same_car(fake_net):
    calls, state = fake_net
    state['battery_error'] = {'acc-myr': PRIVACY_403}
    state['sfdc_links'] = VEHICLES['vehicleLinks']
    st = _conn().get_status()
    assert st.soc_percent == 63 and st.raw_data['vin'] == VIN
    assert calls['vehicles_for'] == ['acc-myr', 'acc-sfdc']
    assert any('/accounts/acc-sfdc/' in e for e in calls['data'])


def test_a_refusal_never_switches_to_another_car(fake_net):
    # Since v3.0.166 a refused battery gives partial data — of the SAME car.
    calls, state = fake_net
    state['battery_error'] = {'acc-myr': PRIVACY_403}
    other = 'VF1AG000000000999'
    state['sfdc_links'] = [{'vin': other, 'vehicleDetails': {
        'vin': other, 'model': {'code': 'XCB1VE', 'label': 'MEGANE E-TECH', 'group': '971'}}}]
    st = _conn().get_status()
    assert st.raw_data['vin'] == VIN and st.soc_percent is None
    assert not any(other in e for e in calls['data'])


def test_another_kamereon_error_still_comes_through(fake_net):
    _, state = fake_net
    state['battery_error'] = json.dumps(
        {'errors': [{'errorCode': 'err.func.wired.overloaded', 'errorMessage': 'Too many requests'}]})
    with pytest.raises(Exception) as ei:
        _conn().get_status()
    assert getattr(ei.value, 'error_code', None) == 'err.func.wired.overloaded'


def test_the_client_gets_the_aiohttp_session_as_websession():
    src = (ROOT / 'services' / 'vehicle' / 'connector_renault.py').read_text(encoding='utf-8')
    assert 'RenaultClient(session=' not in src
    assert 'RenaultClient(websession=' in src


def test_new_messages_exist_in_every_language():
    keys = ('renault.err_login', 'renault.err_2fa', 'renault.err_no_account',
            'renault.err_no_vehicle', 'renault.err_privacy', 'renault.err_data_denied',
            'renault.err_data_denied_all_cars', 'renault.picked_car',
            'renault.partial_no_soc', 'vehicle.err_credentials_rejected')
    for lang in LANGS:
        d = json.loads((ROOT / 'translations' / f'{lang}.json').read_text(encoding='utf-8'))
        for k in keys:
            assert d.get(k), f'{k} missing in {lang}'
    # the base message is no longer German-only in the code
    base = (ROOT / 'services' / 'vehicle' / 'base.py').read_text(encoding='utf-8')
    assert "RuntimeError('Die hinterlegten Zugangsdaten wurden abgelehnt.')" not in base


@pytest.mark.parametrize('lang, renault_fr, kia_eu', [
    ('fr', 'France', 'Europe'), ('en', 'France', 'Europe'), ('de', 'Frankreich', 'Europa'),
    ('it', 'Francia', 'Europa'), ('nl', 'Frankrijk', 'Europa'), ('es', 'Francia', 'Europa')])
def test_region_choices_speak_the_ui_language(lang, renault_fr, kia_eu):
    """The region list was German in every language ("Frankreich",
    "Oesterreich", "Europa") — a French owner had to find "Frankreich"."""
    import app as A
    from services.i18n import get_language, set_language
    vorher = get_language()
    try:
        set_language(lang)
        felder = A._credential_fields_for_ui()
    finally:
        set_language(vorher)

    def options(brand):
        f = next(f for f in felder[brand] if f.get('options'))
        return {o['value']: o['label'] for o in f['options']}
    assert options('renault')['fr_FR'] == renault_fr
    assert options('kia')['EU'] == kia_eu
    for brand in ('renault', 'dacia', 'kia', 'hyundai', 'mg'):
        for label in options(brand).values():
            assert label and not label.startswith('set.'), (brand, label)


# ── v3.0.171: which car, without a VIN (issue #4) ──────────────────────
# The connector took the FIRST car Renault listed. An owner whose list
# starts with a car Renault refuses (sold, half-linked) got "access denied"
# for everything — Home Assistant, which goes through all cars, showed his
# Mégane. Now: every car is tried, cars without details are skipped, the one
# that answers is remembered.
OLD = 'VF1AG000000000777'


def _two_cars(first_details=True):
    details = {'vin': OLD, 'model': {'code': 'X10', 'label': 'ZOE', 'group': '971'}} if first_details else None
    return {'accountId': 'acc-myr', 'country': 'FR', 'vehicleLinks': [
        {'vin': OLD, 'vehicleDetails': details}, VEHICLES['vehicleLinks'][0]]}


def test_without_a_vin_the_car_that_answers_is_found_and_kept(fake_net):
    calls, state = fake_net
    state['vehicles'] = _two_cars()
    state['denied_vins'] = {OLD}
    st = _conn().get_status()
    assert st.raw_data['vin'] == VIN and st.soc_percent == 63
    # remembered: the next sync asks only for the Mégane
    calls['data'].clear()
    st = _conn().get_status()
    assert st.raw_data['vin'] == VIN
    assert calls['data'] and not any(OLD in e for e in calls['data'])


def test_a_car_without_details_is_skipped_like_home_assistant(fake_net):
    calls, state = fake_net
    state['vehicles'] = _two_cars(first_details=False)
    state['no_details'] = {OLD}
    st = _conn().get_status()
    assert st.raw_data['vin'] == VIN
    assert OLD in calls['details']                       # asked once, like HA
    assert not any(OLD in e for e in calls['data'])      # but never for its data


def test_the_test_button_names_the_car_it_found(fake_net):
    _, state = fake_net
    state['vehicles'] = _two_cars()
    state['denied_vins'] = {OLD}
    note = _conn().verify_credentials()
    assert note and 'MEGANE E-TECH (VF1…0001)' in note and 'ZOE (VF1…0777)' in note


def test_all_cars_refused_says_so_and_lists_them(fake_net):
    _, state = fake_net
    state['vehicles'] = _two_cars()
    state['denied_vins'] = {OLD, VIN}
    with pytest.raises(RuntimeError) as ei:
        _conn().get_status()
    msg = str(ei.value)
    assert 'renault.err_data_denied_all_cars' not in msg     # translated
    assert 'VF1…0777' in msg and 'VF1…0001' in msg


def test_an_entered_vin_is_never_left_for_another_car(fake_net):
    calls, state = fake_net
    state['vehicles'] = _two_cars()
    state['denied_vins'] = {OLD}
    with pytest.raises(RuntimeError):
        _conn(vin=OLD).get_status()
    assert not any(VIN in e for e in calls['data'])


def test_a_remembered_car_that_left_the_account_is_forgotten(fake_net):
    calls, state = fake_net
    state['vehicles'] = _two_cars()
    state['denied_vins'] = {OLD}
    _conn().get_status()                                  # remembers the Mégane
    state['vehicles'] = {'accountId': 'acc-myr', 'country': 'FR', 'vehicleLinks': [
        {'vin': OLD, 'vehicleDetails': _two_cars()['vehicleLinks'][0]['vehicleDetails']}]}
    state['denied_vins'] = set()
    assert _conn().get_status().raw_data['vin'] == OLD     # gone → looked again


def test_the_vin_field_is_offered_for_renault_and_dacia():
    for c in (cr.RenaultConnector, cr.DaciaConnector):
        f = {x['key']: x for x in c.credential_fields()}
        assert 'vin' in f and f['vin'].get('optional')


def test_every_refusal_is_logged_not_only_the_battery(fake_net, caplog):
    import logging
    _, state = fake_net
    state['battery_error'], state['denied_endpoints'] = PRIVACY_403, ALL_DATA
    with caplog.at_level(logging.INFO, logger=cr.logger.name):
        with pytest.raises(RuntimeError):
            _conn().get_status()
    text = caplog.text
    assert 'battery-status refused' in text and 'cockpit refused' in text and 'location refused' in text
    assert VIN not in text                                # masked
