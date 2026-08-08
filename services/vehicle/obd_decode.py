"""Decode raw ELM327 / OBD-II responses into battery-cell metrics.

The browser (Web Serial / Web Bluetooth, see ``static/js/obd.js``) does the
*transport* only: it initialises the ELM327, sends each PID and ships the raw
text the adapter printed straight back to the server. All the actual
protocol work — ISO-TP multi-frame reassembly and the manufacturer byte
layout — lives HERE, in Python, so it is unit-testable and correctable in one
place without touching the client.

Two layers:

1. ``reassemble()`` — turns the ELM327 text for ONE query (which may span
   several CAN frames) into the reassembled ISO-TP payload bytes. Works on
   the "raw frames" output produced with ``AT H1`` (headers on) and
   ``AT CAF0`` (CAN auto-formatting off) — the mode ``obd.js`` configures.

2. ``PROFILES`` — a data-driven table of field definitions per vehicle
   family. Each field says which PID it lives in, its byte offset, length,
   scale, sign and unit. Keeping the offsets in a plain table (rather than
   bespoke parsing code) means the engine can be tested exhaustively while
   the car-specific numbers stay easy to verify against a CarScanner readout
   and tweak if a model differs.

⚠️  The Kia/Hyundai offsets below follow the widely-shared community
"Hyundai Kona EV / Kia e-Niro 64 kWh" extended-PID map (EVNotify / SoulEV /
CarScanner). They are the best-documented values but were not verified
against this specific car — every raw response is stored in
``ObdReading.raw_json`` so a capture can always be re-decoded if an offset
needs adjusting. Robert reads the very same PIDs in CarScanner, so a quick
side-by-side confirms them.
"""
from __future__ import annotations

# ── ISO-TP reassembly ────────────────────────────────────────────────
_ERROR_TOKENS = (
    'NO DATA', 'CAN ERROR', 'BUS INIT', 'BUSINIT', 'UNABLE', 'STOPPED',
    'ERROR', 'SEARCHING', 'BUFFER FULL', '?', 'ACT ALERT', 'TIMEOUT',
)


def _is_hex(tok: str) -> bool:
    if not tok:
        return False
    try:
        int(tok, 16)
        return True
    except ValueError:
        return False


def reassemble(raw: str):
    """Reassemble the ISO-TP payload from one ELM327 query's raw text.

    Returns a ``list[int]`` of the response bytes (including the service
    echo, e.g. ``0x62 0x01 0x01 …``) or ``None`` when the adapter reported
    an error / no data.

    Handles single-frame (SF) and multi-frame (FF + consecutive) responses,
    with or without a leading CAN-id token per line. Frames are consumed in
    the order the adapter printed them (the ELM327 emits consecutive frames
    in sequence), and the payload is truncated to the length the first frame
    declared.
    """
    if not raw:
        return None
    upper = raw.upper()
    for err in _ERROR_TOKENS:
        if err in upper:
            return None

    payload: list[int] = []
    total_len = None
    started = False

    for line in raw.splitlines():
        # Drop the ELM327 prompt char wherever it lands (it may be glued to
        # the end of the last data line, not always on its own).
        line = line.strip().upper().replace('\t', ' ').replace('>', ' ').strip()
        if not line or line == 'OK':
            continue
        toks = [tk for tk in line.split(' ') if tk]
        if not toks:
            continue
        # Drop a leading CAN-id token (11-bit like "7EC" = 3 chars, or
        # 29-bit 8-char). A PCI/data byte is always 2 hex chars, so a 3- or
        # 8-char leading hex token is the arbitration id, not data.
        if len(toks) > 1 and _is_hex(toks[0]) and len(toks[0]) in (3, 8):
            toks = toks[1:]
        # Some adapters glue everything into one token — split into bytes.
        if len(toks) == 1 and _is_hex(toks[0]) and len(toks[0]) > 2 and len(toks[0]) % 2 == 0:
            t = toks[0]
            toks = [t[i:i + 2] for i in range(0, len(t), 2)]
        if not all(_is_hex(tk) and len(tk) <= 2 for tk in toks):
            continue
        by = [int(tk, 16) for tk in toks]
        if not by:
            continue
        pci_hi = by[0] >> 4
        if pci_hi == 0:            # Single Frame: 0L <L data bytes>
            length = by[0] & 0x0F
            payload = by[1:1 + length]
            total_len = length
            started = True
            break                  # SF is the whole message
        elif pci_hi == 1:          # First Frame: 1L LL <6 data bytes>
            if len(by) < 2:
                continue
            total_len = ((by[0] & 0x0F) << 8) | by[1]
            payload = by[2:]
            started = True
        elif pci_hi == 2:          # Consecutive Frame: 2N <7 data bytes>
            if not started:
                continue
            payload.extend(by[1:])
        # pci_hi == 3 (flow control) shouldn't appear in a response — ignore.

    if not payload:
        return None
    if total_len is not None:
        payload = payload[:total_len]
    return payload


def _u(resp, start, length):
    """Unsigned big-endian integer from ``resp[start:start+length]``."""
    if start < 0 or start + length > len(resp):
        return None
    val = 0
    for b in resp[start:start + length]:
        val = (val << 8) | b
    return val


def _s(resp, start, length):
    """Signed (two's-complement) big-endian integer."""
    val = _u(resp, start, length)
    if val is None:
        return None
    bits = length * 8
    if val >= (1 << (bits - 1)):
        val -= (1 << bits)
    return val


def _field(resp, spec):
    """Extract one scalar field per its spec dict.

    Spec keys: ``byte`` (offset), ``len`` (bytes, default 1),
    ``signed`` (bool), ``scale`` (multiplier, default 1).
    """
    length = spec.get('len', 1)
    raw = _s(resp, spec['byte'], length) if spec.get('signed') else _u(resp, spec['byte'], length)
    if raw is None:
        return None
    return raw * spec.get('scale', 1.0)


# ── Profiles ─────────────────────────────────────────────────────────
# A profile bundles: the ECU request header, the ELM init lines, the PID
# list to poll, per-scalar field specs (keyed by human name → {pid, byte,
# len, signed, scale}) and the cell-voltage array specs (which PIDs carry
# consecutive cell voltages and where they start).

def _init_lines():
    """Shared ELM327 setup: echo/linefeed off, spaces + headers ON, CAN
    11-bit/500k, raw frames (we reassemble ISO-TP ourselves)."""
    return ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP6', 'ATAT1', 'ATCAF0']


PROFILES = {
    # Hyundai Kona EV / Kia e-Niro / Soul EV — 64 kWh (and 39 kWh) first
    # generation. BMS answers on 7EC to header 7E4, extended service 22.
    'kia_hyundai_ext': {
        'label': 'Kia / Hyundai (Kona, e-Niro, Soul — BMS 7E4)',
        'header': '7E4',
        'init': _init_lines(),
        'pids': ['220101', '220102', '220103', '220104', '220105', '220106'],
        # Scalars. Offsets are 0-based into the reassembled response, whose
        # first three bytes are the echo 62 01 0X.
        'fields': {
            'soc_bms_pct':     {'pid': '220101', 'byte': 6,  'len': 1, 'scale': 0.5},
            'pack_current_a':  {'pid': '220101', 'byte': 12, 'len': 2, 'signed': True, 'scale': 0.1},
            'pack_voltage_v':  {'pid': '220101', 'byte': 14, 'len': 2, 'scale': 0.1},
            'temp_max_c':      {'pid': '220101', 'byte': 16, 'len': 1, 'signed': True},
            'temp_min_c':      {'pid': '220101', 'byte': 17, 'len': 1, 'signed': True},
            'cell_max_v':      {'pid': '220101', 'byte': 25, 'len': 1, 'scale': 0.02},
            'cell_min_v':      {'pid': '220101', 'byte': 27, 'len': 1, 'scale': 0.02},
            'aux_battery_v':   {'pid': '220101', 'byte': 31, 'len': 1, 'scale': 0.1},
            'cumulative_charge_ah':    {'pid': '220101', 'byte': 40, 'len': 4, 'scale': 0.1},
            'cumulative_discharge_ah': {'pid': '220101', 'byte': 44, 'len': 4, 'scale': 0.1},
            # SoH + display SoC live in 220105.
            'soh_pct':         {'pid': '220105', 'byte': 27, 'len': 2, 'scale': 0.1},
            'soc_display_pct': {'pid': '220105', 'byte': 33, 'len': 1, 'scale': 0.5},
        },
        # Module temperatures (byte offsets in 220101). Up to 5 on gen-1.
        'module_temps': {'pid': '220101', 'bytes': [18, 19, 20, 21, 22], 'signed': True},
        # Cell voltages: 32 cells per PID, each 1 byte × 0.02 V, starting at
        # byte 6. 220102/03/04 cover cells 1-96; a 98s pack has 2 more in
        # 220105 but those two are optional and we don't rely on them.
        'cells': [
            {'pid': '220102', 'byte': 6, 'count': 32, 'scale': 0.02},
            {'pid': '220103', 'byte': 6, 'count': 32, 'scale': 0.02},
            {'pid': '220104', 'byte': 6, 'count': 32, 'scale': 0.02},
        ],
    },
    # Generic OBD-II fallback: standard PID 01 5B = Hybrid/EV battery pack
    # remaining life (SoH, %). Supported by a subset of EVs; gives at least
    # a headline SoH when the manufacturer map is unknown.
    'generic_ev': {
        'label': 'Generisch (OBD-II PID 015B — nur SoH)',
        'header': '7DF',
        'init': _init_lines(),
        'pids': ['015B'],
        'fields': {
            # Response 41 5B XX → XX * 100/255 %. Echo bytes 41 5B at [0..1].
            'soh_pct': {'pid': '015B', 'byte': 2, 'len': 1, 'scale': 100.0 / 255.0},
        },
        'module_temps': None,
        'cells': [],
    },
}


# ── Dongle catalog ───────────────────────────────────────────────────
# Curated list of OBD-II / ELM327 adapters known to work for in-browser
# battery reads. The app speaks raw ELM327 AT commands over Web Serial
# (USB) or Web Bluetooth (BLE), so ANY genuine ELM327-compatible adapter
# on one of those two transports works. The two transports the browser
# CANNOT reach are classic Bluetooth (BR/EDR — e.g. OBDLink MX+) and WiFi
# adapters; those are listed so the user understands why they don't show
# up, with a clear hint to pick a USB or BLE model instead.
#
# ``transport`` drives the client UI:
#   'usb'        → connect via USB (Web Serial); ``baud`` presets the rate
#   'ble'        → connect via Bluetooth LE (Web Bluetooth)
#   'usb_ble'    → dual-chip, either transport works
#   'bt_classic' → classic Bluetooth, not reachable from a browser
#   'wifi'       → WiFi, not reachable from a browser
# ``recommended`` marks the quality picks (genuine chip, reliable frames).
DONGLES = [
    {'key': 'obdlink_sx',       'name': 'OBDLink SX (USB)',                    'transport': 'usb',        'baud': 115200, 'recommended': True},
    {'key': 'obdlink_ex',       'name': 'OBDLink EX (USB)',                    'transport': 'usb',        'baud': 115200, 'recommended': True},
    {'key': 'obdlink_cx',       'name': 'OBDLink CX (Bluetooth LE)',           'transport': 'ble',        'baud': None,   'recommended': True},
    {'key': 'vgate_vlinker_fd', 'name': 'Vgate vLinker FD+ (Bluetooth LE)',    'transport': 'ble',        'baud': None,   'recommended': True},
    {'key': 'vgate_vlinker_mc', 'name': 'Vgate vLinker MC+ (Bluetooth LE)',    'transport': 'ble',        'baud': None,   'recommended': True},
    {'key': 'vgate_icar_pro',   'name': 'Vgate iCar Pro BLE 4.0',              'transport': 'ble',        'baud': None,   'recommended': True},
    {'key': 'veepeak_ble_plus', 'name': 'Veepeak OBDCheck BLE+',               'transport': 'ble',        'baud': None,   'recommended': True},
    {'key': 'veepeak_ble',      'name': 'Veepeak OBDCheck BLE',                'transport': 'ble',        'baud': None,   'recommended': False},
    {'key': 'generic_usb',      'name': 'Generic ELM327 (USB)',               'transport': 'usb',        'baud': 38400,  'recommended': False},
    {'key': 'generic_ble',      'name': 'Generic ELM327 (Bluetooth LE)',      'transport': 'ble',        'baud': None,   'recommended': False},
    {'key': 'obdlink_mxplus',   'name': 'OBDLink MX+ (Bluetooth Classic)',    'transport': 'bt_classic', 'baud': None,   'recommended': False},
    {'key': 'generic_bt',       'name': 'Generic ELM327 (Bluetooth Classic)', 'transport': 'bt_classic', 'baud': None,   'recommended': False},
    {'key': 'generic_wifi',     'name': 'Generic ELM327 (WiFi)',              'transport': 'wifi',       'baud': None,   'recommended': False},
]

# Browser-usable transports (Web Serial / Web Bluetooth).
_BROWSER_TRANSPORTS = ('usb', 'ble', 'usb_ble')


def dongle_catalog():
    """Return the dongle catalog with a derived ``supported`` flag (True
    when the browser can reach the adapter's transport)."""
    return [dict(d, supported=d['transport'] in _BROWSER_TRANSPORTS)
            for d in DONGLES]


def get_dongle(key: str | None):
    if not key:
        return None
    return next((d for d in DONGLES if d['key'] == key), None)


def profile_for_brand(brand: str | None):
    """Map a vehicle brand to the best decode profile key."""
    b = (brand or '').strip().lower()
    if b in ('kia', 'hyundai', 'genesis'):
        return 'kia_hyundai_ext'
    return 'generic_ev'


def get_profile(key: str):
    return PROFILES.get(key)


def _summ(values):
    """min / max / avg helper over a non-empty numeric list."""
    if not values:
        return (None, None, None)
    return (min(values), max(values), sum(values) / len(values))


def decode(profile_key: str, frames: dict):
    """Decode a dict of ``{pid: raw_elm_text}`` into battery metrics.

    Returns a dict with the scalar fields, cell/temperature summaries and
    the full per-cell / per-temperature arrays. Missing PIDs or fields
    simply come back as ``None`` / absent — nothing raises on partial data.
    """
    prof = PROFILES.get(profile_key)
    if prof is None:
        return {'error': f'unknown profile {profile_key}'}

    # Reassemble every PID once.
    resp = {}
    for pid, raw in (frames or {}).items():
        resp[pid.upper()] = reassemble(raw)

    out = {'profile': profile_key}

    for name, spec in prof['fields'].items():
        r = resp.get(spec['pid'].upper())
        out[name] = round(_field(r, spec), 3) if (r and _field(r, spec) is not None) else None

    # Cell voltages
    cells = []
    for cspec in prof.get('cells', []):
        r = resp.get(cspec['pid'].upper())
        if not r:
            continue
        for i in range(cspec['count']):
            v = _u(r, cspec['byte'] + i, 1)
            if v is None:
                break
            cells.append(round(v * cspec['scale'], 3))
    if cells:
        cmin, cmax, cavg = _summ(cells)
        out['cell_count'] = len(cells)
        out['cell_min_v'] = round(cmin, 3)
        out['cell_max_v'] = round(cmax, 3)
        out['cell_avg_v'] = round(cavg, 3)
        out['cell_delta_mv'] = round((cmax - cmin) * 1000, 1)
        out['cell_voltages'] = cells
    else:
        # Fall back to the min/max cell voltage scalars from 220101 if the
        # per-cell arrays weren't captured.
        cmin = out.get('cell_min_v')
        cmax = out.get('cell_max_v')
        if cmin is not None and cmax is not None:
            out['cell_delta_mv'] = round((cmax - cmin) * 1000, 1)

    # Module / cell temperatures
    temps = []
    mt = prof.get('module_temps')
    if mt:
        r = resp.get(mt['pid'].upper())
        if r:
            for b in mt['bytes']:
                v = _s(r, b, 1) if mt.get('signed') else _u(r, b, 1)
                if v is not None:
                    temps.append(v)
    if temps:
        tmin, tmax, tavg = _summ(temps)
        out['cell_temps'] = temps
        out['temp_avg_c'] = round(tavg, 1)
        # The dedicated max/min-temp bytes (220101 [16]/[17]) are the BMS'
        # own authoritative pack extremes — keep them and only fall back to
        # the per-module array when those scalars weren't present.
        if out.get('temp_min_c') is None:
            out['temp_min_c'] = tmin
        if out.get('temp_max_c') is None:
            out['temp_max_c'] = tmax
    # else keep the scalar temp_min_c/temp_max_c already extracted from 220101.
    if out.get('temp_avg_c') is None and out.get('temp_min_c') is not None and out.get('temp_max_c') is not None:
        out['temp_avg_c'] = round((out['temp_min_c'] + out['temp_max_c']) / 2, 1)

    return out
