"""Unit tests for services/vehicle/obd_decode.py.

These verify the *engine* — ISO-TP reassembly, signed/unsigned extraction,
scaling and the cell/temperature summaries — by round-tripping synthetic
frames built to the exact byte layout the kia_hyundai_ext profile expects.
They intentionally do NOT assert real-world values against a specific car
(the offsets are the community map and are verifiable against CarScanner);
what they guarantee is that whatever the offset table says, the decoder
reads it back correctly.

Run:  python3 tests/test_obd_decode.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import obd_decode as od  # noqa: E402


def _frame_lines(resp, can_id='7EC'):
    """Render a response byte list as ELM327 raw ISO-TP frame text
    (headers on, spaces on) — the format obd.js ships to the server."""
    n = len(resp)
    lines = []
    if n <= 7:
        by = [0x00 | n] + list(resp)
        lines.append(can_id + ' ' + ' '.join(f'{b:02X}' for b in by))
    else:
        ff = [0x10 | (n >> 8), n & 0xFF] + list(resp[:6])
        lines.append(can_id + ' ' + ' '.join(f'{b:02X}' for b in ff))
        rest = list(resp[6:])
        seq = 1
        while rest:
            chunk = rest[:7]
            rest = rest[7:]
            cf = [0x20 | (seq & 0x0F)] + chunk
            lines.append(can_id + ' ' + ' '.join(f'{b:02X}' for b in cf))
            seq += 1
    return '\r'.join(lines) + '\r>'


def _mk_220101():
    # Offsets match the +1-shifted kia_hyundai_ext profile (verified against a
    # real Niro EV; see obd_decode.py). The reply carries a status byte after
    # `62 01 01`, so every field sits one byte later than the naive count.
    r = [0] * 62
    r[0], r[1], r[2] = 0x62, 0x01, 0x01
    r[7] = 160                      # SoC BMS 160/2 = 80.0 %
    r[13], r[14] = 0xFF, 0x6A       # current -150 * 0.1 = -15.0 A
    r[15], r[16] = 0x0E, 0x10       # voltage 3600 * 0.1 = 360.0 V
    r[17] = 30                      # temp max 30
    r[18] = 20                      # temp min 20 (also module[0])
    r[19], r[20], r[21], r[22] = 25, 26, 24, 25  # module temps
    r[26] = 205                     # cell max 205 * 0.02 = 4.10 V
    r[28] = 200                     # cell min 200 * 0.02 = 4.00 V
    r[32] = 138                     # aux 12V 138 * 0.1 = 13.8 V
    r[41:45] = [0, 0, 0x27, 0x10]   # cum charge 10000 * 0.1 = 1000.0 Ah
    r[45:49] = [0, 0, 0x13, 0x88]   # cum discharge 5000 * 0.1 = 500.0 Ah
    return r


def _mk_2105():
    r = [0] * 48
    r[0], r[1], r[2] = 0x62, 0x01, 0x05
    r[28], r[29] = 0x03, 0xD9       # SoH 985 * 0.1 = 98.5 %
    r[34] = 158                     # display SoC 158 * 0.5 = 79.0 %
    return r


def _mk_cells(pid_last, base=200):
    r = [0] * 40
    r[0], r[1], r[2] = 0x62, 0x01, pid_last
    for i in range(32):
        r[7 + i] = base             # cells start at byte 7 (4-byte prefix)
    return r


def main():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(('PASS' if cond else 'FAIL'), name)
        if not cond:
            fails += 1

    # ── ISO-TP reassembly ──────────────────────────────────────────────
    resp101 = _mk_220101()
    payload = od.reassemble(_frame_lines(resp101))
    check('reassembles multi-frame length', payload == resp101)

    sf = od.reassemble('7EC 03 62 01 05 >')
    check('single frame reassembles', sf == [0x62, 0x01, 0x05])

    check('NO DATA -> None', od.reassemble('NO DATA\r>') is None)
    check('empty -> None', od.reassemble('') is None)

    # No-header adapter output still parses.
    nh = od.reassemble('03 41 5B 7F \r>')
    check('headerless single frame', nh == [0x41, 0x5B, 0x7F])

    # ── signed / unsigned extraction ───────────────────────────────────
    check('unsigned 2-byte', od._u([0x0E, 0x10], 0, 2) == 3600)
    check('signed negative', od._s([0xFF, 0x6A], 0, 2) == -150)
    check('bounds guard', od._u([0x01], 0, 2) is None)

    # ── full decode of the Kia/Hyundai profile ─────────────────────────
    frames = {
        '220101': _frame_lines(resp101),
        '220102': _frame_lines(_mk_cells(0x02, 200)),
        '220103': _frame_lines(_mk_cells(0x03, 201)),
        '220104': _frame_lines(_mk_cells(0x04, 199)),
        '220105': _frame_lines(_mk_2105()),
    }
    d = od.decode('kia_hyundai_ext', frames)

    check('soc_bms 80.0', abs(d['soc_bms_pct'] - 80.0) < 0.01)
    check('pack current -15.0', abs(d['pack_current_a'] + 15.0) < 0.01)
    check('pack voltage 360.0', abs(d['pack_voltage_v'] - 360.0) < 0.01)
    check('temp max 30', d['temp_max_c'] == 30)
    check('temp min 20', d['temp_min_c'] == 20)
    check('aux 13.8', abs(d['aux_battery_v'] - 13.8) < 0.01)
    check('soh 98.5', abs(d['soh_pct'] - 98.5) < 0.01)
    check('soc display 79.0', abs(d['soc_display_pct'] - 79.0) < 0.01)
    check('cum charge 1000.0', abs(d['cumulative_charge_ah'] - 1000.0) < 0.01)
    # Derived live power = pack V × pack A / 1000 = 360.0 × -15.0 / 1000 = -5.4 kW
    check('power_kw -5.4', abs(d['power_kw'] + 5.4) < 0.01)

    # 96 cells across the three PIDs (min 199*0.02=3.98, max 201*0.02=4.02)
    check('96 cells', d['cell_count'] == 96)
    check('cell min 3.98', abs(d['cell_min_v'] - 3.98) < 0.001)
    check('cell max 4.02', abs(d['cell_max_v'] - 4.02) < 0.001)
    check('cell delta 40 mV', abs(d['cell_delta_mv'] - 40.0) < 0.1)

    # module temps summarised
    check('temp avg from modules', d['temp_avg_c'] is not None)
    check('cell_voltages array present', len(d.get('cell_voltages', [])) == 96)

    # ── generic fallback (PID 015B) ────────────────────────────────────
    g = od.decode('generic_ev', {'015B': '7E8 03 41 5B 7F \r>'})
    # 0x7F = 127 -> 127*100/255 = 49.8 %
    check('generic SoH ~49.8', abs(g['soh_pct'] - 49.8) < 0.2)

    # ── partial data degrades gracefully ───────────────────────────────
    p = od.decode('kia_hyundai_ext', {'220105': _frame_lines(_mk_2105())})
    check('partial: soh present', abs(p['soh_pct'] - 98.5) < 0.01)
    check('partial: soc_bms None', p.get('soc_bms_pct') is None)
    check('partial: no cells', p.get('cell_count') is None)

    check('unknown profile -> error', 'error' in od.decode('nope', {'x': '1'}))
    check('brand mapping kia', od.profile_for_brand('Kia') == 'kia_hyundai_ext')
    check('brand mapping xpeng', od.profile_for_brand('XPENG') == 'xpeng')
    check('brand mapping unknown', od.profile_for_brand('Tesla') == 'generic_ev')

    # ── field offset support (raw * scale + offset) ────────────────────
    # 0x0DAC = 3500 -> 3500*0.5 - 1600 = 150.0
    off = od._field([0x62, 0x11, 0x03, 0, 0, 0, 0, 0x0D, 0xAC],
                    {'byte': 7, 'len': 2, 'scale': 0.5, 'offset': -1600.0})
    check('field offset applied', abs(off - 150.0) < 0.01)

    # ── XPENG (G6) profile round-trips its documented byte layout ──────
    def _xp(did, data):
        # 62 11 <xx> echo (3 bytes) + data, rendered as one ELM SF/MF line.
        return _frame_lines([0x62, 0x11, int(did[-2:], 16)] + data, can_id='784')
    xframes = {
        # pack V: B4:B5 = 0x0E10 (3600) * 0.1 = 360.0
        '221101': _xp('221101', [0, 0, 0, 0, 0x0E, 0x10]),
        # current: B4:B5 = 0x0C80 (3200) * 0.5 - 1600 = 0.0
        '221103': _xp('221103', [0, 0, 0, 0, 0x0C, 0x80]),
        # cell max/min: B0:B1 /1000 -> 4.100 / 3.950
        '221105': _xp('221105', [0x10, 0x04]),   # 0x1004 = 4100
        '221106': _xp('221106', [0x0F, 0x6E]),   # 0x0F6E = 3950
        # temp max/min: B0 - 40 -> 32 / 12
        '221107': _xp('221107', [72]),
        '221108': _xp('221108', [52]),
        # SoC: B0:B1 /10 -> 78.0
        '221109': _xp('221109', [0x03, 0x0C]),   # 0x030C = 780
        # SoH: B4:B5 /10 -> 96.0
        '22110A': _xp('22110A', [0, 0, 0, 0, 0x03, 0xC0]),  # 0x03C0 = 960
    }
    x = od.decode('xpeng', xframes)
    check('xpeng pack V 360.0', abs(x['pack_voltage_v'] - 360.0) < 0.01)
    check('xpeng current 0.0', abs(x['pack_current_a'] - 0.0) < 0.01)
    check('xpeng cell max 4.10', abs(x['cell_max_v'] - 4.10) < 0.001)
    check('xpeng cell min 3.95', abs(x['cell_min_v'] - 3.95) < 0.001)
    check('xpeng cell delta 150 mV', abs(x['cell_delta_mv'] - 150.0) < 0.1)
    check('xpeng temp max 32', x['temp_max_c'] == 32)
    check('xpeng temp min 12', x['temp_min_c'] == 12)
    check('xpeng soc 78.0', abs(x['soc_bms_pct'] - 78.0) < 0.01)
    check('xpeng soh 96.0', abs(x['soh_pct'] - 96.0) < 0.01)

    # ── ECU scan classification + interpretation ───────────────────────
    check('probe ok (62)', od._classify_probe('784 06 62 11 01 0E 10 00 >') == 'ok')
    check('probe rejected (7F)', od._classify_probe('7EC 03 7F 22 31 >') == 'rejected')
    check('probe nodata', od._classify_probe('NO DATA\r>') == 'nodata')
    check('scan headers', od._scan_headers('7E8 06 41 00 ...\r7EC 06 41 00 ...\r>')
          == ['7E8', '7EC'])
    plan = od.scan_plan()
    check('scan plan has probes', len(plan['probes']) == len(od.SCAN_CANDIDATES))
    rep = od.interpret_scan({
        'protocol': '6', 'enumerate': '7E8 06 41 00 00 00 00 00 >',
        'probes': {'xpeng': '784 06 62 11 01 0E 10 00 >'},
    })
    check('scan recommends xpeng', rep['recommended_profile'] == 'xpeng')
    check('scan ok flag', rep['ok'] is True)
    rep2 = od.interpret_scan({'protocol': '', 'enumerate': 'NO DATA', 'probes': {}})
    check('scan no ecus -> not ok', rep2['ok'] is False)

    # ── Real Kia Niro EV capture (2026-08-08, awake/ready car) ─────────
    # The exact frames Robert's dongle returned. Two things this guards:
    #  1) the stray `7EC 03 59 02 …` DTC single-frame that arrives ahead of
    #     the real 220101 answer must be skipped, not decoded;
    #  2) every field must decode to a physically sane value (the +1 fix).
    real = {
        '220101': (
            '7EC 03 59 02 FF AA AA AA AA\r'
            '7EC 10 3E 62 01 01 FF F7 E7\r'
            '7EC 21 FF 84 00 00 00 00 83\r'
            '7EC 22 00 0A 0E B0 1D 1C 1C\r'
            '7EC 23 1C 1C 1C 00 00 1C C0\r'
            '7EC 24 28 BF 0E 00 00 94 00\r'
            '7EC 25 08 98 8B 00 08 7E 72\r'
            '7EC 26 00 03 34 10 00 03 0E\r'
            '7EC 27 60 00 C9 6B 58 0D 01\r'
            '7EC 28 77 00 00 00 00 03 E8\r>'),
        '220102': (
            '7EC 10 27 62 01 02 FF FF FF\r'
            '7EC 21 FF BF BF BF BF BF BF\r'
            '7EC 22 BF BF BF BF C0 BF BF\r'
            '7EC 23 BF BF BF BF BF BF BF\r'
            '7EC 24 C0 BF BF BF BF BF BF\r'
            '7EC 25 BF BF C0 BF BF AA AA\r>'),
        '220103': (
            '7EC 10 27 62 01 03 FF FF FF\r'
            '7EC 21 FF BF BF BF BF BF BF\r'
            '7EC 22 BF C0 BF BF BF BF BF\r'
            '7EC 23 BF BF BF BF BF BF BF\r'
            '7EC 24 BF BF BF BF BF BF C0\r'
            '7EC 25 BF BF BF BF BF AA AA\r>'),
        '220104': (
            '7EC 10 27 62 01 04 FF FF FF\r'
            '7EC 21 FF BF BF BF BF BF BF\r'
            '7EC 22 BF BF BF BF BF BF BF\r'
            '7EC 23 BF BF BF BF BF BF BF\r'
            '7EC 24 BF BF BF BF BF BF BF\r'
            '7EC 25 BF BF BF BF BF AA AA\r>'),
        '220105': (
            '7EC 10 2E 62 01 05 00 3F FF\r'
            '7EC 21 90 00 00 00 00 00 00\r'
            '7EC 22 00 00 1C 94 77 01 42\r'
            '7EC 23 68 42 68 00 01 50 1D\r'
            '7EC 24 88 03 B3 00 00 58 00\r'
            '7EC 25 88 00 00 BF BF 01 00\r'
            '7EC 26 19 00 00 00 00 AA AA\r>'),
    }
    # Stray DTC frame skipped → the real 62 01 01 answer is reassembled.
    p101 = od.reassemble(real['220101'], '220101')
    check('real: stray 59 02 skipped', p101[:3] == [0x62, 0x01, 0x01])
    rd = od.decode('kia_hyundai_ext', real)
    check('real soc 66.0', abs(rd['soc_bms_pct'] - 66.0) < 0.01)
    check('real pack V 376.0', abs(rd['pack_voltage_v'] - 376.0) < 0.01)
    check('real pack A 1.0', abs(rd['pack_current_a'] - 1.0) < 0.01)
    check('real temp 28/29', rd['temp_min_c'] == 28 and rd['temp_max_c'] == 29)
    check('real aux 14.8', abs(rd['aux_battery_v'] - 14.8) < 0.01)
    check('real soh 94.7', abs(rd['soh_pct'] - 94.7) < 0.01)
    check('real display soc 68.0', abs(rd['soc_display_pct'] - 68.0) < 0.01)
    check('real 96 cells', rd['cell_count'] == 96)
    check('real cell min 3.82', abs(rd['cell_min_v'] - 3.82) < 0.001)
    check('real cell max 3.84', abs(rd['cell_max_v'] - 3.84) < 0.001)
    check('real cell delta 20 mV', abs(rd['cell_delta_mv'] - 20.0) < 0.5)
    check('real no bogus 5.1V cell', max(rd['cell_voltages']) < 4.3)
    check('real cum charge 20993.6', abs(rd['cumulative_charge_ah'] - 20993.6) < 0.1)
    check('real cum discharge 20028.8', abs(rd['cumulative_discharge_ah'] - 20028.8) < 0.1)

    print('\n%d check(s) failed' % fails if fails else '\nAll checks passed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
