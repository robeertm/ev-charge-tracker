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

✅  The Kia/Hyundai offsets below were VERIFIED on 2026-08-08 against a live
read from a real Kia Niro EV (awake, ready-to-drive), correcting a
systematic 1-byte shift the original community map had. Every raw response
is still stored in ``ObdReading.raw_json`` so any capture can be re-decoded
if a future model differs. See the field table for the per-field cross-check.
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


def _expected_prefix(expect):
    """The ISO echo prefix a positive response to request ``expect`` must
    begin with: the service byte + 0x40, then the echoed request bytes.

    ``expect`` is the request PID hex string (e.g. ``'220101'`` → the reply
    starts ``62 01 01``; ``'015B'`` → ``41 5B``). Used to reject stray frames
    the adapter may print before the real answer (see :func:`reassemble`).
    Returns ``None`` when no expectation is given or the string can't parse.
    """
    if not expect:
        return None
    s = str(expect).strip().replace(' ', '')
    try:
        req = [int(s[i:i + 2], 16) for i in range(0, len(s), 2)]
    except ValueError:
        return None
    if not req:
        return None
    return [(req[0] + 0x40) & 0xFF] + req[1:]


def reassemble(raw: str, expect=None):
    """Reassemble the ISO-TP payload from one ELM327 query's raw text.

    Returns a ``list[int]`` of the response bytes (including the service
    echo, e.g. ``0x62 0x01 0x01 …``) or ``None`` when the adapter reported
    an error / no data.

    Handles single-frame (SF) and multi-frame (FF + consecutive) responses,
    with or without a leading CAN-id token per line. Frames are consumed in
    the order the adapter printed them (the ELM327 emits consecutive frames
    in sequence), and the payload is truncated to the length the first frame
    declared.

    ``expect`` (the request PID, e.g. ``'220101'``) makes reassembly robust
    against a *stray* frame the adapter sometimes prints before the real
    reply — e.g. a queued ``7EC 03 59 02 …`` DTC single-frame arriving ahead
    of the ``62 01 01 …`` battery answer. A frame is only accepted as the
    start of the message when its data begins with the ISO echo prefix for
    the request we sent (``59…`` ≠ ``62 01 01`` → skipped, keep looking).
    Without ``expect`` the first SF/FF is taken as-is (legacy behaviour).
    """
    if not raw:
        return None
    upper = raw.upper()
    for err in _ERROR_TOKENS:
        if err in upper:
            return None

    prefix = _expected_prefix(expect)
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
            data = by[1:1 + length]
            # Skip a stray SF whose service echo isn't the one we asked for
            # (e.g. a queued 59 02 DTC frame ahead of our 62 01 01 answer).
            if prefix and data[:len(prefix)] != prefix:
                continue
            payload = data
            total_len = length
            started = True
            break                  # SF is the whole message
        elif pci_hi == 1:          # First Frame: 1L LL <6 data bytes>
            if len(by) < 2:
                continue
            data = by[2:]
            if prefix and data[:len(prefix)] != prefix:
                continue           # stray multi-frame — keep looking
            total_len = ((by[0] & 0x0F) << 8) | by[1]
            payload = data
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
    ``signed`` (bool), ``scale`` (multiplier, default 1),
    ``offset`` (added after scaling, default 0 — for encodings like
    ``raw * 0.5 - 1600``).
    """
    length = spec.get('len', 1)
    raw = _s(resp, spec['byte'], length) if spec.get('signed') else _u(resp, spec['byte'], length)
    if raw is None:
        return None
    return raw * spec.get('scale', 1.0) + spec.get('offset', 0.0)


# ── Profiles ─────────────────────────────────────────────────────────
# A profile bundles: the ECU request header, the ELM init lines, the PID
# list to poll, per-scalar field specs (keyed by human name → {pid, byte,
# len, signed, scale}) and the cell-voltage array specs (which PIDs carry
# consecutive cell voltages and where they start).

def _init_lines():
    """Shared ELM327 setup: echo/linefeed off, spaces + headers ON, CAN
    11-bit/500k, raw frames (we reassemble ISO-TP ourselves)."""
    return ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP6', 'ATAT1', 'ATCAF0']


def _init_lines_xpeng():
    """Like :func:`_init_lines`, but pins the ISO-TP flow control to the
    XPENG BMS request header (704) and filters replies to its response id
    (784). XPENG answers on 704/784 — NOT the Kia-style 7E4/7EC — and its
    multi-frame reads only come back reliably when the flow-control frame
    the ELM327 sends carries the 704 header (ATFCSH) with a user-defined
    flow-control mode (ATFCSM1). Without this, changing ATSH to 704 leaves
    the auto flow control pointing at the wrong id and multi-frame DIDs
    silently return NO DATA. Source: XPCarData + meatpiHQ/wican-fw #517."""
    return ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP6', 'ATAT1',
            'ATCAF0', 'ATCRA784', 'ATFCSH704', 'ATFCSD300000', 'ATFCSM1']


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
        #
        # ✅ VERIFIED 2026-08-08 against a live read from Robert's own Kia Niro
        # EV (awake, ready-to-drive). Every scalar sat exactly 1 byte too low
        # in the original community table — the real 220101 reply carries an
        # extra status byte (0xFF) right after `62 01 01`, so each documented
        # field lands one byte later than the naive "3-byte echo + Bn" count.
        # The +1 offsets below decode that capture to physically sane values:
        # SoC 66 %, pack 376.0 V, aux 14.8 V, cells 3.82-3.84 V, temps 28-29 C,
        # SoH 94.7 %, display SoC 68 % — cross-checked field by field. The
        # cell max/min block reads as max_v / max_no / min_v / min_no exactly.
        'fields': {
            'soc_bms_pct':     {'pid': '220101', 'byte': 7,  'len': 1, 'scale': 0.5},
            'pack_current_a':  {'pid': '220101', 'byte': 13, 'len': 2, 'signed': True, 'scale': 0.1},
            'pack_voltage_v':  {'pid': '220101', 'byte': 15, 'len': 2, 'scale': 0.1},
            'temp_max_c':      {'pid': '220101', 'byte': 17, 'len': 1, 'signed': True},
            'temp_min_c':      {'pid': '220101', 'byte': 18, 'len': 1, 'signed': True},
            'cell_max_v':      {'pid': '220101', 'byte': 26, 'len': 1, 'scale': 0.02},
            'cell_min_v':      {'pid': '220101', 'byte': 28, 'len': 1, 'scale': 0.02},
            'aux_battery_v':   {'pid': '220101', 'byte': 32, 'len': 1, 'scale': 0.1},
            'cumulative_charge_ah':    {'pid': '220101', 'byte': 41, 'len': 4, 'scale': 0.1},
            'cumulative_discharge_ah': {'pid': '220101', 'byte': 45, 'len': 4, 'scale': 0.1},
            # SoH + display SoC live in 220105 (same +1 shift).
            'soh_pct':         {'pid': '220105', 'byte': 28, 'len': 2, 'scale': 0.1},
            'soc_display_pct': {'pid': '220105', 'byte': 34, 'len': 1, 'scale': 0.5},
        },
        # Module temperatures (byte offsets in 220101). Up to 5 on gen-1. These
        # already point at the real per-module run (0x1C = 28 C on the verified
        # capture) — the dedicated temp_min/max scalars above are the +1-shifted
        # authoritative extremes, so leave these where they read the run.
        'module_temps': {'pid': '220101', 'bytes': [18, 19, 20, 21, 22], 'signed': True},
        # Cell voltages: 32 cells per PID, each 1 byte × 0.02 V. The 220102/03/04
        # replies carry a 4-byte prefix (62 01 0X + one status byte) so the cells
        # start at byte 7 — byte 6 would read the trailing 0xFF as a bogus 5.10 V
        # cell (verified: byte 7 gives the real 0xBF = 3.82 V run). 96 cells over
        # the three PIDs; a 98s pack has 2 more in 220105 we don't rely on.
        'cells': [
            {'pid': '220102', 'byte': 7, 'count': 32, 'scale': 0.02},
            {'pid': '220103', 'byte': 7, 'count': 32, 'scale': 0.02},
            {'pid': '220104', 'byte': 7, 'count': 32, 'scale': 0.02},
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
    # XPENG (G6 baseline) — BMS answers on request header 704 / response
    # 784, UDS service 22 with 4-digit data identifiers of the form 2211xx.
    # This is the only publicly reverse-engineered XPENG battery map
    # (XPCarData + wican-fw #517). The G6 is documented; G3/P7/P5/G9 use the
    # same OEM diagnostic stack and MAY share these ids, but their exact DIDs
    # are not published — if 704 returns NO DATA on those models, run the
    # in-app ECU scan (see scan_plan) to find the address that answers.
    #
    # ⚠️  These byte offsets/scalings are COMMUNITY-GUESSED and even disputed
    # between sources — treat every decoded value as experimental. As with
    # the Kia map, the raw frames are always stored (ObdReading.raw_json) so a
    # capture can be re-decoded once verified against CarScanner. Byte offset
    # = 3 (the 62 11 xx service echo) + the community "Bn" data-byte index.
    'xpeng': {
        'label': 'XPENG (G6 — experimentell, unbestätigt)',
        'header': '704',
        'init': _init_lines_xpeng(),
        'pids': ['221101', '221103', '221105', '221106',
                 '221107', '221108', '221109', '22110A'],
        'fields': {
            # Pack voltage 221101 B4:B5 / 10.
            'pack_voltage_v': {'pid': '221101', 'byte': 7, 'len': 2, 'scale': 0.1},
            # Pack current 221103 B4:B5 * 0.5 - 1600 (negative = charging).
            'pack_current_a': {'pid': '221103', 'byte': 7, 'len': 2, 'scale': 0.5, 'offset': -1600.0},
            # Cell voltage max/min 221105/221106 B0:B1 / 1000 (mV -> V).
            'cell_max_v': {'pid': '221105', 'byte': 3, 'len': 2, 'scale': 0.001},
            'cell_min_v': {'pid': '221106', 'byte': 3, 'len': 2, 'scale': 0.001},
            # Temp max/min 221107/221108 B0 - 40.
            'temp_max_c': {'pid': '221107', 'byte': 3, 'len': 1, 'offset': -40.0},
            'temp_min_c': {'pid': '221108', 'byte': 3, 'len': 1, 'offset': -40.0},
            # SoC (BMS) 221109 B0:B1 / 10.
            'soc_bms_pct': {'pid': '221109', 'byte': 3, 'len': 2, 'scale': 0.1},
            # SoH 22110A B4:B5 / 10.
            'soh_pct': {'pid': '22110A', 'byte': 7, 'len': 2, 'scale': 0.1},
        },
        # Per-cell arrays and per-module temps use undocumented multi-frame
        # DIDs (only in CarScanner's closed profile) — we don't guess them.
        'module_temps': None,
        'cells': [],
    },
}


# ── ECU discovery scan ───────────────────────────────────────────────
# When a normal read returns nothing but NO DATA, the header we addressed
# has no ECU answering — wrong profile for the car, or an unknown model
# whose BMS lives at a different id. Rather than leave the user staring at
# six NO DATAs, the scan enumerates which ECUs are actually alive on the
# bus (functional broadcast 0100 at 7DF) and probes the known EV-BMS header
# pairs with a real UDS request, so we can tell "704 answered → use the
# XPENG profile" from "nobody answers → the OBD port is gateway-locked".
SCAN_CANDIDATES = [
    {'key': 'xpeng', 'header': '704', 'resp': '784', 'probe': '221101',
     'label': 'XPENG BMS (704/784)', 'profile': 'xpeng'},
    {'key': 'kia_hyundai', 'header': '7E4', 'resp': '7EC', 'probe': '220101',
     'label': 'Kia/Hyundai BMS (7E4/7EC)', 'profile': 'kia_hyundai_ext'},
    {'key': 'bms_7e5', 'header': '7E5', 'resp': '7ED', 'probe': '220101',
     'label': 'BMS (7E5/7ED)', 'profile': None},
    {'key': 'bms_7e2', 'header': '7E2', 'resp': '7EA', 'probe': '220101',
     'label': 'BMS (7E2/7EA)', 'profile': None},
]


def scan_plan():
    """The step list the browser runs for an ECU scan. Auto-detects the CAN
    protocol (ATSP0), reads it back (ATDPN), enumerates responders with a
    functional 0100, then probes each candidate BMS header. The client stays
    a dumb transport: it just runs these lines and ships the raw text back to
    :func:`interpret_scan`."""
    return {
        'init': ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP0', 'ATAT1', 'ATCAF0'],
        'protocol_cmd': 'ATDPN',
        'enumerate': {'header': '7DF', 'cmd': '0100'},
        'probes': [{'key': c['key'], 'header': c['header'], 'cmd': c['probe'],
                    'label': c['label']} for c in SCAN_CANDIDATES],
    }


def _scan_headers(raw):
    """Distinct CAN arbitration ids (responder headers) seen in raw ELM327
    text — the 3- or 8-char leading hex token on each data line."""
    if not raw:
        return []
    found = []
    for line in raw.splitlines():
        line = line.strip().upper().replace('\t', ' ').replace('>', ' ').strip()
        toks = [tk for tk in line.split(' ') if tk]
        if len(toks) > 1 and _is_hex(toks[0]) and len(toks[0]) in (3, 8):
            if toks[0] not in found:
                found.append(toks[0])
    return found


def _classify_probe(raw):
    """Classify a candidate-header probe response:
    'ok'       — a positive UDS/OBD reply (0x62/0x50/0x41): the profile fits.
    'rejected' — a 0x7F negative reply: the ECU IS there but declined this
                 request (right address, wrong DID/session) — still a find.
    'nodata'   — nothing answered at that header.
    """
    payload = reassemble(raw)
    if payload:
        if payload[0] in (0x62, 0x50, 0x41):
            return 'ok'
        if payload[0] == 0x7F:
            return 'rejected'
    # reassemble() returns None on NO DATA/errors; a stray 7F that failed
    # ISO-TP reassembly still counts as "ECU alive".
    if raw and '7F' in raw.upper() and not any(
            e in raw.upper() for e in ('NO DATA', 'UNABLE', 'CAN ERROR')):
        return 'rejected'
    return 'nodata'


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
    if b == 'xpeng':
        return 'xpeng'
    return 'generic_ev'


def interpret_scan(results: dict):
    """Turn the raw text an ECU scan collected into a human report.

    ``results`` = ``{'protocol': raw, 'enumerate': raw, 'probes': {key: raw}}``.
    Returns ``{'lines': [str], 'ecus': [str], 'recommended_profile': key|None,
    'ok': bool}`` — ``lines`` are localised for the current request language.
    """
    from services.i18n import t

    proto_raw = (results or {}).get('protocol') or ''
    ecus = _scan_headers((results or {}).get('enumerate') or '')
    probes = (results or {}).get('probes') or {}

    lines = []
    # Detected CAN protocol. ATDPN prints the protocol number (e.g. "6"), with
    # a leading "A" when it was auto-detected via ATSP0 (e.g. "A6") — strip it.
    hexchars = ''.join(ch for ch in proto_raw.upper() if ch in '0123456789ABCDEF')
    pnum = hexchars.lstrip('A')[:1] if hexchars else ''
    proto_names = {
        '6': 'ISO 15765-4 CAN 11-bit 500k', '7': 'ISO 15765-4 CAN 29-bit 500k',
        '8': 'ISO 15765-4 CAN 11-bit 250k', '9': 'ISO 15765-4 CAN 29-bit 250k',
    }
    if pnum:
        lines.append(t('obd.scan_protocol', default='Erkanntes Protokoll: {p}',
                       p=proto_names.get(pnum, 'ATSP' + pnum)))
    if ecus:
        lines.append(t('obd.scan_ecus', default='Antwortende Steuergeräte: {list}',
                       list=', '.join(ecus)))
    else:
        lines.append(t('obd.scan_no_ecus',
                       default='Kein Steuergerät hat auf die Standard-Abfrage geantwortet.'))

    recommended = None
    for c in SCAN_CANDIDATES:
        cls = _classify_probe(probes.get(c['key']))
        if cls == 'ok':
            lines.append(t('obd.scan_hit', default='✓ {label} antwortet mit Daten.',
                           label=c['label']))
            if recommended is None and c['profile']:
                recommended = c['profile']
        elif cls == 'rejected':
            lines.append(t('obd.scan_alive',
                           default='• {label} ist vorhanden, lehnt aber diese Abfrage ab (falsche DID/Sitzung).',
                           label=c['label']))
            if recommended is None and c['profile']:
                recommended = c['profile']

    if recommended:
        prof = PROFILES.get(recommended)
        lines.append(t('obd.scan_recommend',
                       default='Empfehlung: Profil „{label}" wählen und erneut auslesen.',
                       label=prof['label'] if prof else recommended))
    elif not ecus:
        lines.append(t('obd.scan_locked',
                       default='Es antwortet gar nichts — vermutlich ist der OBD-Port ab Werk gesperrt (Gateway). Batteriedaten sind dann über den OBD-Stecker nicht auslesbar.'))
    else:
        lines.append(t('obd.scan_no_bms',
                       default='Standard-Steuergeräte antworten, aber keine bekannte Batterie-Adresse. Das Fahrzeug nutzt vermutlich eine noch undokumentierte BMS-Adresse.'))

    return {'lines': lines, 'ecus': ecus,
            'recommended_profile': recommended, 'ok': bool(recommended)}


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

    # Reassemble every PID once. Pass the PID so a stray frame (a queued DTC
    # single-frame the adapter prints ahead of the real answer) is skipped
    # instead of clobbering the whole reply.
    resp = {}
    for pid, raw in (frames or {}).items():
        resp[pid.upper()] = reassemble(raw, pid)

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

    # Derived instantaneous pack power (kW). CarScanner shows this as a live
    # gauge; it's just V×A, signed the same way as the current (negative =
    # charging on the Kia/Hyundai sign convention). Only when both are present.
    if out.get('pack_voltage_v') is not None and out.get('pack_current_a') is not None:
        out['power_kw'] = round(out['pack_voltage_v'] * out['pack_current_a'] / 1000.0, 2)

    return out
