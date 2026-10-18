# -*- coding: utf-8 -*-
"""29-bit CAN replies as a real ELM327 prints them (v3.0.175).

With ATS1 + ATH1 an ELM327 prints a 29-bit id as four separate bytes:
``18 DA F1 DB 10 14 62 90 01 …``. The decoder only knew the glued
``18DAF1DB`` form the test data used, read 0x18 as a First Frame and decoded
nothing for VW MEB and Renault Zoe ZE50. And the client set the header with a
4-byte ``ATSH18DADBF1``, which a common v1.5 ELM327 answers with "?".
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import obd_decode as od  # noqa: E402


def _frames(resp, can_id):
    """ISO-TP frame text for ``resp``; ``can_id`` printed as given (for a
    29-bit id with spaces on: ``'18 DA F1 DB'``)."""
    n = len(resp)
    rows = []
    if n <= 7:
        rows.append([n] + list(resp))
    else:
        rows.append([0x10 | (n >> 8), n & 0xFF] + list(resp[:6]))
        rest, seq = list(resp[6:]), 1
        while rest:
            rows.append([0x20 | (seq & 0x0F)] + rest[:7])
            rest, seq = rest[7:], seq + 1
    return '\r'.join(can_id + ' ' + ' '.join(f'{b:02X}' for b in r) for r in rows) + '\r>'


def _did(pid, data, can_id):
    return _frames([0x62, int(pid[2:4], 16), int(pid[4:6], 16)] + data, can_id)


LONG = [0x62, 0x90, 0x01] + list(range(0x30, 0x30 + 17))     # 20 bytes → FF + 2 CF


def test_spaced_29bit_multi_frame_reassembles():
    raw = _frames(LONG, '18 DA F1 DB')
    assert raw.startswith('18 DA F1 DB 10 14 62 90 01')
    assert od.reassemble(raw, '229001') == LONG
    assert od.reassemble(raw) == LONG                  # without expect too


def test_spaced_29bit_single_frame_reassembles():
    raw = _did('22028C', [0xC8], '17 FE 00 7B')
    assert od.reassemble(raw, '22028C') == [0x62, 0x02, 0x8C, 0xC8]
    assert od.reassemble(raw) == [0x62, 0x02, 0x8C, 0xC8]


def test_glued_29bit_still_works():
    assert od.reassemble(_frames(LONG, '18DAF1DB'), '229001') == LONG


def test_other_ecu_between_frames_is_skipped():
    lines = _frames(LONG, '18 DA F1 DB').split('\r')
    lines.insert(1, '18 DA F1 01 03 7F 22 31')      # another ECU, own id
    assert od.reassemble('\r'.join(lines), '229001') == LONG


def test_missing_frame_in_29bit_reply_is_refused():
    lines = _frames(LONG + list(range(10)), '18 DA F1 DB').split('\r')
    del lines[1]                                     # CF 1 lost
    assert od.reassemble('\r'.join(lines), '229001') is None


def test_headerless_and_11bit_replies_unchanged():
    resp = [0x62, 0x01, 0x01] + list(range(1, 30))
    assert od.reassemble(_frames(resp, '7EC'), '220101') == resp
    headerless = _frames(resp, '').replace('\r ', '\r').lstrip()
    assert od.reassemble(headerless, '220101') == resp
    assert od.reassemble(headerless) == resp
    assert od.reassemble('06 62 01 01 00 07 08 \r>') == [0x62, 0x01, 0x01, 0x00, 0x07, 0x08]
    assert od.reassemble('03 41 5B 7F \r>') == [0x41, 0x5B, 0x7F]


def test_zoe_and_vw_decode_from_real_elm_output():
    zoe = od.decode('renault_zoe_ph2', {
        '229001': _did('229001', [0x1F, 0x9E], '18 DA F1 DB'),
        '229003': _did('229003', [0x25, 0x0F], '18 DA F1 DB'),
        '229005': _did('229005', [0x0E, 0xA6], '18 DA F1 DB'),
    })
    assert abs(zoe['soc_bms_pct'] - 77.94) < 0.02
    assert abs(zoe['soh_pct'] - 94.87) < 0.01
    assert abs(zoe['pack_voltage_v'] - 375.0) < 0.01
    vw = od.decode('vw_meb', {
        '22028C': _did('22028C', [0xC8], '17 FE 00 7B'),
        '221E3B': _did('221E3B', [0x05, 0xDC], '17 FE 00 7B'),
    })
    assert abs(vw['soc_bms_pct'] - 80.0) < 0.01
    assert abs(vw['pack_voltage_v'] - 375.0) < 0.01


def test_scan_lists_spaced_29bit_responders():
    raw = '18 DA F1 10 06 41 00 BE 3F A8 13\r18 DA F1 01 06 41 00 98 18 80 11\r>'
    assert od._scan_headers(raw) == ['18DAF110', '18DAF101']
    assert od._scan_headers('7E8 06 41 00 BE 3F A8 13\r>') == ['7E8']


def test_29bit_header_goes_out_as_atcp_plus_three_bytes():
    for key, prio, low in (('renault_zoe_ph2', '18', 'DADBF1'), ('vw_meb', '17', 'FC007B')):
        p = od.PROFILES[key]
        assert od.wire_header(p) == low
        assert 'ATCP' + prio in p['init']
        assert p['init'].index('ATCP' + prio) > p['init'].index('ATSP7')
    for key in ('kia_hyundai_ext', 'xpeng'):
        p = od.PROFILES[key]
        assert od.wire_header(p) == p['header']
        assert not any(l.startswith('ATCP') for l in p['init'])
