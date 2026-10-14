# -*- coding: utf-8 -*-
"""A car read from Home Assistant (v3.0.158).

For cars with no vendor API a private owner can use — the XPENG G6 in
October 2026 — the data often already sits in Home Assistant: the core
Tibber integration (state of charge, target, range, plug, charging) or an
OBD dongle such as the WiCAN Pro over MQTT (state of charge, odometer,
pack values, 12 V). These tests pin how entities are recognised, that a
value HA marks unavailable is no reading, that units arrive as km, and
that the test button names what it picked.

No network: ``urllib.request.urlopen`` is replaced.
"""
import io
import json
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import connector_homeassistant as H  # noqa: E402


def _st(eid, state, unit=None, dc=None, **attrs):
    a = dict(attrs)
    if unit is not None:
        a['unit_of_measurement'] = unit
    if dc is not None:
        a['device_class'] = dc
    return {'entity_id': eid, 'state': state, 'attributes': a,
            'last_updated': attrs.pop('stamp', '2026-10-14T14:00:00+00:00')}


# What the WiCAN guide's renamed MQTT sensors look like: units, no classes.
WICAN = [
    _st('sensor.xpeng_g6_soc', '78.4', '%'),
    _st('sensor.xpeng_g6_odometer', '1234', 'km'),
    _st('sensor.xpeng_g6_hv_v', '402.1', 'V'),
    _st('sensor.xpeng_g6_hv_a', '-12.5', 'A'),
    _st('sensor.xpeng_g6_hv_t_max', '24', '°C'),
    _st('sensor.xpeng_g6_12v', '12.6', 'V'),
]
# Core Tibber vehicle entities: device classes, enum states.
TIBBER = [
    _st('sensor.xpeng_g6_battery_level', '64', '%', 'battery'),
    _st('sensor.xpeng_g6_target_battery_level', '90', '%', 'battery'),
    _st('sensor.xpeng_g6_range', '301000', 'm', 'distance'),
    _st('sensor.xpeng_g6_charging_status', 'charging', None, 'enum'),
    _st('sensor.xpeng_g6_plug_status', 'connected', None, 'enum'),
]
OTHERS = [
    _st('sensor.niro_soc', '50', '%'),                       # another car
    _st('sensor.wohnzimmer_temperatur', '21', '°C'),
    _st('light.xpeng_g6_garage', 'on'),                     # wrong domain
]


class _HA:
    def __init__(self, states, token='good'):
        self.states, self.token, self.paths = states, token, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.paths.append(url)
        if req.get_header('Authorization') != f'Bearer {self.token}':
            raise urllib.error.HTTPError(url, 401, 'no', {}, io.BytesIO(b''))
        body = {'message': 'API running.'} if url.endswith('/api/') else self.states
        r = io.BytesIO(json.dumps(body).encode())
        r.__enter__ = lambda *a: r
        r.__exit__ = lambda *a: False
        return _Ctx(r)


class _Ctx:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        return self.r

    def __exit__(self, *a):
        return False


def _conn(prefix='xpeng_g6', url='http://10.0.0.5:8123', token='good'):
    return H.HomeAssistantConnector({'username': url, 'password': token, 'vin': prefix})


@pytest.fixture
def ha(monkeypatch):
    def install(states, token='good'):
        fake = _HA(states, token)
        monkeypatch.setattr(H.urllib.request, 'urlopen', fake)
        return fake
    return install


def test_an_obd_dongle_over_mqtt_is_recognised_by_units_and_words(ha):
    ha(WICAN + OTHERS)
    s = _conn().get_status()
    assert s.soc_percent == 78
    assert s.odometer_km == 1234
    assert s.raw_data['aux_battery_v'] == 12.6
    assert s.raw_data['entities']['soc'] == 'sensor.xpeng_g6_soc'
    assert 'niro' not in json.dumps(s.raw_data)


def test_the_tibber_integration_is_recognised_by_device_class(ha):
    ha(TIBBER)
    s = _conn().get_status()
    assert s.soc_percent == 64, 'the target must not be taken for the state of charge'
    assert s.charge_limit_ac == 90
    assert s.estimated_range_km == 301                     # metres → km
    assert s.is_charging is True and s.is_plugged_in is True


def test_both_sources_together_fill_each_other(ha):
    ha(TIBBER + WICAN)
    s = _conn().get_status()
    assert s.soc_percent in (64, 78)
    assert s.odometer_km == 1234 and s.estimated_range_km == 301 and s.is_charging


def test_unavailable_is_no_reading(ha):
    ha([_st('sensor.xpeng_g6_soc', 'unavailable', '%'),
        _st('sensor.xpeng_g6_odometer', '1500', 'km')])
    s = _conn().get_status()
    assert s.soc_percent is None and s.odometer_km == 1500


def test_miles_arrive_as_kilometres(ha):
    ha([_st('sensor.xpeng_g6_odometer', '1000', 'mi'),
        _st('sensor.xpeng_g6_soc', '50', '%')])
    s = _conn().get_status()
    assert s.odometer_km == 1609 and s.distance_unit == 'mi'


def test_a_device_tracker_gives_the_position_and_zero_zero_is_dropped(ha):
    tracker = _st('device_tracker.xpeng_g6', 'home', latitude=48.1, longitude=11.5)
    ha(WICAN + [tracker])
    s = _conn().get_status()
    assert (s.location_lat, s.location_lon) == (48.1, 11.5)
    ha(WICAN + [_st('device_tracker.xpeng_g6', 'home', latitude=0, longitude=0)])
    s = _conn().get_status()
    assert s.location_lat is None


def test_charging_power_in_watts_and_an_implied_plug(ha):
    ha([_st('sensor.xpeng_g6_soc', '40', '%'),
        _st('sensor.xpeng_g6_charging_power', '7400', 'W', 'power')])
    s = _conn().get_status()
    assert s.charge_power_kw == 7.4 and s.is_charging and s.is_plugged_in


def test_the_test_button_names_what_it_found(ha):
    ha(WICAN + TIBBER)
    found = _conn().verify_credentials()
    assert 'sensor.xpeng_g6_odometer' in found and 'sensor.xpeng_g6_range' in found


def test_a_prefix_with_no_car_says_so(ha):
    ha(OTHERS)
    with pytest.raises(H.HomeAssistantError) as ex:
        _conn().verify_credentials()
    assert ex.value.kind == 'nothing' and 'xpeng_g6' in str(ex.value)


def test_a_rejected_token_says_token(ha):
    ha(WICAN, token='other')
    with pytest.raises(H.HomeAssistantError) as ex:
        _conn().verify_credentials()
    assert ex.value.kind == 'token'
    assert 'good' not in str(ex.value)


def test_address_forms_are_normalised(ha):
    fake = ha(WICAN)
    for url in ('10.0.0.5:8123', 'http://10.0.0.5:8123/', 'http://10.0.0.5:8123/api'):
        _conn(url=url).get_status()
    assert all(p.startswith('http://10.0.0.5:8123/api/') for p in fake.paths)


def test_an_entity_id_given_as_prefix_still_works(ha):
    ha(WICAN)
    assert _conn(prefix='sensor.xpeng_g6').get_status().odometer_km == 1234


def test_nothing_is_written_to_home_assistant(ha):
    fake = ha(WICAN + TIBBER)
    c = _conn()
    c.verify_credentials()
    c.get_status()
    assert all(p.endswith(('/api/', '/api/states')) for p in fake.paths)


def test_the_brand_is_offered_without_any_package():
    from services.vehicle import registry
    from services.vehicle.catalog import by_key, required_columns
    assert registry.is_installed('homeassistant')
    assert by_key('homeassistant').pkg is None
    assert required_columns('homeassistant') == ['api_username', 'api_password', 'api_vin']


def test_every_string_exists_in_every_language():
    import re
    src = open(H.__file__, encoding='utf-8').read()
    keys = set(re.findall(r"""['"]((?:ha|cred)\.[a-z_]+)['"]""", src))
    assert 'ha.err_nothing_found' in keys and 'cred.help_ha_prefix' in keys
    for lang in ('de', 'en', 'fr', 'es', 'it', 'nl'):
        d = json.load(open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8'))
        assert not sorted(k for k in keys if k not in d), lang


# ── the XPENG G6 facelift in the spec catalog ─────────────────────────

def test_the_g6_facelift_carries_its_lfp_packs_not_the_old_numbers():
    from services.vehicle import build_ev_specs as B
    data = json.load(open(os.path.join(ROOT, 'services', 'vehicle', 'ev_specs.json'),
                          encoding='utf-8'))
    g6 = {r['variant']: r for r in data['vehicles'] if r['make'] == 'XPeng' and r['model'] == 'G6'}
    for year in ('2025', '2026'):
        perf = g6[f'{year} · AWD Performance']
        assert (perf['net_kwh'], perf['dc_kw']) == (80.0, 451.0)
        assert g6[f'{year} · Standard Range']['net_kwh'] == 67.8
    assert '2025 · Ultra Long Range' not in g6
    # a rebuild cannot bring the old rows back
    stale = [{'make': 'XPeng', 'model': 'G6', 'variant': '2025 · AWD Performance', 'net_kwh': 87.5}]
    again = B.apply_corrections(stale)
    assert [r['net_kwh'] for r in again if r['variant'] == '2025 · AWD Performance'] == [80.0]
    assert B.apply_corrections(again) == again
