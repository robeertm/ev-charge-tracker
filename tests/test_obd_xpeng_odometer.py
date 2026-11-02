# -*- coding: utf-8 -*-
"""XPENG G6 browser-OBD profile: the odometer (220101) and the pack current.

Sources count the odometer as [B4:B6] (3 bytes) or [B5:B6] (2 bytes).
WiCAN's B0 is the PCI byte, so on a single frame B4 is our byte 3:

    784 06 62 01 01 00 04 7A
        B0 B1 B2 B3 B4 B5 B6      → [B4:B6] = 00 04 7A = 1146
                                    [B5:B6] =    04 7A = 1146

Wrong readings reported for the 3-byte form: 100 000 for 10 000, 0, and
25 089 for 1 146. 25 089 = 0x6201 — the service echo "62 01" read as a
number, i.e. a shifted or cut-short answer. All frames below are made up.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import obd_decode as od  # noqa: E402


def odo(raw, context=None):
    return od.decode('xpeng', {'220101': raw}, context)['odometer_km']


def sf(*data_bytes, echo=(0x62, 0x01, 0x01)):
    """A single-frame reply from the BMS: PCI + echo + data."""
    body = list(echo) + list(data_bytes)
    return '784 %02X %s\r>' % (len(body), ' '.join('%02X' % b for b in body))


def test_the_example_frame_is_what_we_think_it_is():
    # derive it: 1146 km = 0x00047A
    assert 1146 == 0x00047A == (0x00 << 16) | (0x04 << 8) | 0x7A == 0x047A
    raw = '784 06 62 01 01 00 04 7A\r>'
    assert od.reassemble(raw, '220101') == [0x62, 0x01, 0x01, 0x00, 0x04, 0x7A]
    assert odo(raw) == 1146


def test_both_formulas_agree_when_the_top_byte_is_zero():
    for km in (1, 1146, 12345, 65535):
        b = [0x00, km >> 8 & 0xFF, km & 0xFF]
        assert odo(sf(*b)) == km


def test_a_top_byte_makes_a_mileage_above_65535(    ):
    raw = sf(0x01, 0x11, 0x70)                                # 0x011170 = 70 000
    assert odo(raw) == 70_000                                 # the 2-byte formula would say 4 464
    assert odo(raw, {'odometer_km': 69_900}) == 70_000


def test_a_garbage_top_byte_on_a_young_car_falls_back_to_the_two_byte_value():
    raw = sf(0x01, 0x04, 0x7A)                                # 3 bytes: 66 682, 2 bytes: 1 146
    assert odo(raw, {'odometer_km': 1100}) == 1146            # the car was at 1 100 km a week ago
    assert odo(sf(0x80, 0x04, 0x7A)) == 1146                  # no history: a top byte beyond any car


def test_with_history_neither_reading_fitting_gives_no_value():
    assert odo(sf(0x01, 0x04, 0x7A), {'odometer_km': 30_000}) is None


def test_a_shifted_or_cut_short_answer_gives_no_value():
    # the echo displaced by one byte: the number would be 0x6201 = 25 089
    assert odo('784 07 00 62 01 01 00 04 7A\r>') is None
    # cut short right after the echo and two data bytes read as "62 01"
    assert odo('784 05 62 01 01 62 01\r>') is None
    # echo present, data too short for either reading
    assert odo('784 04 62 01 01 00\r>') is None
    assert odo('784 03 62 01 01\r>') is None


def test_a_wrong_echo_gives_no_value():
    assert odo(sf(0x00, 0x04, 0x7A, echo=(0x62, 0x01, 0x02))) is None     # other DID
    assert odo(sf(0x00, 0x04, 0x7A, echo=(0x61, 0x01, 0x01))) is None     # other service
    assert odo('784 03 7F 22 31\r>') is None                              # negative response
    assert odo('NO DATA\r>') is None


def test_the_echo_guard_also_holds_when_reassembly_does_not_check(  ):
    """``_field`` itself refuses a reply whose first bytes are not the echo."""
    spec = od.PROFILES['xpeng']['fields']['odometer_km']
    assert od._field([0x62, 0x01, 0x01, 0x00, 0x04, 0x7A], spec) == 1146
    assert od._field([0x00, 0x62, 0x01, 0x01, 0x00, 0x04], spec) is None
    assert od._field([0x62, 0x01, 0x02, 0x00, 0x04, 0x7A], spec) is None


def test_the_known_false_values_are_no_reading():
    assert odo(sf(0x00, 0x62, 0x01)) is None                  # 25 089 = 0x6201
    assert odo(sf(0x00, 0x00, 0x00)) is None                  # 0
    assert od._field([0x62, 0x01, 0x01, 0x00, 0x62, 0x01],
                     od.PROFILES['xpeng']['fields']['odometer_km']) is None


def test_a_multi_frame_reply_decodes_like_a_single_frame():
    payload = [0x62, 0x01, 0x01, 0x00, 0x04, 0x7A, 0x11, 0x22, 0x33, 0x44]
    n = len(payload)
    raw = ('784 %02X %02X %s\r784 21 %s\r>' % (
        0x10 | (n >> 8), n & 0xFF, ' '.join('%02X' % b for b in payload[:6]),
        ' '.join('%02X' % b for b in payload[6:])))
    assert od.reassemble(raw, '220101') == payload
    assert odo(raw) == 1146


def test_only_the_profiles_that_use_it_ask_for_the_last_mileage():
    assert od.wants_context('xpeng') is True
    assert od.wants_context('kia_hyundai_ext') is False
    assert od.wants_context('nonsense') is False


# ── pack current / pack voltage ───────────────────────────────────────

def test_the_pack_current_stays_raw_and_the_profile_says_the_sign_is_disputed():
    # 221103: (B4*256+B5)*0.5-1600; 0x0B40 = 2880 → -160 A, 0x0C80 = 3200 → 0 A
    neg = od.decode('xpeng', {'221103': sf(0x0B, 0x40, echo=(0x62, 0x11, 0x03))})
    assert neg['pack_current_a'] == -160.0
    pos = od.decode('xpeng', {'221103': sf(0x0D, 0xC0, echo=(0x62, 0x11, 0x03))})
    assert pos['pack_current_a'] == 160.0
    src = open(od.__file__, encoding='utf-8').read()
    assert 'positive = charging\n            # on the G6' not in src
    assert 'SIGN is disputed' in src and 'negative = charging per stevelea' in src


def test_a_missing_pack_voltage_is_empty_and_nothing_fails():
    """The 2026 LFP G6 reportedly does not answer 221101 at all."""
    frames = {'221103': sf(0x0B, 0x40, echo=(0x62, 0x11, 0x03)),
              '221109': sf(0x03, 0x0C, echo=(0x62, 0x11, 0x09)),
              '220101': sf(0x00, 0x04, 0x7A),
              '221101': 'NO DATA\r>'}
    out = od.decode('xpeng', frames)
    assert out['pack_voltage_v'] is None and 'power_kw' not in out
    assert out['pack_current_a'] == -160.0 and out['soc_bms_pct'] == 78.0
    assert out['odometer_km'] == 1146
    # not even present in the request set
    del frames['221101']
    assert od.decode('xpeng', frames)['pack_voltage_v'] is None


def test_with_both_the_derived_power_is_there():
    frames = {'221101': sf(0x0E, 0x10, echo=(0x62, 0x11, 0x01)),
              '221103': sf(0x0B, 0x40, echo=(0x62, 0x11, 0x03))}
    assert od.decode('xpeng', frames)['power_kw'] == -57.6     # 360 V x -160 A
