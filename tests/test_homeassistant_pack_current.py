# -*- coding: utf-8 -*-
"""Charging and charge power from the pack current (WiCAN → MQTT → HA).

The XPENG G6 has no charging entity; a WiCAN Pro publishes SOC, ODOMETER,
HV_A (pack current), HV_V (pack voltage) and maybe SPEED. Nobody has real
data yet — everything here is made-up example data in the entity shapes
the sources show. What is pinned:

* the roles are found by device class / unit AND name words, without
  tipping the roles other integrations rely on;
* "charging" is not "current flows into the pack": regeneration has the
  same sign, so driving never counts, and without a speed entity the
  plug state or two quiet polls must confirm;
* the sense of the current (default: negative = charging) is learned from
  the car if it turns out the other way round, and remembered;
* a real charging / plug / power entity wins;
* power is |V x A| only with a plausible voltage — never invented;
* an odometer that cannot be true (0 after a value, below the last one,
  faster than any car, the false value 25 089 = 0x6201) is no reading.
"""
import json
import logging
import os
import stat
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import connector_homeassistant as H  # noqa: E402
from test_homeassistant_connector import _HA, _st, _conn  # noqa: E402

P = 'sensor.xpeng_g6_wican_ab12_local_'          # the one public naming example
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(H, '_now', c)
    return c


@pytest.fixture
def ha(monkeypatch):
    def install(states):
        monkeypatch.setattr(H.urllib.request, 'urlopen', _HA(states))
    return install


def car(amps=None, volts=400.0, speed=None, soc=60, odo=5000, stamp='t1', extra=(), amps_stamp=None):
    """The entities a WiCAN profile produces. ``None`` leaves one out."""
    out = [_st(P + 'soc', str(soc), '%', 'battery'),
           _st(P + 'odometer', str(odo), 'km', 'distance')]
    out[1]['last_updated'] = stamp
    if amps is not None:
        out.append(_st(P + 'hv_a', str(amps), 'A', 'current'))
    if volts is not None:
        out.append(_st(P + 'hv_v', str(volts), 'V', 'voltage'))
    if speed is not None:
        out.append(_st(P + 'speed', str(speed), 'km/h', 'speed'))
    return out + list(extra)


def poll(ha, states):
    ha(states)
    return _conn().get_status()


# ── roles ─────────────────────────────────────────────────────────────

def test_wican_entities_get_the_new_roles_by_class_unit_and_words(ha):
    ha(car(-16, 400, 0))
    roles = _conn().map_entities(_conn()._states())
    assert roles['pack_current']['entity_id'] == P + 'hv_a'
    assert roles['pack_voltage']['entity_id'] == P + 'hv_v'
    assert roles['speed']['entity_id'] == P + 'speed'
    assert roles['soc']['entity_id'] == P + 'soc' and roles['odometer']['entity_id'] == P + 'odometer'


def test_plain_mqtt_sensors_with_only_a_unit_are_found_too(ha):
    ha([_st('sensor.xpeng_g6_hv_a', '-3', 'A'), _st('sensor.xpeng_g6_hv_v', '410', 'V'),
        _st('sensor.xpeng_g6_pack_current', '5', 'A'), _st('sensor.xpeng_g6_soc', '50', '%')])
    roles = _conn().map_entities(_conn()._states())
    assert roles['pack_current']['entity_id'] == 'sensor.xpeng_g6_hv_a'
    assert roles['pack_voltage']['entity_id'] == 'sensor.xpeng_g6_hv_v'


def test_look_alike_names_and_the_12v_battery_are_not_the_pack(ha):
    ha([_st('sensor.xpeng_g6_hv_ac_current', '2', 'A', 'current'),      # hv_ac ≠ hv_a
        _st('sensor.xpeng_g6_hv_ambient', '3', 'A'),
        _st('sensor.xpeng_g6_12v_battery_voltage', '12.6', 'V', 'voltage'),
        _st('sensor.xpeng_g6_battery_voltage', '12.4', 'V', 'voltage'),  # a 12 V value, not the pack
        _st('sensor.xpeng_g6_max_speed', '200', 'km/h', 'speed'),
        _st('sensor.xpeng_g6_soc', '50', '%')])
    roles = _conn().map_entities(_conn()._states())
    assert not {'pack_current', 'pack_voltage', 'speed'} & set(roles)


def test_roles_of_other_integrations_are_unchanged(ha):
    from test_homeassistant_connector import TIBBER, WICAN
    ha(TIBBER + WICAN)
    roles = _conn().map_entities(_conn()._states())
    assert roles['soc']['entity_id'] == 'sensor.xpeng_g6_battery_level'
    assert roles['charging']['entity_id'] == 'sensor.xpeng_g6_charging_status'
    assert roles['aux_v']['entity_id'] == 'sensor.xpeng_g6_12v'
    assert roles['pack_voltage']['entity_id'] == 'sensor.xpeng_g6_hv_v'


def test_describe_shows_the_new_roles(ha):
    ha(car(-16, 400, 0))
    found = _conn().verify_credentials()
    for eid in ('hv_a', 'hv_v', 'speed'):
        assert P + eid in found


# ── charging with a speed entity ──────────────────────────────────────

def test_ac_charge_at_standstill(ha, clock):
    s = poll(ha, car(amps=-16, volts=400, speed=0))
    assert s.is_charging and s.is_plugged_in
    assert s.charge_power_kw == 6.4
    assert s.raw_data['charge_inference']['mode'] == 'current+standstill'
    assert s.raw_data['charge_inference']['charging_current_sign'] == 'negative'


def test_dc_charge(ha, clock):
    s = poll(ha, car(amps=-300, volts=600, speed=0))
    assert s.is_charging and s.charge_power_kw == 180.0


def test_regeneration_while_driving_is_not_charging(ha, clock):
    s = poll(ha, car(amps=-40, volts=500, speed=50))
    assert s.is_charging is False and s.charge_power_kw is None
    assert s.raw_data['charge_inference']['mode'] == 'driving'


def test_standstill_without_real_current_is_not_charging(ha, clock):
    for amps in (0, -0.5, 0.4):
        s = poll(ha, car(amps=amps, speed=0))
        assert s.is_charging is False and s.charge_power_kw is None
    assert poll(ha, car(amps=4, speed=0)).is_charging is False     # discharge sense (climate)


def test_the_threshold_is_one_ampere(ha, clock):
    assert H.CHARGE_MIN_A == 1.0
    assert poll(ha, car(amps=-1.0, speed=0)).is_charging is True


def test_absurd_values_are_no_reading(ha, clock):
    s = poll(ha, car(amps=-16, volts=40, speed=0))                  # 40 V is no traction pack
    assert s.is_charging and s.charge_power_kw is None
    s = poll(ha, car(amps=-5000, volts=400, speed=0))
    assert s.is_charging is False


# ── charging without a speed entity ───────────────────────────────────

def test_without_speed_two_quiet_polls_are_needed(ha, clock):
    first = poll(ha, car(amps=-16, odo=5000, stamp='a'))
    assert first.is_charging is False
    assert first.raw_data['charge_inference']['mode'] == 'waiting_for_second_poll'
    clock.advance(900)
    second = poll(ha, car(amps=-16, odo=5000, stamp='b'))
    assert second.is_charging is True and second.charge_power_kw == 6.4
    assert second.raw_data['charge_inference']['mode'] == 'current+two_polls'


def test_without_speed_a_moved_odometer_is_no_charge(ha, clock):
    poll(ha, car(amps=-40, odo=5000, stamp='a'))
    clock.advance(900)
    assert poll(ha, car(amps=-40, odo=5012, stamp='b')).is_charging is False   # it drove


def test_two_polls_in_the_same_moment_are_one_observation(ha, clock):
    poll(ha, car(amps=-16))
    clock.advance(3)
    assert poll(ha, car(amps=-16)).is_charging is False


def test_the_second_poll_must_still_show_the_current(ha, clock):
    poll(ha, car(amps=-16, stamp='a'))
    clock.advance(900)
    assert poll(ha, car(amps=0, stamp='b')).is_charging is False


def test_an_old_previous_poll_does_not_count(ha, clock):
    poll(ha, car(amps=-16, stamp='a'))
    clock.advance(H.MAX_PREV_AGE_S + 60)
    assert poll(ha, car(amps=-16, stamp='b')).is_charging is False


def test_without_speed_a_confirmed_plug_is_enough(ha, clock):
    plug = _st('binary_sensor.xpeng_g6_plug', 'on', None, 'plug')
    s = poll(ha, car(amps=-16, extra=[plug]))
    assert s.is_charging and s.raw_data['charge_inference']['mode'] == 'current+plug'


def test_the_memory_survives_a_new_connector_object(ha, clock):
    """A poll builds a new connector each time; the previous poll is on disk."""
    ha(car(amps=-16, stamp='a'))
    _conn().get_status()
    clock.advance(600)
    ha(car(amps=-16, stamp='b'))
    assert _conn().get_status().is_charging is True


def test_the_memory_file_is_private_and_holds_no_address_or_token(ha, clock):
    poll(ha, car(amps=-16))
    files = os.listdir(H.STATE_DIR)
    assert len(files) == 1 and files[0].endswith('.json')
    path = os.path.join(H.STATE_DIR, files[0])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    body = open(path).read()
    assert '10.0.0.5' not in body and 'good' not in body and 'xpeng_g6' not in body + files[0]


# ── a real entity wins ────────────────────────────────────────────────

def test_a_real_charging_entity_has_priority(ha, clock):
    off = _st('binary_sensor.xpeng_g6_charging', 'off', None, 'battery_charging')
    s = poll(ha, car(amps=-16, speed=0, extra=[off]))
    assert s.is_charging is False and s.charge_power_kw is None
    assert s.raw_data['charge_inference']['mode'] == 'entity'
    on = _st('binary_sensor.xpeng_g6_charging', 'on', None, 'battery_charging')
    s = poll(ha, car(amps=-16, speed=0, extra=[on]))
    assert s.is_charging and s.charge_power_kw == 6.4        # entity says charging; V x A fills the power


def test_a_plug_entity_that_says_unplugged_wins(ha, clock):
    out = _st('binary_sensor.xpeng_g6_plug', 'off', None, 'plug')
    s = poll(ha, car(amps=-16, speed=0, extra=[out]))
    assert s.is_charging is False and s.is_plugged_in is False


def test_a_real_power_entity_has_priority_over_v_times_a(ha, clock):
    pw = _st('sensor.xpeng_g6_charging_power', '7.2', 'kW', 'power')
    s = poll(ha, car(amps=-16, volts=400, speed=0, extra=[pw]))
    assert s.is_charging and s.charge_power_kw == 7.2


# ── power without the pack voltage ────────────────────────────────────

def test_without_hv_v_the_power_is_left_empty(ha, clock):
    s = poll(ha, car(amps=-16, volts=None, speed=0))
    assert s.is_charging is True and s.charge_power_kw is None
    assert s.raw_data['pack_voltage_v'] is None and s.raw_data['pack_current_a'] == -16


# ── the sense is learned from the car ─────────────────────────────────

def test_a_reversed_current_is_learned_and_remembered(ha, clock, caplog):
    caplog.set_level(logging.WARNING)
    # this car reports + while charging: soc climbs at standstill, current positive
    assert poll(ha, car(amps=16, speed=0, soc=50, stamp='a')).is_charging is False
    clock.advance(900)
    assert poll(ha, car(amps=16, speed=0, soc=52, stamp='b')).is_charging is False   # one rise: not yet
    clock.advance(900)
    s = poll(ha, car(amps=16, speed=0, soc=54, stamp='c'))
    assert s.is_charging is True                             # second rise: sense turned round
    assert s.raw_data['charge_inference']['charging_current_sign'] == 'positive'
    assert s.raw_data['charge_inference']['sign_learned'] is True
    assert any('remembered' in r.getMessage() for r in caplog.records)
    # a new connector object (a restart, even) still knows
    clock.advance(900)
    s = poll(ha, car(amps=16, speed=0, soc=56, stamp='d'))
    assert s.is_charging and s.raw_data['charge_inference']['sign_learned']
    # and what used to mean charging is now the other direction
    assert poll(ha, car(amps=-16, speed=0, soc=56, stamp='e')).is_charging is False


def test_driving_regeneration_never_teaches_the_wrong_sense(ha, clock):
    for i in range(5):                                       # regen: soc may rise, car moves
        poll(ha, car(amps=40, speed=60, soc=50 + 2 * i, odo=5000 + 20 * i, stamp=str(i)))
        clock.advance(900)
    assert poll(ha, car(amps=-16, speed=0, soc=60, stamp='x')).is_charging is True   # default kept


def test_a_flat_soc_does_not_teach_anything(ha, clock):
    for i in range(4):
        poll(ha, car(amps=8, speed=0, soc=50, stamp=str(i)))  # climate draw, soc unchanged
        clock.advance(900)
    s = poll(ha, car(amps=-16, speed=0, soc=50, stamp='x'))
    assert s.is_charging is True and not s.raw_data['charge_inference']['sign_learned']


def test_the_sense_is_also_learned_without_a_speed_entity(ha, clock):
    poll(ha, car(amps=16, soc=50, odo=5000, stamp='a'))
    for i, soc in enumerate((52, 54), 1):
        clock.advance(900)
        s = poll(ha, car(amps=16, soc=soc, odo=5000, stamp=str(i)))
    assert s.raw_data['charge_inference']['sign_learned'] is True and s.is_charging


# ── the odometer must be physically possible ──────────────────────────

def km(ha, clock, value, after=900, stamp=None):
    clock.advance(after)
    return poll(ha, car(odo=value, stamp=stamp or str(clock.t))).odometer_km


def test_a_plausible_odometer_passes_and_may_stand_still(ha, clock):
    assert poll(ha, car(odo=5000)).odometer_km == 5000
    assert km(ha, clock, 5000) == 5000
    assert km(ha, clock, 5042) == 5042                       # 42 km in 15 min: fine


def test_zero_after_a_value_is_no_reading(ha, clock, caplog):
    caplog.set_level(logging.WARNING)
    poll(ha, car(odo=5000))
    assert km(ha, clock, 0) is None
    assert any('ignored (zero' in r.getMessage() for r in caplog.records)
    assert km(ha, clock, 5001) == 5001                       # the real value comes back


def test_the_false_value_0x6201_is_no_reading_even_as_the_first(ha, clock):
    assert H.ODO_KNOWN_BAD == {25089}
    assert poll(ha, car(odo=25089)).odometer_km is None
    poll(ha, car(odo=1146))
    assert km(ha, clock, 25089, after=3600 * 24 * 30) is None   # a month later it is still a false value


def test_a_step_back_is_no_reading(ha, clock):
    poll(ha, car(odo=5000))
    assert km(ha, clock, 4000) is None
    assert km(ha, clock, 5000) == 5000                       # the real one is still judged against 5000


def test_a_giant_jump_is_no_reading_but_a_long_gap_allows_distance(ha, clock):
    poll(ha, car(odo=10_000))
    assert km(ha, clock, 100_000) is None                    # 90 000 km in 15 minutes
    assert km(ha, clock, 10_800, after=3600 * 5) == 10_800    # 800 km in 5 h = 160 km/h: possible


def test_a_rejected_reading_is_not_stored(ha, clock):
    poll(ha, car(odo=5000))
    km(ha, clock, 0)
    km(ha, clock, 100_000)
    assert km(ha, clock, 5003) == 5003                       # still judged against 5000, not the garbage


def test_a_wrongly_accepted_high_start_does_not_lock_everything_out(ha, clock):
    poll(ha, car(odo=100_000))                                # the first reading was the glitch
    assert km(ha, clock, 10_000, stamp='u1') is None
    assert km(ha, clock, 10_004, stamp='u2') is None
    assert km(ha, clock, 10_009, stamp='u3') == 10_009       # three separate, consistent updates
    assert km(ha, clock, 10_012) == 10_012


def test_a_value_stuck_in_home_assistant_does_not_reanchor(ha, clock):
    poll(ha, car(odo=100_000))
    for _ in range(6):
        assert km(ha, clock, 10_000, stamp='same') is None


def test_miles_are_judged_in_kilometres(ha, clock):
    ha([_st(P + 'odometer', '1000', 'mi'), _st(P + 'soc', '50', '%')])
    assert _conn().get_status().odometer_km == 1609
    clock.advance(600)
    ha([_st(P + 'odometer', '1001', 'mi'), _st(P + 'soc', '50', '%')])
    assert _conn().get_status().odometer_km == 1610


def test_a_read_only_data_folder_does_not_break_a_poll(ha, clock, monkeypatch, tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    monkeypatch.setattr(H, 'STATE_DIR', str(blocker / 'sub'))     # cannot be created
    assert poll(ha, car(amps=-16, speed=0)).is_charging is True
