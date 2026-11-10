# -*- coding: utf-8 -*-
"""The XPENG G6 read through the community XPENG Home Assistant integration.

The entity ids, units and state words below are taken from the integration's
code (version 0.6.44), device renamed so every id starts with ``xpeng_g6``.
Pinned here: the charging state word is read as its own enum ("Connected" is a
cable, not a charge), a real charging entity beats the pack-current guess, the
data freshness is the integration's ``last_data_update`` (not HA's write time),
the sign-in flag and polling status travel with the reading, the GPS fix time
is counted back from the tracker's own fix age, and nothing that merely looks
like a range, a state of charge or a charge is taken for one.

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

WRITTEN = '2026-11-10T14:05:00+00:00'            # when HA wrote the states
DATA = '2026-11-10T14:03:12+00:00'               # when the car last reported
ENUM = ['Disconnected', 'Connected', 'Charging', 'Complete', 'Error', 'Idle', 'Scheduled']


def _st(eid, state, **attrs):
    return {'entity_id': eid, 'state': state, 'attributes': attrs,
            'last_updated': attrs.pop('stamp', WRITTEN)}


def _car(charge_state='Disconnected', plugged='off', power='0.0', soc='78', **extra):
    """What a parked/charging G6 looks like in HA (compact layout)."""
    st = [
        _st('sensor.xpeng_g6_battery', soc, unit_of_measurement='%', device_class='battery',
            state_class='measurement'),
        _st('sensor.xpeng_g6_range', '392', unit_of_measurement='km', device_class='distance'),
        _st('sensor.xpeng_g6_odometer', '4183', unit_of_measurement='km', device_class='distance',
            state_class='total_increasing'),
        _st('sensor.xpeng_g6_charging_state', charge_state, device_class='enum', options=ENUM),
        _st('sensor.xpeng_g6_charge_power', power, unit_of_measurement='kW', device_class='power'),
        _st('sensor.xpeng_g6_charge_voltage', '232.0', unit_of_measurement='V', device_class='voltage'),
        _st('sensor.xpeng_g6_charge_current', '48.0', unit_of_measurement='A', device_class='current'),
        _st('sensor.xpeng_g6_charge_current_limit', '16', unit_of_measurement='A'),
        _st('sensor.xpeng_g6_charge_time_remaining', '195', unit_of_measurement='min'),
        _st('sensor.xpeng_g6_max_charge', '90', unit_of_measurement='%'),
        _st('sensor.xpeng_g6_last_charge_range_added', '120', unit_of_measurement='km'),
        _st('sensor.xpeng_g6_fast_charge_added_range', '30', unit_of_measurement='km'),
        _st('sensor.xpeng_g6_speed', '0.0', unit_of_measurement='km/h', device_class='speed'),
        _st('sensor.xpeng_g6_cabin_temperature', '19', unit_of_measurement='°C',
            device_class='temperature'),
        _st('sensor.xpeng_g6_polling_status', 'Parked', cadence='parked', mqtt_connected=True),
        _st('sensor.xpeng_g6_last_data_update', DATA, device_class='timestamp',
            timestamp='2026-11-10T14:03:12.482913+00:00', age_seconds=3),
        _st('sensor.xpeng_g6_last_data_timestamp', DATA, device_class='timestamp'),
        _st('binary_sensor.xpeng_g6_plugged_in', plugged, device_class='plug'),
        _st('binary_sensor.xpeng_g6_credentials_valid', 'on', reason='ok'),
        _st('binary_sensor.xpeng_g6_mqtt_connected', 'on', device_class='connectivity'),
        _st('binary_sensor.xpeng_g6_doors', 'off', device_class='door'),
        _st('lock.xpeng_g6_door_lock', 'locked'),
        _st('switch.xpeng_g6_maximum_heating', 'off'),
        _st('number.xpeng_g6_charge_limit', '90', unit_of_measurement='%'),
        _st('device_tracker.xpeng_g6_location', 'home', source_type='gps', latitude=51.0,
            longitude=14.0, gps_accuracy=0, speed=0.0, bearing=212.0, coord_valid=True,
            fix_age_s=2710),
    ]
    st.extend(extra.get('more', []))
    return st


class _Ctx:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        return self.r

    def __exit__(self, *a):
        return False


@pytest.fixture
def ha(monkeypatch, tmp_path):
    monkeypatch.setattr(H, 'STATE_DIR', str(tmp_path))

    def install(states):
        def fake(req, timeout=None):
            url = req.full_url
            body = {'message': 'API running.'} if url.endswith('/api/') else states
            r = io.BytesIO(json.dumps(body).encode())
            return _Ctx(r)
        monkeypatch.setattr(H.urllib.request, 'urlopen', fake)
    return install


def _status():
    return H.HomeAssistantConnector({'username': 'http://10.0.0.5:8123', 'password': 't',
                                     'vin': 'xpeng_g6'}).get_status()


def test_parked_car_reads_soc_km_range_and_no_charge(ha):
    ha(_car())
    s = _status()
    assert s.soc_percent == 78
    assert s.odometer_km == 4183
    assert s.estimated_range_km == 392, 'the range added by a charge is not the range'
    assert s.is_charging is False and s.is_plugged_in is False
    assert s.charge_power_kw == 0.0
    ent = s.raw_data['entities']
    assert ent['soc'] == 'sensor.xpeng_g6_battery'
    assert ent['range'] == 'sensor.xpeng_g6_range'
    assert ent['odometer'] == 'sensor.xpeng_g6_odometer'
    assert ent['power'] == 'sensor.xpeng_g6_charge_power'
    assert ent['plug'] == 'binary_sensor.xpeng_g6_plugged_in'
    assert ent['location'] == 'device_tracker.xpeng_g6_location'
    assert ent['charging_state'] == 'sensor.xpeng_g6_charging_state'
    assert 'pack_current' not in ent and 'pack_voltage' not in ent, \
        'charge voltage/current are the charger, not the traction pack'
    assert s.raw_data['speed_kmh'] == 0.0


def test_the_charge_target_is_not_taken_from_the_max_charge_percent_sensor_as_soc(ha):
    ha(_car(soc='41'))
    assert _status().soc_percent == 41


def test_charging_state_word_charging_is_a_charge_with_a_cable(ha):
    ha(_car('Charging', plugged='on', power='11.2', soc='54'))
    s = _status()
    assert s.is_charging is True and s.is_plugged_in is True
    assert s.charge_power_kw == 11.2
    assert s.raw_data['charge_state_word'] == 'Charging'
    assert s.raw_data['charge_inference']['mode'] == 'entity'


def test_connected_means_a_cable_not_a_charge(ha):
    ha(_car('Connected', plugged='on'))
    s = _status()
    assert s.is_plugged_in is True
    assert s.is_charging is False, '"Connected" must not be read as "on" like a plug entity'


@pytest.mark.parametrize('word,charging,plugged', [
    ('Disconnected', False, False), ('Connected', False, True), ('Charging', True, True),
    ('Complete', False, True), ('Idle', False, False), ('Scheduled', False, False),
])
def test_every_charging_state_word(ha, word, charging, plugged):
    # the plug entity says "unknown" (a waiting schedule): the word decides what it can
    ha(_car(word, plugged='unknown'))
    s = _status()
    assert s.is_charging is charging
    # Idle/Scheduled say nothing about the cable -> not plugged (no reading)
    assert s.is_plugged_in is (plugged if word not in ('Idle', 'Scheduled') else False)


def test_error_state_is_no_reading_for_charging(ha):
    ha(_car('Error', plugged='unknown', power='0.0'))
    s = _status()
    assert s.is_charging is False        # power 0 and no word -> not charging, not invented


def test_the_charging_switch_of_the_compact_layout_wins(ha):
    ha(_car('Disconnected', plugged='off') +
       [_st('switch.xpeng_g6_charging', 'on', charging_state='Charging', cable_connected=True)])
    s = _status()
    assert s.is_charging is True
    assert s.raw_data['entities']['charging'] == 'switch.xpeng_g6_charging'
    assert s.raw_data['charge_inference']['mode'] == 'entity'


def test_other_switches_are_not_taken_for_anything(ha):
    ha(_car() + [_st('switch.xpeng_g6_maximum_heating', 'on'), _st('switch.xpeng_g6_refresh', 'on')])
    s = _status()
    assert s.is_charging is False
    assert all(not v.startswith('switch.') for v in s.raw_data['entities'].values())


def test_a_real_charging_entity_beats_the_pack_current_guess(ha, tmp_path):
    # a pack current in the charging sense with the car standing would infer "charging" ...
    wican = [_st('sensor.xpeng_g6_hv_a', '-20', unit_of_measurement='A'),
             _st('sensor.xpeng_g6_hv_v', '402', unit_of_measurement='V')]
    ha(_car('Disconnected', plugged='off', more=wican))
    s = _status()
    assert s.raw_data['pack_current_a'] == -20.0
    assert s.is_charging is False, 'the real entity says "Disconnected"'
    assert s.raw_data['charge_inference']['mode'] == 'entity'


def test_freshness_is_the_cars_last_report_not_the_time_ha_wrote(ha):
    ha(_car())
    s = _status()
    assert s.last_updated == DATA
    assert s.last_updated != WRITTEN
    assert getattr(s, 'captured_at', None) is None, 'arrival times keep using the poll time'
    assert s.raw_data['data_time'] == DATA


def test_a_stamp_with_another_offset_is_normalised_to_utc(ha):
    states = _car()
    for st in states:
        if st['entity_id'] == 'sensor.xpeng_g6_last_data_update':
            st['state'] = '2026-11-10T15:03:12+01:00'
    ha(states)
    assert _status().last_updated == DATA


def test_without_the_integration_stamp_ha_time_stays_the_fallback(ha):
    states = [s for s in _car() if 'last_data' not in s['entity_id']]
    ha(states)
    s = _status()
    assert s.last_updated == WRITTEN


def test_unknown_data_stamp_is_no_freshness_at_all(ha):
    states = _car()
    for st in states:
        if st['entity_id'] == 'sensor.xpeng_g6_last_data_update':
            st['state'] = 'unknown'
    ha(states)
    s = _status()
    assert s.last_updated is None


def test_a_stamp_without_offset_is_a_guess_and_not_used(ha):
    states = _car()
    for st in states:
        if st['entity_id'] == 'sensor.xpeng_g6_last_data_update':
            st['state'] = '2026-11-10T14:03:12'
    ha(states)
    assert _status().last_updated is None


def test_signin_flag_and_polling_status_travel_with_the_reading(ha):
    ha(_car())
    s = _status()
    assert s.raw_data['credentials_valid'] is True
    assert s.raw_data['polling_status'] == 'Parked'


def test_lost_signin_is_visible_and_values_stay_the_frozen_last_ones(ha):
    states = _car()
    for st in states:
        if st['entity_id'] == 'binary_sensor.xpeng_g6_credentials_valid':
            st['state'] = 'off'
            st['attributes']['reason'] = '5 consecutive failures'
        if st['entity_id'] == 'sensor.xpeng_g6_polling_status':
            st['state'] = 'Reconnecting'
    ha(states)
    s = _status()
    assert s.raw_data['credentials_valid'] is False
    assert s.raw_data['polling_status'] == 'Reconnecting'
    assert s.soc_percent == 78 and s.last_updated == DATA     # the age of the data tells the truth


def test_integration_not_loaded_gives_unavailable_and_therefore_no_reading(ha):
    states = [
        _st('sensor.xpeng_g6_battery', 'unavailable', restored=True, unit_of_measurement='%',
            device_class='battery'),
        _st('sensor.xpeng_g6_odometer', 'unavailable', restored=True, unit_of_measurement='km',
            device_class='distance'),
        _st('binary_sensor.xpeng_g6_credentials_valid', 'unavailable', restored=True),
        _st('device_tracker.xpeng_g6_location', 'unavailable', restored=True),
        _st('sensor.xpeng_g6_last_data_update', 'unavailable', restored=True,
            device_class='timestamp'),
    ]
    ha(states)
    s = _status()
    assert s.soc_percent is None and s.odometer_km is None
    assert s.raw_data['credentials_valid'] is None
    assert s.last_updated is None
    assert s.location_lat is None


def test_fresh_install_without_data_is_unknown_not_a_value(ha):
    states = [_st('sensor.xpeng_g6_battery', 'unknown', unit_of_measurement='%', device_class='battery'),
              _st('sensor.xpeng_g6_odometer', 'unknown', unit_of_measurement='km', device_class='distance'),
              _st('sensor.xpeng_g6_last_data_update', 'unknown', device_class='timestamp'),
              _st('binary_sensor.xpeng_g6_plugged_in', 'unknown', device_class='plug')]
    ha(states)
    s = _status()
    assert s.soc_percent is None and s.is_plugged_in is False and s.last_updated is None


def test_position_and_the_time_of_the_fix(ha):
    ha(_car())
    s = _status()
    assert (s.location_lat, s.location_lon) == (51.0, 14.0)
    # HA wrote the state at 14:05:00, the fix was 2710 s old then -> 13:19:50
    assert s.location_last_updated_at == '2026-11-10T13:19:50+00:00'


def test_no_fix_age_no_fix_time(ha):
    states = _car()
    for st in states:
        if st['entity_id'].startswith('device_tracker.'):
            st['attributes']['fix_age_s'] = None
    ha(states)
    s = _status()
    assert (s.location_lat, s.location_lon) == (51.0, 14.0)
    assert getattr(s, 'location_last_updated_at', None) is None


def test_driving_regeneration_is_not_a_charge(ha):
    states = _car('Disconnected', plugged='off')
    for st in states:
        if st['entity_id'] == 'sensor.xpeng_g6_speed':
            st['state'] = '87.3'
    ha(states)
    s = _status()
    assert s.is_charging is False and s.raw_data['speed_kmh'] == 87.3


def test_legacy_layout_charging_binary_sensor_still_works(ha):
    ha(_car('Disconnected', plugged='off') +
       [_st('binary_sensor.xpeng_g6_charging', 'on', device_class='battery_charging')])
    s = _status()
    assert s.is_charging is True


def test_the_test_button_names_the_charging_state_sensor(ha):
    ha(_car('Charging', plugged='on', power='11.2'))
    c = H.HomeAssistantConnector({'username': 'http://10.0.0.5:8123', 'password': 't',
                                  'vin': 'xpeng_g6'})
    text = c.verify_credentials()
    assert 'sensor.xpeng_g6_battery' in text and 'sensor.xpeng_g6_odometer' in text
    assert 'sensor.xpeng_g6_charging_state' in text
