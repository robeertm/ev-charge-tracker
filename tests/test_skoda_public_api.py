# -*- coding: utf-8 -*-
"""The official Škoda API: mapping, quota and the remote-control gates.

Škoda retires the unofficial access in October 2026. These tests pin the
three things that are easy to get wrong and impossible to notice until a
real car is involved:

  * the mapping from the API's shapes to ours (metres vs kilometres, a
    charging state that means "plug the cable in" rather than "plugged
    in", and a position that must NOT be read while the car is moving);
  * the hourly quota, which is small enough — 20 requests per VIN — that
    a background poll could spend the user's own refresh;
  * the remote-control gates, because the commands move a real car.

No network: the transport is replaced. Everything below is shaped after
the published OpenAPI document, not after a guess.
"""
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.vehicle import skoda_public_api as api  # noqa: E402
from services.vehicle.base import SENSITIVE_COMMANDS  # noqa: E402
from services.vehicle.connector_skoda_public import SkodaPublicConnector  # noqa: E402


# ── test doubles ──────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload, headers=None, status=200):
        self._b = json.dumps(payload).encode()
        self.headers = headers or {}
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(**over):
    """A response shaped like the documented one, Enyaq-ish."""
    veh = {
        'vin': 'TMBTESTVIN000001',
        'name': 'Enyaq',
        'odometer': {'mileageInKm': 33377},
        'charging': {
            'carCapturedTimestamp': '2026-09-09T15:00:00Z',
            'status': {
                'state': 'CHARGING',
                'chargeType': 'AC',
                'chargePowerInKw': 10.5,
                'remainingTimeToFullyChargedInMinutes': 95,
                'battery': {'stateOfChargeInPercent': 70,
                            'remainingCruisingRangeInMeters': 285000},
            },
            'settings': {'targetStateOfChargeInPercent': 80},
        },
        'parkingPosition': {'state': 'PARKED',
                            'gpsCoordinates': {'latitude': 48.1, 'longitude': 11.5},
                            'formattedAddress': 'Irgendwo 1'},
        'status': {'overall': {'locked': 'YES'}, 'detail': {'trunk': 'CLOSED',
                                                            'bonnet': 'CLOSED'}},
        'fuelStatus': {'primaryEngineRange': {'engineType': 'ELECTRIC',
                                              'currentSoCInPercent': 70,
                                              'remainingRangeInKm': 285}},
    }
    veh.update(over)
    return {'vehicle': veh}


@pytest.fixture(autouse=True)
def _fresh_budget():
    """Each test starts with no memory of the quota."""
    api._budget._state.clear()
    yield
    api._budget._state.clear()


def _connector():
    return SkodaPublicConnector({'password': 'KEY', 'vin': 'TMBTESTVIN000001'})


# ── mapping ───────────────────────────────────────────────────────────
def test_the_documented_response_maps_to_our_status(monkeypatch):
    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: _Resp(_payload()))
    s = _connector().get_status()
    assert s.soc_percent == 70
    assert s.odometer_km == 33377
    assert s.is_charging is True
    assert s.is_plugged_in is True
    assert s.charge_power_kw == pytest.approx(10.5)
    assert s.charge_limit_ac == 80
    assert s.est_charge_duration_min == 95
    assert (s.location_lat, s.location_lon) == (48.1, 11.5)


def test_range_arrives_in_metres_and_must_not_be_reported_as_kilometres(monkeypatch):
    """285000 m is 285 km. Passing the raw number through would claim a
    range no electric car has."""
    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: _Resp(_payload()))
    assert _connector().get_status().estimated_range_km == 285


def test_a_moving_car_reports_no_position_and_we_must_not_invent_one(monkeypatch):
    """With state IN_MOTION the API omits coordinates. Reading the empty
    dict as 0/0 would drop a pin in the Gulf of Guinea and corrupt the
    trip log with a trip across the planet."""
    p = _payload(parkingPosition={'state': 'IN_MOTION'})
    monkeypatch.setattr(api.urllib.request, 'urlopen', lambda *a, **k: _Resp(p))
    s = _connector().get_status()
    assert s.location_lat is None and s.location_lon is None


def test_connect_cable_means_the_cable_is_NOT_connected(monkeypatch):
    """The state names the action the car wants, not the state it is in."""
    p = _payload()
    p['vehicle']['charging']['status']['state'] = 'CONNECT_CABLE'
    monkeypatch.setattr(api.urllib.request, 'urlopen', lambda *a, **k: _Resp(p))
    s = _connector().get_status()
    assert s.is_charging is False
    assert s.is_plugged_in is False


@pytest.mark.parametrize('state,plugged', [
    ('CHARGING', True), ('CONSERVING', True), ('READY_FOR_CHARGING', True),
    ('CHARGING_INTERRUPTED', True), ('CONNECT_CABLE', False),
])
def test_plugged_in_is_derived_from_the_documented_states(monkeypatch, state, plugged):
    p = _payload()
    p['vehicle']['charging']['status']['state'] = state
    monkeypatch.setattr(api.urllib.request, 'urlopen', lambda *a, **k: _Resp(p))
    assert _connector().get_status().is_plugged_in is plugged


# ── quota ─────────────────────────────────────────────────────────────
def test_background_polling_leaves_room_for_the_person(monkeypatch):
    """20 requests an hour is little. A background sync that spends the
    last one turns the user's own "sync now" into an error, so it stops
    early — and a user-driven call may still use the reserve."""
    headers = {'RateLimit-Limit': '20', 'RateLimit-Remaining': '2',
               'RateLimit-Reset': '600'}
    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: _Resp(_payload(), headers))
    c = _connector()
    c.get_status()                      # primes the budget from the headers

    with pytest.raises(api.SkodaApiError) as e:
        c.get_status(force=False)       # background
    assert e.value.kind == 'rate_limited'
    assert e.value.retry_after is not None

    c.get_status(force=True)            # a person is waiting — allowed


def test_an_exhausted_window_refuses_even_the_person(monkeypatch):
    headers = {'RateLimit-Limit': '20', 'RateLimit-Remaining': '0',
               'RateLimit-Reset': '900'}
    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: _Resp(_payload(), headers))
    c = _connector()
    c.get_status()
    with pytest.raises(api.SkodaApiError) as e:
        c.get_status(force=True)
    assert e.value.kind == 'rate_limited'


def test_the_quota_is_remembered_from_a_429_too(monkeypatch):
    """Otherwise we would keep hammering a closed door for the whole
    window — and every attempt is itself a request."""
    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            'u', 429, 'Too Many Requests',
            {'RateLimit-Remaining': '0', 'RateLimit-Reset': '1200',
             'Retry-After': '1200'},
            None)
    monkeypatch.setattr(api.urllib.request, 'urlopen', _raise)
    with pytest.raises(api.SkodaApiError):
        api.get_vehicle('KEY', 'VIN', spend_reserve=True)
    assert api.budget_state('VIN').get('remaining') == 0


# ── errors the user can act on ────────────────────────────────────────
@pytest.mark.parametrize('code,ptype,kind', [
    (401, 'https://x/api-key-expired', 'key_expired'),
    (401, 'https://x/api-key-invalid', 'key_invalid'),
    (403, 'https://x/api-key-not-authorized', 'key_not_authorized'),
    (404, '', 'unknown_vin'),
    (503, '', 'asleep'),
])
def test_each_failure_says_what_to_do_about_it(monkeypatch, code, ptype, kind):
    body = json.dumps({'type': ptype, 'detail': 'x'}).encode()

    class _Err(urllib.error.HTTPError):
        def __init__(self):
            super().__init__('u', code, 'e', {}, None)

        def read(self):
            return body

    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(_Err()))
    with pytest.raises(api.SkodaApiError) as e:
        api.get_vehicle('KEY', 'VIN', spend_reserve=True)
    assert e.value.kind == kind


def test_the_key_expiry_is_carried_through_so_the_ui_can_warn_in_time(monkeypatch):
    monkeypatch.setattr(
        api.urllib.request, 'urlopen',
        lambda *a, **k: _Resp(_payload(), {'X-API-Key-Expires-At': '2026-12-01T00:00:00Z'}))
    s = _connector().get_status()
    assert s.raw_data['key_expires_at'] == '2026-12-01T00:00:00Z'


# ── remote control ────────────────────────────────────────────────────
def test_a_command_without_the_opt_in_is_refused_in_the_api_layer():
    """Not only in the UI. A hidden button is not a safety property."""
    with pytest.raises(api.SkodaApiError) as e:
        api.send_command('KEY', 'VIN', 'charging_start', enabled=False)
    assert e.value.kind == 'remote_disabled'


def test_unlocking_counts_as_sensitive():
    """Allowing pre-heating in winter is not the same decision as being
    one stray click from unlocking the car in a car park."""
    assert 'unlock' in SENSITIVE_COMMANDS
    assert 'ac_start' not in SENSITIVE_COMMANDS


def test_the_official_skoda_api_cannot_unlock_and_does_not_pretend_to():
    assert 'unlock' not in _connector().remote_commands()
    assert 'charging_start' in _connector().remote_commands()


@pytest.mark.parametrize('pct', [0, 49, 101, 200])
def test_an_impossible_charge_limit_is_rejected_before_it_reaches_the_car(pct):
    with pytest.raises(api.SkodaApiError) as e:
        api.send_command('KEY', 'VIN', 'charging_limit', enabled=True,
                         params={'target_soc': pct})
    assert e.value.kind == 'bad_request'


def test_a_command_is_reported_as_sent_not_as_done(monkeypatch):
    """The API answers 202: the car has been asked, not yet obeyed."""
    monkeypatch.setattr(api.urllib.request, 'urlopen',
                        lambda *a, **k: _Resp({}, {}, status=202))
    res = api.send_command('KEY', 'VIN', 'charging_start', enabled=True)
    assert res == {'accepted': True, 'status': 202}


if __name__ == '__main__':
    raise SystemExit(pytest.main([os.path.abspath(__file__), '-v']))


# ── lock state: three answers, not two (found on a real car) ──────────
@pytest.mark.parametrize('overall,expect_locked,expect_known', [
    ({'reliableLockStatus': 'LOCKED'},            True,  True),
    ({'reliableLockStatus': 'UNLOCKED'},          False, True),
    ({'locked': 'YES'},                           True,  True),
    ({'locked': 'NO'},                            False, True),
    # The documented third value, and the reason this test exists: a
    # car whose lock state the system cannot read must not be shown as
    # standing open.
    ({'locked': 'UNKNOWN'},                       True,  False),
    ({'reliableLockStatus': 'UNKNOWN', 'locked': 'UNKNOWN'}, True, False),
    ({},                                          True,  False),
])
def test_an_unknown_lock_state_is_never_reported_as_unlocked(
        monkeypatch, overall, expect_locked, expect_known):
    p = _payload()
    p['vehicle']['status']['overall'] = overall
    monkeypatch.setattr(api.urllib.request, 'urlopen', lambda *a, **k: _Resp(p))
    s = _connector().get_status()
    assert s.is_locked is expect_locked
    assert s.raw_data['lock_status_known'] is expect_known


def test_every_remote_command_has_a_label_in_every_language():
    """A missing label renders the raw command key on the button —
    'charging_limit' sat between 'Laden stoppen' and 'Klima starten'
    until a screenshot showed it. Buttons are the one place where a
    fallback to the key is guaranteed to be seen."""
    import json
    import pathlib
    from services.vehicle.base import REMOTE_COMMANDS
    root = pathlib.Path(__file__).resolve().parent.parent
    for lang in ('de', 'en', 'es', 'fr', 'it', 'nl'):
        d = json.loads((root / 'translations' / f'{lang}.json').read_text())
        missing = [c for c in REMOTE_COMMANDS if f'remote.cmd_{c}' not in d]
        assert not missing, f'{lang}: no label for {missing}'
