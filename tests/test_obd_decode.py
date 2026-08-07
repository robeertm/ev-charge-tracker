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
    r = [0] * 61
    r[0], r[1], r[2] = 0x62, 0x01, 0x01
    r[6] = 160                      # SoC BMS 160/2 = 80.0 %
    r[12], r[13] = 0xFF, 0x6A       # current -150 * 0.1 = -15.0 A
    r[14], r[15] = 0x0E, 0x10       # voltage 3600 * 0.1 = 360.0 V
    r[16] = 30                      # temp max 30
    r[17] = 20                      # temp min 20
    r[18], r[19], r[20], r[21], r[22] = 25, 25, 26, 24, 25  # module temps
    r[25] = 205                     # cell max 205 * 0.02 = 4.10 V
    r[27] = 200                     # cell min 200 * 0.02 = 4.00 V
    r[31] = 138                     # aux 12V 138 * 0.1 = 13.8 V
    r[40:44] = [0, 0, 0x27, 0x10]   # cum charge 10000 * 0.1 = 1000.0 Ah
    r[44:48] = [0, 0, 0x13, 0x88]   # cum discharge 5000 * 0.1 = 500.0 Ah
    return r


def _mk_2105():
    r = [0] * 48
    r[0], r[1], r[2] = 0x62, 0x01, 0x05
    r[27], r[28] = 0x03, 0xD9       # SoH 985 * 0.1 = 98.5 %
    r[33] = 158                     # display SoC 158 * 0.5 = 79.0 %
    return r


def _mk_cells(pid_last, base=200):
    r = [0] * 40
    r[0], r[1], r[2] = 0x62, 0x01, pid_last
    for i in range(32):
        r[6 + i] = base
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
    check('brand mapping unknown', od.profile_for_brand('Tesla') == 'generic_ev')

    print('\n%d check(s) failed' % fails if fails else '\nAll checks passed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
