"""Battery analysis — what the logged history can honestly say about the pack.

Three questions, three sources, kept apart on purpose:

1. **Does the displayed 0–100 % still hold the same energy?** — from the
   charges whose energy was actually METERED (wallbox meter, charging receipt).
   Energy at the wall per displayed SoC percent, per charge type (AC / DC),
   compared half-year against the SAME half-year of an earlier year (winter
   charging loses more than summer charging; comparing January with July
   measures the weather, not the battery).

2. **What does the battery-management system itself say?** — from OBD reads:
   its SoH register, its own SoC next to the displayed one, and its cumulative
   Ah counters. Two reads far apart in SoC give a capacity in Ah that needs no
   assumption about charging losses at all.

3. **What do the single cells look like?** — per-cell voltages from an OBD
   read, as deviation from the pack mean, cell by cell.

🔴 WHY THE OLD "MEASURED SoH" WAS DROPPED (v3.0.154)

``Charge.calculate_fields`` derives the charging loss as
``kwh_loaded - soc_charged/100 * battery_kwh``. The certificate then took
``(kwh_loaded - loss) / window`` as the measured capacity — which is
``battery_kwh`` again, for every single charge, by construction. On a real
car with 335 metered charges over four years it returned exactly 0.6400
kWh/% in every year: the configuration, not the battery. Capacity and loss
cannot be separated from wall energy alone. What wall energy CAN show is
whether the per-percent energy CHANGES over time, at comparable conditions —
the loss of the same wallbox in the same season does not change much, the
battery does. That is what ``window_trend`` measures.

Everything here is pure (no database, no Flask) so it can be tested with
plain lists and reused by the certificate and the web page alike.
"""
from __future__ import annotations

import math
from datetime import datetime
from statistics import median, pstdev

# ── Cell chemistries ──────────────────────────────────────────────────
# key → (label, nominal cell voltage, maximum charge voltage). The nominal
# voltage turns an Ah count into kWh when no pack voltage is known; the
# maximum is printed next to a full-charge read, so the reader sees how much
# head-room the top of the window still has.
CHEMISTRIES = {
    'nmc111': ('NMC 111', 3.65, 4.20),
    'nmc532': ('NMC 532', 3.65, 4.20),
    'nmc622': ('NMC 622', 3.63, 4.20),
    'nmc712': ('NMC 712', 3.65, 4.20),
    'nmc811': ('NMC 811', 3.65, 4.20),
    'nca':    ('NCA',     3.60, 4.20),
    'lfp':    ('LFP',     3.20, 3.65),
    'lmfp':   ('LMFP',    3.70, 4.30),
    'other':  ('—',       None, None),
}

# A wall-energy comparison needs this many charges on BOTH sides.
MIN_PERIOD_N = 8
# And a charge this wide a SoC window: ±1 % SoC quantisation on a 5 % window
# is ±20 %, on 25 % it is ±4 %.
MIN_WINDOW = 25
# Two OBD reads at least this far apart in BMS SoC make a capacity reading.
MIN_OBD_DSOC = 25
# ... and not further apart in time than this (counters and SoC drift).
MAX_OBD_GAP_DAYS = 7


def chemistry(key):
    """(key, label, nominal V, max V) for a stored chemistry key — tolerant
    of None and of unknown keys (shown as given, no voltages)."""
    k = (key or '').strip().lower()
    if k in CHEMISTRIES:
        lab, nom, mx = CHEMISTRIES[k]
        return k, lab, nom, mx
    return k, (key or ''), None, None


# ── 1. The displayed window, from metered charges ─────────────────────
def is_metered(charge, battery_kwh_values=(), efficiencies=()):
    """Was the energy of this charge MEASURED, or estimated by the app?

    Estimated are the auto-detected charges that nobody has measured: the app
    filled their kWh from SoC delta x capacity / efficiency, so using them
    would measure the configuration once more. A charge filed against a
    wallbox meter reading counts as measured even though it was detected.

    🔴 As a second wall, a value that equals capacity/efficiency to the
    second decimal is treated as estimated whatever its note says — an old
    estimate that lost its marker must not pass for a measurement.
    """
    if getattr(charge, 'wallbox_charge_id', None):
        return True
    note = (getattr(charge, 'notes', None) or '').strip()
    if note.startswith('Automatisch erkannt') or getattr(charge, 'needs_review', False):
        return False
    window = (charge.soc_to or 0) - (charge.soc_from or 0)
    if window > 0 and charge.kwh_loaded:
        per_pct = charge.kwh_loaded / window
        for cap in battery_kwh_values:
            for eta in efficiencies:
                if cap and eta and abs(per_pct - cap / 100.0 / eta) < 0.0006:
                    return False
    return True


def _half_year(d):
    if isinstance(d, str):
        d = datetime.fromisoformat(d[:19])
    return '%dH%d' % (d.year, 1 if d.month <= 6 else 2)


def window_samples(charges, battery_kwh_values=(), efficiencies=()):
    """[(period, type, kWh per 100 % displayed)] from metered wide charges."""
    raus = []
    for c in charges:
        if c.soc_from is None or c.soc_to is None or not c.kwh_loaded:
            continue
        window = c.soc_to - c.soc_from
        if window < MIN_WINDOW or c.kwh_loaded <= 0:
            continue
        typ = (c.charge_type or '').upper()
        if typ not in ('AC', 'DC'):
            continue                      # PV runs through the AC charger but at
        if not is_metered(c, battery_kwh_values, efficiencies):  # odd powers
            continue
        raus.append((_half_year(c.date), typ, c.kwh_loaded / window * 100.0))
    return raus


def _quartiles(values):
    v = sorted(values)
    n = len(v)
    return v[n // 4], median(v), v[(3 * n) // 4]


def window_trend(samples, min_n=MIN_PERIOD_N):
    """Energy per displayed 100 %, per type and half-year, and its change.

    Returns ``None`` when no comparison is possible, else a dict with
    ``periods`` (every type/half-year with its quartiles), ``comparisons``
    (each season compared with the same season of the earliest year that has
    enough charges) and ``change_pct`` — the comparisons combined, weighted by
    the smaller side's charge count. ``agreement_pct`` is the spread between
    the comparisons: small means AC and DC, summer and winter all tell the
    same story.
    """
    groups = {}
    for period, typ, val in samples:
        groups.setdefault((typ, period), []).append(val)
    periods = []
    for (typ, period), vals in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        q1, med, q3 = _quartiles(vals)
        periods.append({'type': typ, 'period': period, 'n': len(vals),
                        'median': round(med, 2), 'q1': round(q1, 2), 'q3': round(q3, 2)})
    comparisons = []
    for typ in ('AC', 'DC'):
        for half in ('H1', 'H2'):
            usable = [p for p in periods
                      if p['type'] == typ and p['period'].endswith(half) and p['n'] >= min_n]
            if len(usable) < 2:
                continue
            ref, last = usable[0], usable[-1]
            comparisons.append({
                'type': typ, 'half': half,
                'ref_period': ref['period'], 'ref_median': ref['median'], 'ref_n': ref['n'],
                'last_period': last['period'], 'last_median': last['median'], 'last_n': last['n'],
                'change_pct': round((last['median'] / ref['median'] - 1) * 100.0, 2),
            })
    if not comparisons:
        return {'periods': periods, 'comparisons': [], 'change_pct': None,
                'agreement_pct': None, 'sample_count': len(samples)}
    weights = [min(c['ref_n'], c['last_n']) for c in comparisons]
    change = sum(c['change_pct'] * w for c, w in zip(comparisons, weights)) / sum(weights)
    spread = (max(c['change_pct'] for c in comparisons)
              - min(c['change_pct'] for c in comparisons))
    return {'periods': periods, 'comparisons': comparisons,
            'change_pct': round(change, 2), 'agreement_pct': round(spread, 2),
            'sample_count': len(samples),
            'ref_period': min(c['ref_period'] for c in comparisons),
            'last_period': max(c['last_period'] for c in comparisons)}


# ── 2. Capacity and buffer ────────────────────────────────────────────
def capacity_picture(net_kwh, gross_kwh=None, window_change_pct=None, obd_soh=None):
    """Combine net/gross, the window trend and the BMS into one statement.

    🔑 The displayed 0–100 % is the NET window. If it still holds the same
    energy as in the reference period, the usable capacity is unchanged —
    but that does NOT prove the cells have not aged: a manufacturer may keep
    the displayed window constant by spending the hidden buffer (gross − net).
    So without a BMS figure the honest answer is a RANGE:

      * upper bound — nothing hidden: SoH = usable / net
      * lower bound — the buffer already spent: SoH = usable / gross

    With the BMS SoH register (referenced to the pack as new) the gross
    capacity today is ``gross × SoH``; what is left of the buffer is that
    minus the usable window.
    """
    if not net_kwh:
        return None
    pic = {'net_kwh': net_kwh, 'gross_kwh': gross_kwh or None}
    if gross_kwh and gross_kwh > net_kwh:
        pic['buffer_kwh'] = round(gross_kwh - net_kwh, 2)
        pic['buffer_pct'] = round((gross_kwh - net_kwh) / gross_kwh * 100.0, 2)
    usable = None
    if window_change_pct is not None:
        usable = net_kwh * (1 + window_change_pct / 100.0)
    if usable is not None:
        pic['usable_kwh'] = round(usable, 2)
        pic['soh_high'] = round(min(usable / net_kwh * 100.0, 100.0), 1)
        if gross_kwh and gross_kwh > net_kwh:
            pic['soh_low'] = round(min(usable / gross_kwh * 100.0, 100.0), 1)
    if obd_soh is not None and gross_kwh:
        gross_now = gross_kwh * obd_soh / 100.0
        pic['gross_now_kwh'] = round(gross_now, 2)
        reference = usable if usable is not None else net_kwh
        if pic.get('buffer_kwh'):
            left = gross_now - reference
            pic['buffer_left_kwh'] = round(left, 2)
            pic['buffer_used_pct'] = round(
                max(0.0, min(100.0, (1 - left / pic['buffer_kwh']) * 100.0)), 0)
    return pic


def obd_capacity(readings, nominal_cell_v=None, cells_series=None,
                 min_dsoc=MIN_OBD_DSOC, max_gap_days=MAX_OBD_GAP_DAYS):
    """Capacity from the BMS's own Ah counters between two reads.

    ``readings`` are dicts (timestamp, soc_bms_pct, cumulative_charge_ah,
    cumulative_discharge_ah, pack_voltage_v, and where the BMS has them
    cumulative_charge_kwh / cumulative_discharge_kwh) — any order.
    For every pair at least ``min_dsoc`` BMS-SoC points apart and at most
    ``max_gap_days`` apart: net Ah out of the pack ÷ SoC points × 100.
    No charging loss, no wall meter, no assumption — the BMS counted it.
    kWh comes from the BMS's own energy counters when both reads carry
    them (v3.0.167), else from the mean of the two pack voltages (or
    nominal × cells).

    Reads of the same moment (a snapshot saved right after a read, same
    BMS SoC, within the hour) count once — otherwise one drive shows up as
    four measurements.
    Returns the list of measurements, newest pair last.
    """
    rs = [r for r in readings
          if r.get('soc_bms_pct') is not None
          and r.get('cumulative_charge_ah') is not None
          and r.get('cumulative_discharge_ah') is not None
          and r.get('timestamp')]

    def ts(r):
        t = r['timestamp']
        return datetime.fromisoformat(t[:26]) if isinstance(t, str) else t
    rs.sort(key=ts)
    einmal = []
    for r in rs:
        p = einmal[-1] if einmal else None
        if (p and p['soc_bms_pct'] == r['soc_bms_pct']
                and (ts(r) - ts(p)).total_seconds() <= 3600):
            continue
        einmal.append(r)
    rs = einmal
    raus = []
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            a, b = rs[i], rs[j]
            if (ts(b) - ts(a)).total_seconds() > max_gap_days * 86400:
                break
            dsoc = a['soc_bms_pct'] - b['soc_bms_pct']        # + = discharged
            if abs(dsoc) < min_dsoc:
                continue
            ah_out = ((b['cumulative_discharge_ah'] - a['cumulative_discharge_ah'])
                      - (b['cumulative_charge_ah'] - a['cumulative_charge_ah']))
            if ah_out == 0 or (ah_out > 0) != (dsoc > 0):
                continue                  # counters and SoC disagree on direction
            cap_ah = ah_out / dsoc * 100.0
            kwh_out = None
            if all(x.get(k) is not None for x in (a, b)
                   for k in ('cumulative_charge_kwh', 'cumulative_discharge_kwh')):
                kwh_out = ((b['cumulative_discharge_kwh'] - a['cumulative_discharge_kwh'])
                           - (b['cumulative_charge_kwh'] - a['cumulative_charge_kwh']))
                if kwh_out == 0 or (kwh_out > 0) != (dsoc > 0):
                    kwh_out = None
            volts = [v for v in (a.get('pack_voltage_v'), b.get('pack_voltage_v')) if v]
            if volts:
                pack_v = sum(volts) / len(volts)
            elif nominal_cell_v and cells_series:
                pack_v = nominal_cell_v * cells_series
            else:
                pack_v = None
            if kwh_out is not None:
                cap_kwh = round(kwh_out / dsoc * 100.0, 1)
            else:
                cap_kwh = round(cap_ah * pack_v / 1000.0, 1) if pack_v else None
            raus.append({
                'from': ts(a).isoformat(timespec='minutes'),
                'to': ts(b).isoformat(timespec='minutes'),
                'soc_from': a['soc_bms_pct'], 'soc_to': b['soc_bms_pct'],
                'ah': round(abs(ah_out), 1),
                'kwh': round(abs(kwh_out), 1) if kwh_out is not None else None,
                'capacity_ah': round(cap_ah, 1),
                'capacity_kwh': cap_kwh,
                'kwh_counted': kwh_out is not None,
            })
    return raus


def buffer_view(readings, max_cell_v=None):
    """Displayed SoC next to the BMS's own SoC, per read — the buffer seen
    from the outside. A read at ≥ 97 % displayed also carries the top cell
    voltage: if it climbs over the years, the top of the window is being
    moved; if the BMS-SoC at displayed 100 % climbs, the window is."""
    raus = []
    for r in readings:
        disp, bms = r.get('soc_display_pct'), r.get('soc_bms_pct')
        if disp is None or bms is None:
            continue
        row = {'timestamp': r.get('timestamp'), 'display': disp, 'bms': bms,
               'gap': round(disp - bms, 1), 'top': disp >= 97}
        if row['top'] and r.get('cell_max_v'):
            row['cell_max_v'] = r['cell_max_v']
            if max_cell_v:
                row['headroom_mv'] = round((max_cell_v - r['cell_max_v']) * 1000)
        raus.append(row)
    return raus


# ── 3. The cells ──────────────────────────────────────────────────────
def cell_stats(voltages):
    """Everything worth saying about one set of cell voltages.

    🔴 ``resolution_mv`` matters for reading the result: many BMS report a
    cell in 20 mV steps. Then a healthy pack in the middle of its SoC shows
    every cell identical — not because they are, but because the steps are
    too coarse. The spread only opens up near full and near empty, where the
    voltage curve is steep.
    """
    cells = [float(v) for v in (voltages or []) if isinstance(v, (int, float))]
    if len(cells) < 2:
        return None
    n = len(cells)
    mean = sum(cells) / n
    dev = [(v - mean) * 1000.0 for v in cells]
    lo, hi = min(cells), max(cells)
    distinct = sorted(set(round(v, 4) for v in cells))
    steps = [round((b - a) * 1000.0, 1) for a, b in zip(distinct, distinct[1:])]
    # 🔴 A step can only be SEEN between three or more distinct values: with
    # two (an even pack and one weak cell) the "smallest step" is simply the
    # weak cell's whole deviation, and using it as a threshold would hide
    # exactly that cell. The decoder profile knows the real step; the caller
    # passes it on where it matters.
    resolution = min(steps) if len(distinct) >= 3 else None
    sd = pstdev(cells) * 1000.0
    out_lim = max(2 * sd, resolution or 0.0)
    return {
        'n': n,
        'mean_v': round(mean, 4),
        'median_v': round(median(cells), 4),
        'std_mv': round(sd, 1),
        'min_v': lo, 'max_v': hi,
        'min_cells': [i + 1 for i, v in enumerate(cells) if v == lo],
        'max_cells': [i + 1 for i, v in enumerate(cells) if v == hi],
        'spread_mv': round((hi - lo) * 1000.0, 1),
        'resolution_mv': resolution,
        'deviation_mv': [round(d, 1) for d in dev],
        'outliers': [i + 1 for i, d in enumerate(dev) if out_lim and abs(d) > out_lim],
        'all_equal': len(distinct) == 1,
    }


def grid_columns(n):
    """Columns for drawing ``n`` cells as a block: a divisor between 10 and
    16 when there is one (98 → 14, 96 → 12, 108 → 12), else 14."""
    for c in (14, 12, 16, 10, 13, 11, 15):
        if n % c == 0:
            return c
    return 14 if n > 40 else max(4, int(math.ceil(math.sqrt(n))))
