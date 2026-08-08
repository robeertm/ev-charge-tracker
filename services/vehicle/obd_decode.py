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

Beyond Kia/Hyundai and XPENG, the table also ships community-sourced maps for
Jaguar I-Pace, MG/SAIC (ZS EV, MG5) + MG4/MULAN, BYD (Atto 3, Dolphin),
Nissan Leaf/e-NV200, Renault Zoe (Ph1 + ZE50), and the VW MEB platform
(ID.3/4/5, Enyaq, Cupra Born, Q4 e-tron). Each carries its documented source
in a comment and a confidence caveat in its label. These offsets are NOT
bench-verified on Robert's own car (he drives the Kia) — they are transcribed
from open reference implementations (OpenVehicles/OVMS, CanZE, evDash, OBDb,
the meatpiHQ/WiCAN profiles and community Torque/CarScanner PID lists). As
with every profile the raw frames are stored, so a capture can be re-decoded
if a value looks off. Tesla is deliberately absent: it exposes no standard
UDS battery PIDs — its pack data is only reachable by passively sniffing the
raw vehicle CAN bus with a model-specific harness, which a browser-driven
ELM327 transport cannot do.

Encoding notes shared across the newer maps: fields support little-endian
byte order (``le`` — BYD) and the ``(raw - k) * scale`` convention (expressed
as ``scale`` + ``offset = -k*scale``); cell arrays support multi-byte cells
(``len``) and a per-cell ``offset`` (e.g. ``raw/1000 + 1`` V). Service 0x21
(KWP group reads — Leaf, Zoe Ph1) and 29-bit extended addressing (VW MEB, Zoe
ZE50) are handled via dedicated init helpers.
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


def _u(resp, start, length, le=False):
    """Unsigned integer from ``resp[start:start+length]``.

    Big-endian by default; ``le=True`` reads the bytes little-endian (some
    OEMs, e.g. BYD, encode their multi-byte scalars low-byte-first)."""
    if start < 0 or start + length > len(resp):
        return None
    chunk = resp[start:start + length]
    if le:
        chunk = chunk[::-1]
    val = 0
    for b in chunk:
        val = (val << 8) | b
    return val


def _s(resp, start, length, le=False):
    """Signed (two's-complement) integer (big- or little-endian)."""
    val = _u(resp, start, length, le)
    if val is None:
        return None
    bits = length * 8
    if val >= (1 << (bits - 1)):
        val -= (1 << bits)
    return val


def _field(resp, spec):
    """Extract one scalar field per its spec dict.

    Spec keys: ``byte`` (offset), ``len`` (bytes, default 1),
    ``signed`` (bool), ``le`` (little-endian byte order, default big),
    ``scale`` (multiplier, default 1), ``offset`` (added after scaling,
    default 0 — encodes ``raw * scale + offset``, which also covers the
    ``(raw - k) * scale`` convention CanZE/OVMS use via ``offset = -k*scale``).
    """
    length = spec.get('len', 1)
    le = spec.get('le', False)
    raw = _s(resp, spec['byte'], length, le) if spec.get('signed') else _u(resp, spec['byte'], length, le)
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


def _init_flowctrl(req, resp):
    """11-bit CAN init that pins the ISO-TP flow control to a BMS on a
    non-default header (``req``) and filters replies to ``resp``. Needed by
    any ECU whose battery DIDs come back multi-frame — without a user
    flow-control frame carrying the request header, the ELM327's auto flow
    control points at the wrong id and the read silently returns NO DATA.
    The client appends ``ATSH<req>`` after these lines (see obd.html)."""
    return ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP6', 'ATAT1',
            'ATCAF0', 'ATCRA' + resp, 'ATFCSH' + req, 'ATFCSD300000', 'ATFCSM1']


def _init_29bit(req, resp):
    """29-bit extended-address CAN init (ISO 15765-4, 29-bit, 500k → ATSP7).
    Used by the VW MEB platform (17FC007B/17FE007B) and the Renault Zoe ZE50
    LBC (18DADBF1/18DAF1DB), whose BMS is addressed with a full 4-byte CAN id
    rather than the 11-bit 7Ex range. ``req``/``resp`` are the 8-hex ids; the
    client appends ``ATSH<req>`` afterwards. NOTE: setting a 29-bit header via
    a single ATSH<8hex> works on the STN/OBDLink and most v1.5 clones the app
    recommends; a strict genuine ELM327 that only takes a 3-byte ATSH would
    need ATCP for the priority byte — such adapters simply return NO DATA here
    and the ECU-scan hint kicks in, and the raw frames are stored regardless."""
    return ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP7', 'ATAT1',
            'ATCAF0', 'ATCRA' + resp, 'ATFCSH' + req, 'ATFCSD300000', 'ATFCSM1']


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
    # ── Jaguar I-Pace (EV400) — BECM on 7E4/7EC, UDS service 22 ─────────
    # Source: OpenVehicles/OVMS Jaguar I-Pace driver (ipace_poll_becm.cpp),
    # cross-checked against the community Torque/CarScanner PID list. Every
    # scale below is copied from OVMS' own decode. The BECM exposes only
    # aggregate (min/max/avg) cell + temperature data — there is NO documented
    # per-cell array DID, so ``cells`` stays empty (min/max fill the delta).
    # Byte offsets = 3 (the 62 49 xx service echo) + OVMS' data index.
    'jaguar_ipace': {
        'label': 'Jaguar I-Pace (BECM 7E4)',
        'header': '7E4',
        'init': _init_flowctrl('7E4', '7EC'),
        'pids': ['224910', '224918', '22490F', '22490C',
                 '224903', '224904', '224905', '224906', '224907'],
        'fields': {
            'soc_bms_pct':    {'pid': '224910', 'byte': 3, 'len': 2, 'scale': 0.01},
            'soh_pct':        {'pid': '224918', 'byte': 3, 'len': 1, 'scale': 0.5},
            'pack_voltage_v': {'pid': '22490F', 'byte': 3, 'len': 2, 'scale': 0.01},
            # (raw - 0x8000) / 40  →  raw*0.025 - 819.2
            'pack_current_a': {'pid': '22490C', 'byte': 3, 'len': 2, 'scale': 0.025, 'offset': -819.2},
            'cell_max_v':     {'pid': '224903', 'byte': 3, 'len': 2, 'scale': 0.001},
            'cell_min_v':     {'pid': '224904', 'byte': 3, 'len': 2, 'scale': 0.001},
            'temp_max_c':     {'pid': '224905', 'byte': 3, 'len': 1, 'scale': 0.5, 'offset': -40.0},
            'temp_min_c':     {'pid': '224906', 'byte': 3, 'len': 1, 'scale': 0.5, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [],
    },
    # ── MG / SAIC (ZS EV, MG5) — BMS on 781/789, UDS service 22 ─────────
    # Source: bugcoder76 Torque extended-PID CSVs + the MGEVs reverse-
    # engineering sheet + OBDb/MG-MG4 signalset. DIDs B0xx. The MG4/MULAN
    # answers the SAME DIDs on a different header — see ``mg_mulan``.
    # Byte offsets = 3 (62 B0 xx echo) + the CSV "A" data index.
    'mg_saic': {
        'label': 'MG / SAIC (ZS EV, MG5 — BMS 781)',
        'header': '781',
        'init': _init_flowctrl('781', '789'),
        'pids': ['22B046', '22B061', '22B042', '22B043',
                 '22B058', '22B059', '22B056'],
        'fields': {
            'soc_bms_pct':    {'pid': '22B046', 'byte': 3, 'len': 2, 'scale': 0.1},
            'soh_pct':        {'pid': '22B061', 'byte': 3, 'len': 2, 'scale': 0.01},
            'pack_voltage_v': {'pid': '22B042', 'byte': 3, 'len': 2, 'scale': 0.25},
            # raw*0.025 - 1000 (unsigned raw, sign falls out of the offset)
            'pack_current_a': {'pid': '22B043', 'byte': 3, 'len': 2, 'scale': 0.025, 'offset': -1000.0},
            'cell_max_v':     {'pid': '22B058', 'byte': 3, 'len': 2, 'scale': 0.001},
            'cell_min_v':     {'pid': '22B059', 'byte': 3, 'len': 2, 'scale': 0.001},
            'temp_max_c':     {'pid': '22B056', 'byte': 3, 'len': 1, 'scale': 0.5, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [],
    },
    # ── MG4 / MULAN (MSP platform) — same SAIC DIDs on 7E5/7ED ──────────
    # Source: OBDb/MG-MG4 signalset (req 7DF broadcast / resp 7ED; a directed
    # 7E5 also answers) + MGEVs MG4 threads. The MG4 also exposes a 24-module
    # cell map (B001…B115, two cells + two temps per DID) — not modelled here
    # because it isn't a contiguous cell array; min/max come from the pack DIDs.
    'mg_mulan': {
        'label': 'MG4 / MULAN (BMS 7E5)',
        'header': '7E5',
        'init': _init_flowctrl('7E5', '7ED'),
        'pids': ['22B046', '22B061', '22B042', '22B043', '22B056'],
        'fields': {
            'soc_bms_pct':    {'pid': '22B046', 'byte': 3, 'len': 2, 'scale': 0.1},
            'soh_pct':        {'pid': '22B061', 'byte': 3, 'len': 2, 'scale': 0.01},
            'pack_voltage_v': {'pid': '22B042', 'byte': 3, 'len': 2, 'scale': 0.25},
            'pack_current_a': {'pid': '22B043', 'byte': 3, 'len': 2, 'scale': 0.025, 'offset': -1000.0},
            'temp_max_c':     {'pid': '22B056', 'byte': 3, 'len': 1, 'scale': 0.5, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [],
    },
    # ── BYD Atto 3 / Dolphin (Blade LFP) — BMS on 7E7/7EF, service 22 ───
    # Source: OpenVehicles/OVMS BYD Atto3 driver + meatpiHQ/wican-fw atto3.json
    # + loryanstrant/BYD-PID-list. IMPORTANT: BYD encodes its multi-byte
    # scalars LITTLE-ENDIAN (``le``). SoH is not exposed on any documented
    # generic DID (LFP pack; needs a factory tool) → left None. Byte offset
    # 3 = first data byte after the 62 xx xx echo (CarScanner "B4").
    'byd_atto': {
        'label': 'BYD (Atto 3, Dolphin — BMS 7E7)',
        'header': '7E7',
        'init': _init_flowctrl('7E7', '7EF'),
        'pids': ['221FFC', '220008', '220009', '220031', '22002F', '220032'],
        'fields': {
            'soc_bms_pct':    {'pid': '221FFC', 'byte': 3, 'len': 2, 'le': True, 'scale': 0.01},
            'pack_voltage_v': {'pid': '220008', 'byte': 3, 'len': 2, 'le': True, 'scale': 1.0},
            # (raw - 5000) / 10  →  raw*0.1 - 500
            'pack_current_a': {'pid': '220009', 'byte': 3, 'len': 2, 'le': True, 'scale': 0.1, 'offset': -500.0},
            'temp_max_c':     {'pid': '220031', 'byte': 3, 'len': 1, 'scale': 1.0, 'offset': -40.0},
            'temp_min_c':     {'pid': '22002F', 'byte': 3, 'len': 1, 'scale': 1.0, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [],
    },
    # ── Nissan Leaf / e-NV200 — LBC on 79B/7BB, KWP service 21 ──────────
    # Source: OpenVehicles/OVMS Nissan Leaf driver + dalathegreat DBC +
    # MyNissanLeaf decoding thread. The LBC answers manufacturer service 0x21
    # (group reads), NOT the Hyundai-style 0x22. The reassembled reply starts
    # `61 0X` (2-byte echo), so byte offset = 2 + OVMS' data-start index.
    #   • 2102 carries all 96 cell voltages as 2-byte big-endian millivolts.
    #   • 2104 carries the pack thermistors (direct °C bytes).
    #   • SoC/Hx offsets below are the ZE1 (2018+, 40/62 kWh) layout — the
    #     older ZE0/AZE0 packs place them differently, so those scalars are
    #     experimental on pre-2018 cars (cells/temps stay valid). Pack CURRENT
    #     is only on the passive 0x1DB broadcast, not a group read → omitted.
    'nissan_leaf': {
        'label': 'Nissan Leaf / e-NV200 (LBC 79B — Zellen+Temp, SoC exp.)',
        'header': '79B',
        'init': _init_flowctrl('79B', '7BB'),
        'pids': ['2101', '2102', '2104'],
        'fields': {
            # SoC (ZE1): raw/10000 → *0.0001, unit already %.
            'soc_bms_pct': {'pid': '2101', 'byte': 33, 'len': 3, 'scale': 0.0001},
            # Hx capacity health (ZE1): raw/102.4.
            'soh_pct':     {'pid': '2101', 'byte': 30, 'len': 2, 'scale': 1.0 / 102.4},
        },
        # Four direct-°C thermistor bytes in 2104 (offsets 2,5,8,11 from data
        # start → +2 for the 61 04 echo).
        'module_temps': {'pid': '2104', 'bytes': [4, 7, 10, 13], 'signed': True},
        # 96 cells, 2-byte big-endian mV, from data start (byte 2 after echo).
        'cells': [
            {'pid': '2102', 'byte': 2, 'count': 96, 'len': 2, 'scale': 0.001},
        ],
    },
    # ── Renault Zoe ZE50 (Ph2, R110/R135) — LBC on 18DADBF1/18DAF1DB ────
    # 29-bit extended addressing, UDS service 22, DIDs 90xx. Source: CanZE
    # (fesch/CanZE) ZOE_Ph2 LBC field DB + ljames28 Ph2 LBC notes. CanZE's
    # convention is physical = (raw - offset) * resolution, mapped here to
    # raw*scale + (-offset*scale). Response starts `62 90 xx` → byte 3 = data.
    'renault_zoe_ph2': {
        'label': 'Renault Zoe ZE50 / R135 (LBC 18DADBF1)',
        'header': '18DADBF1',
        'init': _init_29bit('18DADBF1', '18DAF1DB'),
        'pids': ['229001', '229003', '229005', '22900D',
                 '229007', '229009', '229013', '229014'],
        'fields': {
            'soc_bms_pct':    {'pid': '229001', 'byte': 3, 'len': 2, 'scale': 0.01, 'offset': -3.0},
            'soh_pct':        {'pid': '229003', 'byte': 3, 'len': 2, 'scale': 0.01},
            'pack_voltage_v': {'pid': '229005', 'byte': 3, 'len': 2, 'scale': 0.1},
            # (raw - 48000) * 0.025
            'pack_current_a': {'pid': '22900D', 'byte': 3, 'len': 4, 'scale': 0.025, 'offset': -1200.0},
            'cell_max_v':     {'pid': '229007', 'byte': 3, 'len': 2, 'scale': 0.000976563},
            'cell_min_v':     {'pid': '229009', 'byte': 3, 'len': 2, 'scale': 0.000976563},
            'temp_min_c':     {'pid': '229013', 'byte': 3, 'len': 2, 'scale': 0.0625, 'offset': -40.0},
            'temp_max_c':     {'pid': '229014', 'byte': 3, 'len': 2, 'scale': 0.0625, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [],
    },
    # ── Renault Zoe Ph1 (Q210/R240/Q90/R110) — LBC on 79B/7BB, svc 21 ───
    # Source: CanZE ZOE (Ph1) LBC field DB. 11-bit, KWP service 21 groups.
    # CanZE bit positions are byte-aligned; byte offset counts from the `61`
    # response byte (offset 0), so these are used as-is (no +2 shift — CanZE
    # already indexes the echo). Per-cell array 2141 is community-MEDIUM.
    'renault_zoe_ph1': {
        'label': 'Renault Zoe Ph1 / Kangoo ZE (LBC 79B)',
        'header': '79B',
        'init': _init_flowctrl('79B', '7BB'),
        'pids': ['2101', '2103', '2104', '2142', '2161', '2141'],
        'fields': {
            'soc_bms_pct':    {'pid': '2103', 'byte': 24, 'len': 2, 'scale': 0.01},
            'soh_pct':        {'pid': '2161', 'byte': 9,  'len': 1, 'scale': 0.5},
            'pack_voltage_v': {'pid': '2142', 'byte': 72, 'len': 2, 'scale': 0.01},
            # (raw - 5000) * 0.1
            'pack_current_a': {'pid': '2101', 'byte': 2,  'len': 2, 'scale': 0.1, 'offset': -500.0},
            'aux_battery_v':  {'pid': '2101', 'byte': 28, 'len': 2, 'scale': 0.01},
            'temp_max_c':     {'pid': '2104', 'byte': 75, 'len': 1, 'scale': 1.0, 'offset': -40.0},
        },
        'module_temps': None,
        'cells': [
            {'pid': '2141', 'byte': 2, 'count': 96, 'len': 2, 'scale': 0.001},
        ],
    },
    # ── VW Group MEB — ID.3/4/5, Enyaq, Cupra Born, Q4 e-tron ───────────
    # BMS on 29-bit 17FC007B/17FE007B, UDS service 22. Source: spot2000 &
    # raimuucka VW-MEB UDS CSVs + nickn17/evDash CarVWID3.cpp. There is NO
    # direct SoH % DID (the "max energy content" DID 2AB2 on header 710 has an
    # undocumented formula) → SoH left None. Per-cell voltages are one DID per
    # cell (1E40…1EAB, 108 reads) — not polled; pack min/max via 1E33/1E34.
    # Response starts `62 xx xx` → byte 3 = first data byte.
    'vw_meb': {
        'label': 'VW MEB (ID.3/4/5, Enyaq, Born, Q4 — BMS 17FC007B)',
        'header': '17FC007B',
        'init': _init_29bit('17FC007B', '17FE007B'),
        'pids': ['22028C', '221E3B', '221E3D', '222A0B', '221E33', '221E34'],
        'fields': {
            'soc_bms_pct':    {'pid': '22028C', 'byte': 3, 'len': 1, 'scale': 0.4},
            'pack_voltage_v': {'pid': '221E3B', 'byte': 3, 'len': 2, 'scale': 0.25},
            # (raw - 150000) / 100
            'pack_current_a': {'pid': '221E3D', 'byte': 3, 'len': 4, 'scale': 0.01, 'offset': -1500.0},
            'temp_max_c':     {'pid': '222A0B', 'byte': 3, 'len': 1, 'scale': 0.5, 'offset': -40.0},
            'cell_max_v':     {'pid': '221E33', 'byte': 3, 'len': 2, 'scale': 1.0 / 4096.0},
            'cell_min_v':     {'pid': '221E34', 'byte': 3, 'len': 2, 'scale': 1.0 / 4096.0},
        },
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
# Only 11-bit candidates are probed here: the scan auto-detects the protocol
# with ATSP0 and a plain ATSH<3-hex> works under any detected 11-bit CAN mode.
# The 29-bit BMS profiles (VW MEB, Zoe ZE50) need ATSP7 + an 8-hex header, so
# they aren't auto-probed — the user selects them from the profile list.
SCAN_CANDIDATES = [
    {'key': 'xpeng', 'header': '704', 'resp': '784', 'probe': '221101',
     'label': 'XPENG BMS (704/784)', 'profile': 'xpeng'},
    {'key': 'kia_hyundai', 'header': '7E4', 'resp': '7EC', 'probe': '220101',
     'label': 'Kia/Hyundai BMS (7E4/7EC)', 'profile': 'kia_hyundai_ext'},
    {'key': 'jaguar_ipace', 'header': '7E4', 'resp': '7EC', 'probe': '224910',
     'label': 'Jaguar I-Pace BECM (7E4/7EC)', 'profile': 'jaguar_ipace'},
    {'key': 'mg_saic', 'header': '781', 'resp': '789', 'probe': '22B046',
     'label': 'MG/SAIC BMS (781/789)', 'profile': 'mg_saic'},
    {'key': 'byd_atto', 'header': '7E7', 'resp': '7EF', 'probe': '220005',
     'label': 'BYD BMS (7E7/7EF)', 'profile': 'byd_atto'},
    {'key': 'nissan_leaf', 'header': '79B', 'resp': '7BB', 'probe': '2101',
     'label': 'Nissan Leaf LBC (79B/7BB)', 'profile': 'nissan_leaf'},
    {'key': 'mg_mulan', 'header': '7E5', 'resp': '7ED', 'probe': '22B046',
     'label': 'MG4/MULAN BMS (7E5/7ED)', 'profile': 'mg_mulan'},
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
        # 0x62 = UDS svc 22, 0x61 = KWP svc 21 (Leaf/Zoe Ph1), 0x50 = session,
        # 0x41 = OBD mode 01 — all positive replies that mean "ECU is here".
        if payload[0] in (0x62, 0x61, 0x50, 0x41):
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
    """Map a vehicle brand to the best decode profile key. Where a brand ships
    several BMS generations (e.g. Renault Ph1/Ph2, MG SAIC/MULAN) the most
    common current platform is chosen; the others stay selectable in the UI."""
    b = (brand or '').strip().lower()
    if b in ('kia', 'hyundai', 'genesis'):
        return 'kia_hyundai_ext'
    if b == 'xpeng':
        return 'xpeng'
    if b in ('nissan',):
        return 'nissan_leaf'
    if b in ('renault', 'dacia'):
        return 'renault_zoe_ph2'
    if b in ('vw', 'volkswagen', 'skoda', 'škoda', 'cupra', 'seat', 'audi'):
        return 'vw_meb'
    if b == 'jaguar':
        return 'jaguar_ipace'
    if b == 'mg':
        return 'mg_saic'
    if b == 'byd':
        return 'byd_atto'
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

    # Cell voltages. Each cell spec says which PID carries the array, the
    # start byte, the cell count, the bytes-per-cell (``len``, default 1 —
    # Kia packs one 0.02 V step per byte; Leaf/Zoe/VW use 2-byte big-endian
    # millivolts), the ``scale`` and an optional per-cell ``offset`` (some
    # OEMs encode ``raw/1000 + 1`` V) and ``le`` byte order.
    cells = []
    for cspec in prof.get('cells', []):
        r = resp.get(cspec['pid'].upper())
        if not r:
            continue
        clen = cspec.get('len', 1)
        cle = cspec.get('le', False)
        coff = cspec.get('offset', 0.0)
        for i in range(cspec['count']):
            v = _u(r, cspec['byte'] + i * clen, clen, cle)
            if v is None:
                break
            cells.append(round(v * cspec['scale'] + coff, 3))
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
