# -*- coding: utf-8 -*-
"""Importer for the XPENG EU Data Act export.

What the export is (from the public description of the files by the open
source viewers that read it; there was no real file at hand when this was
written, so everything here is tolerant and the first real file is kept as
a format probe, see ``save_probe``):

* up to three CSV streams per request, one row per second while the car is
  awake, last 7/14/30 days up to yesterday, **no GPS**:
  ``DA<id>_dwd_opp_gdpr_veh_driving_status_di[_partN].csv``,
  ``..._driving_operation_di...`` and ``..._driving_power_energy_di...``.
  Each file holds at most 1,000,000 rows, longer streams continue in
  ``_part1``, ``_part2`` ... (ordered by number, not alphabetically).
* columns in every stream: ``vin``, ``vmodel``, ``timer`` (epoch seconds),
  ``ds`` (date partition).
* operation: ``esp_vehspd`` km/h (255 = n/a), ``cdcu_totalodometer`` km,
  ``ldcu_currentgearlev`` (1=D 2=N 3=R 4=P), ``ldcu_accpedalsig`` ...
* power & energy: ``bms_battvolt`` V (1023 = n/a), ``bms_battcurr`` A
  (positive discharges, negative charges, 1676.7 = n/a), ``ldcu_chrgpwr`` kW
  (only while plugged in, 1638.3 = n/a), ``ldcu_bms_soc_disp`` % (255 = n/a),
  ``ldcu_dstbatdisp_dynamic`` km (1638.3 = n/a), pack temperatures ...

Nothing in the file is a trip or a charge: both are derived here from the
signals. This module is pure (no Flask, no database); ``services.data_act_apply``
files the result.
"""
import csv
import io
import json
import os
import re
import time
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from datetime import datetime

# ---- format ---------------------------------------------------------------

FILE_RE = re.compile(
    r'^(?P<export>DA[A-Za-z0-9]+)_dwd_opp_gdpr_veh_'
    r'(?P<stream>driving_status|driving_operation|driving_power_energy)_di'
    r'(?:_part(?P<part>\d+))?\.csv$', re.I)
STREAM_OF = {'driving_status': 'status', 'driving_operation': 'operation',
             'driving_power_energy': 'power'}

# columns that identify a stream when the file name is not the usual one
_IDENT = {
    'power': ('ldcu_chrgpwr', 'bms_battvolt', 'ldcu_bms_soc_disp'),
    'operation': ('esp_vehspd', 'cdcu_totalodometer', 'ldcu_currentgearlev'),
    'status': ('ldcu_driverdoorajarst', 'ldcu_tpmsprfl'),
}

# value that means "signal not available", per column (physical units)
SENTINELS = {
    'esp_vehspd': (255.0,),
    'bms_battvolt': (1023.0,),
    'bms_battcurr': (1676.7,),
    'ldcu_chrgpwr': (1638.3,),
    'ldcu_bms_soc_disp': (255.0,),
    'ldcu_dstbatdisp_dynamic': (1638.3,),
}
GEAR_D, GEAR_N, GEAR_R, GEAR_P = 1, 2, 3, 4

EPOCH_2015 = 1420070400          # anything older is not a real timestamp

# derivation thresholds (kept near what the public viewers use)
GAP_AWAKE_S = 60                 # longer silence = car asleep
TRIP_STOP_S = 120                # standing this long ends a trip
TRIP_MIN_S = 30
TRIP_MIN_KM = 0.2
CHARGE_MIN_KW = 0.05
CHARGE_JOIN_GAP_S = 4 * 3600     # a nap inside a charge
CHARGE_MIN_S = 300
DC_MIN_KW = 20.0                 # the on-board AC charger tops out at 11 kW
INTEGRATE_MAX_STEP_S = 60
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROBE_ROWS = 12


class DataActError(Exception):
    """A problem the user should be told about; ``code`` selects the text."""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


# ---- reading --------------------------------------------------------------

def classify_name(name):
    """(export_id, stream, part) from a file name, or None."""
    m = FILE_RE.match(os.path.basename(name or ''))
    if not m:
        return None
    return (m.group('export'), STREAM_OF[m.group('stream').lower()],
            int(m.group('part') or 0))


def _open_text(raw):
    return io.TextIOWrapper(raw, encoding='utf-8-sig', errors='replace',
                            newline='')


def _sniff_delimiter(first_line):
    best = max((',', ';', '\t', '|'), key=first_line.count)
    return best if first_line.count(best) else ','


class Source:
    """One CSV file to read: a path on disk, or a member of a ZIP."""

    def __init__(self, label, opener, size=0):
        self.label = label
        self._opener = opener
        self.size = size

    def open(self):
        return self._opener()


def sources_from_path(path, display_name=None):
    """Expand an uploaded file into CSV Sources (ZIPs are opened lazily)."""
    name = display_name or os.path.basename(path)
    if zipfile.is_zipfile(path):
        out = []
        try:
            zf = zipfile.ZipFile(path)
            infos = [i for i in zf.infolist()
                     if not i.is_dir() and i.filename.lower().endswith('.csv')
                     and '__MACOSX' not in i.filename]
        except zipfile.BadZipFile:
            raise DataActError('zip_broken')
        if not infos:
            raise DataActError('zip_no_csv')
        for info in infos:
            def opener(p=path, n=info.filename):
                return zipfile.ZipFile(p).open(n)
            out.append(Source(info.filename, opener, info.file_size))
        return out
    return [Source(name, lambda p=path: open(p, 'rb'), os.path.getsize(path))]


def _read_header(src):
    with src.open() as raw:
        txt = _open_text(raw)
        first = txt.readline()
    delim = _sniff_delimiter(first)
    cols = [c.strip().lower() for c in first.strip('\r\n').split(delim)]
    return delim, cols


def plan_sources(sources):
    """Order the sources per stream. Returns ``{stream: [(part, Source)]}``,
    the export ids and the labels that could not be placed. Raises
    ``DataActError('unknown_format')`` when nothing looks like the export."""
    streams, exports, unknown = {}, [], []
    for src in sources:
        info = classify_name(src.label)
        try:
            delim, cols = _read_header(src)
        except Exception:
            unknown.append(src.label)
            continue
        if 'timer' not in cols:
            unknown.append(src.label)
            continue
        if info:
            export, stream, part = info
        else:
            export, part, stream = None, 0, None
            for name, idents in _IDENT.items():
                if any(c in cols for c in idents):
                    stream = name
                    break
            if stream is None:
                unknown.append(src.label)
                continue
        if export and export not in exports:
            exports.append(export)
        streams.setdefault(stream, []).append((part, src, delim, cols))
    if not streams:
        raise DataActError('unknown_format', files=', '.join(unknown[:5]))
    for lst in streams.values():
        lst.sort(key=lambda x: x[0])
    return streams, exports, unknown


def _num(text):
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        try:
            v = float(text.replace(',', '.'))
        except ValueError:
            return None
    return v if v == v else None


_TS_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y%m%d%H%M%S',
               '%Y-%m-%d %H:%M:%S.%f')


def parse_timer(text):
    """Epoch seconds from the ``timer`` cell. The documented form is epoch
    seconds; epoch milliseconds and date strings are accepted too."""
    v = _num(text)
    if v is not None:
        if v > 1e14:
            v /= 1e6
        elif v > 1e11:
            v /= 1000.0
        return int(v)
    if text:
        for f in _TS_FORMATS:
            try:
                return int(datetime.strptime(text.strip(), f).timestamp())
            except ValueError:
                continue
    return None


class Progress:
    def __init__(self, cb, total_bytes):
        self.cb = cb
        self.total = max(total_bytes, 1)
        self.done = 0
        self.rows = 0
        self.stage = 'read'
        self._last = 0.0

    def tick(self, nbytes=0, rows=0, force=False):
        self.done += nbytes
        self.rows += rows
        now = time.monotonic()
        if self.cb and (force or now - self._last > 0.4):
            self._last = now
            self.cb(self.stage, min(self.done / self.total, 1.0), self.rows)


def _iter_rows(src, delim, cols, want, prog):
    """Yield ``(t, {col: float|None})`` for the wanted columns, streaming."""
    idx_t = cols.index('timer')
    idx = {c: cols.index(c) for c in want if c in cols}
    with src.open() as raw:
        txt = _open_text(raw)
        rdr = csv.reader(txt, delimiter=delim)
        next(rdr, None)
        n = 0
        last_pos = 0
        for row in rdr:
            n += 1
            if n % 20000 == 0:
                prog.tick(rows=20000)
            if len(row) <= idx_t:
                continue
            t = parse_timer(row[idx_t])
            if t is None or t < EPOCH_2015:
                continue
            vals = {}
            for c, i in idx.items():
                v = _num(row[i]) if i < len(row) else None
                if v is not None and v in SENTINELS.get(c, ()):
                    v = None
                vals[c] = v
            yield t, vals
        prog.tick(rows=n % 20000)


def _meta(src, delim, cols):
    """vin / model from the first data row."""
    with src.open() as raw:
        txt = _open_text(raw)
        rdr = csv.reader(txt, delimiter=delim)
        next(rdr, None)
        row = next(rdr, None)
    vin = model = ''
    if row:
        if 'vin' in cols and cols.index('vin') < len(row):
            vin = row[cols.index('vin')].strip()
        if 'vmodel' in cols and cols.index('vmodel') < len(row):
            model = row[cols.index('vmodel')].strip()
    return vin, model


# ---- probe ----------------------------------------------------------------

def _mask(value, col):
    if col in ('vin',):
        return 'MASKED'
    return value


def save_probe(sources, data_dir, reason, note=''):
    """Keep the head of every file (header + a few rows, VIN masked) so a
    format we did not expect can be understood afterwards. Returns the
    folder, or None when nothing could be written."""
    try:
        folder = os.path.join(data_dir, 'data_act', 'probe',
                              time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(folder, exist_ok=True)
        report = {'reason': reason, 'note': note, 'files': []}
        for n, src in enumerate(sources):
            entry = {'name': os.path.basename(src.label), 'bytes': src.size}
            try:
                with src.open() as raw:
                    txt = _open_text(raw)
                    lines = []
                    for _ in range(MAX_PROBE_ROWS + 1):
                        ln = txt.readline()
                        if not ln:
                            break
                        lines.append(ln.rstrip('\r\n')[:2000])
                if lines:
                    delim = _sniff_delimiter(lines[0])
                    head = [c.strip().lower() for c in lines[0].split(delim)]
                    out = [lines[0]]
                    for ln in lines[1:]:
                        cells = ln.split(delim)
                        for i, c in enumerate(head):
                            if i < len(cells):
                                cells[i] = _mask(cells[i], c)
                        out.append(delim.join(cells))
                    entry['columns'] = head
                    with open(os.path.join(folder, 'file%d.head.csv' % n), 'w',
                              encoding='utf-8') as fh:
                        fh.write('\n'.join(out) + '\n')
            except Exception as e:                    # pragma: no cover
                entry['error'] = type(e).__name__
            report['files'].append(entry)
        with open(os.path.join(folder, 'probe.json'), 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=1)
        return folder
    except Exception:                                  # pragma: no cover
        return None


# ---- derivation -----------------------------------------------------------

def _sorted_unique(ts, *cols):
    """Sort parallel arrays by time and drop duplicate timestamps (keep the
    last row for a second), which is what overlapping part files produce."""
    n = len(ts)
    ordered = all(ts[i] <= ts[i + 1] for i in range(n - 1))
    if ordered and all(ts[i] < ts[i + 1] for i in range(n - 1)):
        return ts, cols
    order = sorted(range(n), key=lambda i: (ts[i], i))
    keep = []
    for k, i in enumerate(order):
        if k + 1 < n and ts[order[k + 1]] == ts[i]:
            continue
        keep.append(i)
    nts = array('q', (ts[i] for i in keep))
    ncols = tuple(array(c.typecode, (c[i] for i in keep)) for c in cols)
    return nts, ncols


NAN = float('nan')


def _isnan(v):
    return v != v


def _integrate(ts, vals, i0, i1, positive=None):
    """kWh = integral of a kW series between sample indexes (inclusive),
    ignoring gaps longer than INTEGRATE_MAX_STEP_S. ``positive`` True keeps
    only the positive part, False only the negative part (as positive)."""
    total = 0.0
    prev = NAN
    pt = 0
    for i in range(i0, i1 + 1):
        v = vals[i]
        if _isnan(v):
            prev = NAN
            continue
        if not _isnan(prev):
            dt = ts[i] - pt
            if 0 < dt <= INTEGRATE_MAX_STEP_S:
                e = (v + prev) / 2.0 * dt / 3600.0
                if positive is None:
                    total += e
                elif positive and e > 0:
                    total += e
                elif positive is False and e < 0:
                    total += -e
        prev = v
        pt = ts[i]
    return total


def _first_valid(a, i0, i1):
    for i in range(i0, i1 + 1):
        if not _isnan(a[i]):
            return a[i]
    return NAN


def _last_valid(a, i0, i1):
    for i in range(i1, i0 - 1, -1):
        if not _isnan(a[i]):
            return a[i]
    return NAN


def derive_charges(pw, op):
    """Charging sessions from the power stream (``pw``). ``op`` (operation
    arrays or None) only splits a session when the car moved in between."""
    ts, soc, kw, volt, cur = pw['t'], pw['soc'], pw['kw'], pw['volt'], pw['cur']
    n = len(ts)
    runs = []
    start = last = -1
    for i in range(n):
        v = kw[i]
        if not _isnan(v) and v > CHARGE_MIN_KW:
            if start == -1:
                start = i
            else:
                moved = _moved_between(op, ts[last], ts[i])
                if ts[i] - ts[last] > CHARGE_JOIN_GAP_S or moved:
                    runs.append((start, last))
                    start = i
            last = i
    if start != -1:
        runs.append((start, last))
    # V*I as the independent energy measure
    pwr = array('d', (NAN if (_isnan(volt[i]) or _isnan(cur[i]))
                      else volt[i] * cur[i] / 1000.0 for i in range(n))) \
        if n else array('d')
    out = []
    for a, b in runs:
        dur = ts[b] - ts[a]
        s_from, s_to = _first_valid(soc, a, b), _last_valid(soc, a, b)
        gain = (s_to - s_from) if not (_isnan(s_from) or _isnan(s_to)) else NAN
        if dur < CHARGE_MIN_S and not (gain >= 1):
            continue
        peak = max((kw[i] for i in range(a, b + 1)
                    if not _isnan(kw[i])), default=0.0)
        kwh = _integrate(ts, kw, a, b)
        kwh_vi = _integrate(ts, pwr, a, b, positive=False)   # charging current is negative
        out.append({
            'start': ts[a], 'end': ts[b], 'duration': dur,
            'soc_from': None if _isnan(s_from) else min(100, int(round(s_from))),
            'soc_to': None if _isnan(s_to) else min(100, int(round(s_to))),
            'peak_kw': round(peak, 1),
            'avg_kw': round(kwh / (dur / 3600.0), 1) if dur > 0 and kwh > 0 else None,
            'kwh': round(kwh, 2) if kwh > 0 else (round(kwh_vi, 2) if kwh_vi > 0 else None),
            'kwh_pack': round(kwh_vi, 2) if kwh_vi > 0 else None,
            'type': 'DC' if peak >= DC_MIN_KW else 'AC',
            'odometer': _odo_at(op, ts[a]),
        })
    return out


def _moved_between(op, t0, t1):
    if not op or t1 - t0 < 120:
        return False
    ot = op['t']
    i, j = bisect_left(ot, t0), bisect_right(ot, t1) - 1
    if i >= len(ot) or j < 0 or i > j:
        return False
    a, b = _first_valid(op['odo'], i, j), _last_valid(op['odo'], i, j)
    return not (_isnan(a) or _isnan(b)) and b - a >= 0.2


def _odo_at(op, t):
    """The odometer at (just before) ``t``; just after when nothing came before."""
    if not op:
        return None
    ot = op['t']
    j = bisect_right(ot, t) - 1
    i = bisect_left(ot, t - 3600)
    if j >= i and j >= 0:
        v = _last_valid(op['odo'], i, j)
        if not _isnan(v):
            return int(round(v))
    i2, j2 = bisect_left(ot, t), bisect_right(ot, t + 600) - 1
    if i2 <= j2 < len(ot):
        v = _first_valid(op['odo'], i2, j2)
        if not _isnan(v):
            return int(round(v))
    return None


def derive_trips(op, pw):
    ts, spd, odo, gear = op['t'], op['spd'], op['odo'], op['gear']
    n = len(ts)
    # awake segments
    segs = []
    s = 0
    for i in range(1, n):
        if ts[i] - ts[i - 1] > GAP_AWAKE_S:
            segs.append((s, i - 1))
            s = i
    if n:
        segs.append((s, n - 1))
    raw = []
    for a, b in segs:
        t0 = t1 = -1
        for i in range(a, b + 1):
            g, v = gear[i], spd[i]
            driving = ((not _isnan(g) and g in (GEAR_D, GEAR_N, GEAR_R))
                       or (not _isnan(v) and v > 0))
            if driving:
                if t0 == -1:
                    t0 = i
                t1 = i
            elif t0 != -1 and ts[i] - ts[t1] >= TRIP_STOP_S:
                raw.append((t0, t1))
                t0 = t1 = -1
        if t0 != -1:
            raw.append((t0, t1))
    trips = []
    pt = pw['t'] if pw else None
    for a, b in raw:
        dur = ts[b] - ts[a]
        if dur < TRIP_MIN_S:
            continue
        o0, o1 = _first_valid(odo, a, b), _last_valid(odo, a, b)
        if _isnan(o0) or _isnan(o1) or o1 - o0 < TRIP_MIN_KM:
            continue
        dist = o1 - o0
        moving = 0
        vmax = 0.0
        for i in range(a + 1, b + 1):
            v = spd[i]
            if not _isnan(v):
                vmax = max(vmax, v)
                dt = ts[i] - ts[i - 1]
                if v > 0 and 0 < dt <= 10:
                    moving += dt
        t = {
            'start': ts[a], 'end': ts[b], 'duration': dur,
            'moving_s': moving, 'distance_km': round(dist, 1),
            'odo_start': int(round(o0)), 'odo_end': int(round(o1)),
            'max_speed': int(round(vmax)),
            'avg_speed': round(dist / moving * 3600.0, 1) if moving > 0 else None,
            'soc_from': None, 'soc_to': None,
            'consumption': None, 'regen_per_100': None, 'kwh': None,
        }
        if pw:
            i = bisect_left(pt, ts[a])
            j = bisect_right(pt, ts[b]) - 1
            if i <= j and j < len(pt):
                sf, st = _first_valid(pw['soc'], i, j), _last_valid(pw['soc'], i, j)
                t['soc_from'] = None if _isnan(sf) else min(100, int(round(sf)))
                t['soc_to'] = None if _isnan(st) else min(100, int(round(st)))
                p = pw['pwr']
                used = _integrate(pt, p, i, j, positive=True)
                regen = _integrate(pt, p, i, j, positive=False)
                if used > 0 or regen > 0:
                    t['kwh'] = round(used - regen, 2)
                    if dist >= 1:
                        t['consumption'] = round((used - regen) / dist * 100, 1)
                        t['regen_per_100'] = round(regen / dist * 100, 1)
        trips.append(t)
    return trips


def analyze(sources, progress_cb=None):
    """Read the export and derive trips and charging sessions.

    ``progress_cb(stage, fraction, rows)`` is called now and then. Returns a
    dict with ``charges``, ``trips``, ``vin``, ``model``, ``window``,
    ``exports`` and ``rows``. Raises ``DataActError`` when the files are not
    a recognisable export or hold nothing usable."""
    streams, exports, unknown = plan_sources(sources)
    if 'power' not in streams and 'operation' not in streams:
        raise DataActError('no_signal_streams')
    total = sum(x[1].size for lst in streams.values() for x in lst)
    prog = Progress(progress_cb, total)
    vin = model = ''
    pw = op = None

    if 'power' in streams:
        t, soc, kw, volt, cur = (array('q'), array('d'), array('d'),
                                 array('d'), array('d'))
        want = ('ldcu_bms_soc_disp', 'ldcu_chrgpwr', 'bms_battvolt',
                'bms_battcurr')
        for part, src, delim, cols in streams['power']:
            if not vin:
                vin, model = _meta(src, delim, cols)
            for ts_, v in _iter_rows(src, delim, cols, want, prog):
                t.append(ts_)
                for arr, c in ((soc, 'ldcu_bms_soc_disp'), (kw, 'ldcu_chrgpwr'),
                               (volt, 'bms_battvolt'), (cur, 'bms_battcurr')):
                    x = v.get(c)
                    arr.append(NAN if x is None else x)
            prog.tick(src.size, force=True)
        t, (soc, kw, volt, cur) = _sorted_unique(t, soc, kw, volt, cur)
        pwr = array('d', (NAN if (_isnan(volt[i]) or _isnan(cur[i]))
                          else volt[i] * cur[i] / 1000.0 for i in range(len(t))))
        pw = {'t': t, 'soc': soc, 'kw': kw, 'volt': volt, 'cur': cur, 'pwr': pwr}

    if 'operation' in streams:
        t, spd, odo, gear = array('q'), array('d'), array('d'), array('d')
        want = ('esp_vehspd', 'cdcu_totalodometer', 'ldcu_currentgearlev')
        for part, src, delim, cols in streams['operation']:
            if not vin:
                vin, model = _meta(src, delim, cols)
            for ts_, v in _iter_rows(src, delim, cols, want, prog):
                t.append(ts_)
                for arr, c in ((spd, 'esp_vehspd'), (odo, 'cdcu_totalodometer'),
                               (gear, 'ldcu_currentgearlev')):
                    x = v.get(c)
                    arr.append(NAN if x is None else x)
            prog.tick(src.size, force=True)
        t, (spd, odo, gear) = _sorted_unique(t, spd, odo, gear)
        op = {'t': t, 'spd': spd, 'odo': odo, 'gear': gear}

    if 'status' in streams:
        prog.tick(sum(x[1].size for x in streams['status']), force=True)

    prog.stage = 'derive'
    prog.tick(force=True)
    rows = len(pw['t']) if pw else 0
    rows += len(op['t']) if op else 0
    if rows == 0:
        raise DataActError('empty')
    charges = derive_charges(pw, op) if pw else []
    trips = derive_trips(op, pw) if op else []
    times = []
    for d in (pw, op):
        if d and len(d['t']):
            times += [d['t'][0], d['t'][-1]]
    window = (min(times), max(times)) if times else (0, 0)
    prog.stage = 'done'
    prog.tick(force=True)
    return {
        'vin': vin, 'model': model, 'exports': exports,
        'unknown_files': unknown, 'rows': rows, 'window': window,
        'streams': sorted(streams),
        'charges': charges, 'trips': trips,
        'has_charge_signal': bool(pw),
        'has_trip_signal': bool(op),
    }
