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

    Returns a dict with
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
                'agreement_pct': None, 'sample_count': len(samples), 'compared_n': 0}
    # 🔴 Only comparisons over the SAME span are combined: −8 % over four
    # years and −0.5 % over one year averaged to "−2 % since 2022" says
    # neither. The longest span wins; the others are listed, not mixed in.
    def span(c):
        return int(c['last_period'][:4]) - int(c['ref_period'][:4])
    longest = max(span(c) for c in comparisons)
    for c in comparisons:
        c['combined'] = span(c) == longest
    used = [c for c in comparisons if c['combined']]
    weights = [min(c['ref_n'], c['last_n']) for c in used]
    change = sum(c['change_pct'] * w for c, w in zip(used, weights)) / sum(weights)
    spread = (max(c['change_pct'] for c in used)
              - min(c['change_pct'] for c in used))
    return {'periods': periods, 'comparisons': comparisons,
            'change_pct': round(change, 2), 'agreement_pct': round(spread, 2),
            'sample_count': len(samples),
            'compared_n': sum(c['ref_n'] + c['last_n'] for c in used),
            'ref_period': min(c['ref_period'] for c in used),
            'last_period': max(c['last_period'] for c in used)}


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
        # The buffer left needs the usable window MEASURED — assuming it is
        # still the full net would print a verdict nobody measured.
        if pic.get('buffer_kwh') and usable is not None:
            left = gross_now - usable
            if left < 0:
                # Less gross today than the window shows as usable: the BMS
                # figure, the window and the configured sizes cannot all be
                # right. Say so instead of printing a negative buffer.
                pic['buffer_conflict'] = True
            else:
                pic['buffer_left_kwh'] = round(left, 2)
                pic['buffer_used_pct'] = round(
                    max(0.0, min(100.0, (1 - left / pic['buffer_kwh']) * 100.0)), 0)
    return pic


def _ts(r):
    t = r['timestamp']
    if isinstance(t, str):
        t = datetime.fromisoformat(t[:19])
    return t.replace(tzinfo=None) if getattr(t, 'tzinfo', None) else t


def counters_plausible(r):
    """Can the four lifetime counters of one read be true?

    🔴 A dropped CAN frame shifts every byte behind it: one missing line
    turned 58 448 Ah into 424 million and printed a capacity of 107 million
    Ah. Lifetime energy ÷ lifetime charge is the pack's mean voltage — it has
    to lie near the voltage the same read shows."""
    ah = (r.get('cumulative_charge_ah'), r.get('cumulative_discharge_ah'))
    if any(v is None or v <= 0 for v in ah):
        return False
    kwh = (r.get('cumulative_charge_kwh'), r.get('cumulative_discharge_kwh'))
    if any(v is None for v in kwh):
        return True                      # no energy counters to cross-check
    if any(v <= 0 for v in kwh):
        return False
    pack_v = r.get('pack_voltage_v')
    for a_, k_ in zip(ah, kwh):
        v = k_ * 1000.0 / a_
        if pack_v:
            if not 0.7 * pack_v <= v <= 1.3 * pack_v:
                return False
        elif not 150 <= v <= 1000:
            return False
    return True


def obd_capacity(readings, nominal_cell_v=None, cells_series=None,
                 min_dsoc=MIN_OBD_DSOC, max_gap_days=MAX_OBD_GAP_DAYS):
    """Capacity from the BMS's own counters over a drive.

    ``readings`` are dicts (timestamp, soc_bms_pct, cumulative_charge_ah,
    cumulative_discharge_ah, pack_voltage_v, and where the BMS has them
    cumulative_charge_kwh / cumulative_discharge_kwh) — any order.

    A measurement is a DRIVE between two reads: BMS SoC fell by at least
    ``min_dsoc`` points within ``max_gap_days``, every counter moved forward,
    and what flowed back in stayed below half of what flowed out — more than
    that is a charge, not regeneration. Net Ah out (out − regeneration) ÷ SoC
    points × 100.

    🔴 Only INDEPENDENT drives count: reads at 95, 60 and 25 % are one drive,
    not three measurements (95→60, 95→25, 60→25). Of overlapping pairs the
    widest is kept. Sorted by end time — the last one is the newest.

    kWh comes from the BMS's own energy counters when both reads carry them,
    else from the nominal pack voltage (cell voltage × cells in series), else
    from the mean of BOTH endpoint voltages — never from one endpoint alone.
    ``kwh_basis`` says which.
    """
    rs = [r for r in readings
          if r.get('soc_bms_pct') is not None and r.get('timestamp')
          and counters_plausible(r)]
    rs.sort(key=_ts)
    einmal = []
    for r in rs:
        p = einmal[-1] if einmal else None
        # The same moment saved twice (a snapshot after a read): within the
        # hour, the SoC within a point, the charge counters unmoved.
        if (p and (_ts(r) - _ts(p)).total_seconds() <= 3600
                and abs(p['soc_bms_pct'] - r['soc_bms_pct']) <= 1.0
                and abs(r['cumulative_discharge_ah'] - p['cumulative_discharge_ah']) < 1.0):
            continue
        einmal.append(r)
    rs = einmal

    def r1(v):
        return round(v + 0.0, 1)

    def d(a, b, k):
        # from the counters AS PRINTED (0.1), so the difference row of the
        # table is exactly end − start of the rows above it
        return r1(round(b[k], 1) - round(a[k], 1))

    kandidaten = []
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            a, b = rs[i], rs[j]
            if (_ts(b) - _ts(a)).total_seconds() > max_gap_days * 86400:
                break
            dsoc = a['soc_bms_pct'] - b['soc_bms_pct']        # + = discharged
            if dsoc < min_dsoc:
                continue
            # 🔴 A read in between that shows the SoC RISING is a charge stop:
            # 95 → 40, charged to 60, → 25 is two drives, not one with
            # 52 Ah of "regeneration".
            if any(rs[q + 1]['soc_bms_pct'] > rs[q]['soc_bms_pct'] + 1.0 for q in range(i, j)):
                continue
            d_out = d(a, b, 'cumulative_discharge_ah')
            d_in = d(a, b, 'cumulative_charge_ah')
            if d_out <= 0 or d_in < 0 or d_in > 0.5 * d_out:
                continue                  # counters ran back, or a charge in between
            net_ah = r1(d_out - d_in)
            if net_ah <= 0:
                continue
            k_out = k_in = None
            if all(x.get(k) is not None for x in (a, b)
                   for k in ('cumulative_charge_kwh', 'cumulative_discharge_kwh')):
                k_out = d(a, b, 'cumulative_discharge_kwh')
                k_in = d(a, b, 'cumulative_charge_kwh')
                if k_out <= 0 or k_in < 0 or k_in >= k_out:
                    k_out = k_in = None
            kandidaten.append((dsoc, a, b, d_out, d_in, net_ah, k_out, k_in))
    # Widest first; a pair joins only if its time span overlaps no kept one.
    kandidaten.sort(key=lambda k: -k[0])
    gewaehlt = []
    for k in kandidaten:
        a0, b0 = _ts(k[1]), _ts(k[2])
        if any(a0 < _ts(g[2]) and _ts(g[1]) < b0 for g in gewaehlt):
            continue
        gewaehlt.append(k)
    gewaehlt.sort(key=lambda k: _ts(k[2]))

    raus = []
    for dsoc, a, b, d_out, d_in, net_ah, k_out, k_in in gewaehlt:
        dsoc = round(dsoc, 1)
        cap_ah = net_ah / dsoc * 100.0
        net_kwh = r1(k_out - k_in) if k_out is not None else None
        if net_kwh is not None:
            cap_kwh, basis = round(net_kwh / dsoc * 100.0, 1), 'counters'
        elif nominal_cell_v and cells_series:
            cap_kwh, basis = round(cap_ah * nominal_cell_v * cells_series / 1000.0, 1), 'nominal'
        elif a.get('pack_voltage_v') and b.get('pack_voltage_v'):
            pack_v = (a['pack_voltage_v'] + b['pack_voltage_v']) / 2.0
            cap_kwh, basis = round(cap_ah * pack_v / 1000.0, 1), 'endpoints'
        else:
            cap_kwh, basis = None, None
        raus.append({
            'from': _ts(a).isoformat(timespec='minutes'),
            'to': _ts(b).isoformat(timespec='minutes'),
            'soc_from': a['soc_bms_pct'], 'soc_to': b['soc_bms_pct'], 'dsoc': dsoc,
            'ah_out': d_out, 'ah_in': d_in, 'ah': net_ah,
            'kwh_out': k_out, 'kwh_in': k_in, 'kwh': net_kwh,
            'capacity_ah': round(cap_ah, 1),
            'capacity_kwh': cap_kwh,
            'kwh_counted': basis == 'counters',
            'kwh_basis': basis,
            # The two reads themselves, for the measurement table.
            'start': _ends(a), 'end': _ends(b),
        })
    return raus


def _ends(r):
    return {'timestamp': r.get('timestamp'), 'soc_bms': r.get('soc_bms_pct'),
            'soc_display': r.get('soc_display_pct'), 'pack_v': r.get('pack_voltage_v'),
            'ah_in': r.get('cumulative_charge_ah'), 'ah_out': r.get('cumulative_discharge_ah'),
            'kwh_in': r.get('cumulative_charge_kwh'), 'kwh_out': r.get('cumulative_discharge_kwh')}


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
def cell_stats(voltages, resolution_mv=None):
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
    # 🔴 A cell one BMS step off is the step, not a weak cell: on 98 cells at
    # 3.80 V with eight at 3.82 V, 2 σ is 11 mV and all eight were "outliers".
    # The limit is never below one and a half steps, and deviations are
    # compared rounded — 20.000000000000018 mV is one step, not more.
    step = resolution_mv or resolution or 0.0
    out_lim = max(2 * sd, 1.5 * step)
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
        'outliers': [i + 1 for i, d in enumerate(dev) if out_lim and round(abs(d), 1) > out_lim],
        'all_equal': len(distinct) == 1,
    }


# ── 4. A capacity ESTIMATE from wallbox energy, for a BMS that says nothing ──
# Some packs answer neither a SoH register nor Ah/kWh counters (the 2026 LFP
# XPENG G6 is one). What is left is the energy the home wallbox measured and
# the displayed SoC before and after:
#
#     capacity ≈ E_wallbox × (1 − charging loss) ÷ ΔSoC
#
# 🔴 THE LOSS MUST NOT COME FROM THE APP. ``Charge.calculate_fields`` writes
# ``loss = kwh_loaded − ΔSoC × battery_kwh`` — the configured capacity — and
# the self-learning efficiency (``_get_charge_efficiency``) is the median of
# exactly those losses. Fed back in, it returns the configured capacity for
# every charge (the v3.0.154 lesson, see the module docstring). So the loss
# is a stated ASSUMPTION with a band, the same 12 % the app falls back to
# before it has learned anything (efficiency 0.88), ±4 points: typical AC
# home charging at wallbox power. The band is carried into the result.
#
# Thresholds:
#   * ΔSoC ≥ 30 points: the displayed SoC is a whole number, so ΔSoC is good
#     to about ±1 point — ±3.3 % of a 30-point window, ±10 % of a 10-point one.
#   * at least 5 such charges, else nothing is stated ("too few").
#   * the wallbox reading must cover the charge (≥ 95 % of the window) and,
#     where its mean power is known, run at ≥ 3 kW — slow charging loses
#     clearly more than the assumed band.
EST_MIN_DSOC = 30
EST_MIN_N = 5
EST_MIN_COVERAGE = 0.95
EST_MIN_POWER_W = 3000.0
EST_LOSS = 0.12
EST_LOSS_BAND = (0.08, 0.16)
EST_RECENT_DAYS = 365
# A per-charge figure outside this share of the pack is a data error (a
# reading of the wrong car, a SoC that did not update), not a battery.
EST_SAMPLE_BAND = (0.40, 1.30)
# A median above the pack as new (gross, else net) by more than this is not
# ageing that can be graded — the assumption or the SoC does not hold.
EST_ABOVE_NEW = 1.05


def _as_date(d):
    if isinstance(d, str):
        return datetime.fromisoformat(d[:19])
    if d is not None and not isinstance(d, datetime):
        return datetime(d.year, d.month, d.day)
    return d


def wallbox_capacity_estimate(rows, net_kwh, gross_kwh=None, floor_pct=70.0,
                              loss=EST_LOSS, loss_band=EST_LOSS_BAND,
                              min_n=EST_MIN_N, min_dsoc=EST_MIN_DSOC):
    """Usable capacity ESTIMATED from wallbox energy against the SoC lift.

    ``rows`` are dicts, one per logged charge: ``date``, ``soc_from``,
    ``soc_to``, ``energy_kwh`` (what the wallbox meter measured, None when
    nobody measured), ``coverage`` (0..1 of the charge the meter saw, None =
    unknown), ``avg_power_w`` (None = unknown), ``charge_type``.

    Returns None without a net capacity, else a dict whose ``state`` is
    ``'too_few'`` (fewer than ``min_n`` usable charges — no figure at all),
    ``'implausible'`` (the median lies above the pack as new or below 40 %
    — no SoH) or ``'ok'``. With ``'ok'``:

    * ``capacity_kwh`` — median of the per-charge capacities at the assumed
      loss; ``capacity_lo_kwh``/``capacity_hi_kwh`` — lower quartile at the
      HIGH end of the loss band, upper quartile at the LOW end: scatter of
      the charges and the loss assumption in one range.
    * ``soh_pct`` (≤ 100), ``soh_lo_pct``, ``soh_hi_pct`` — whole percent of
      NET; a decimal would claim a precision this method does not have.
    * ``verdict`` against ``floor_pct``: ``'pass'`` only when the whole range
      is at or above it, ``'fail'`` when the whole range is below it, else
      ``'undecided'`` — an estimate whose range crosses the line passes
      nothing. Judged on the whole percent as printed.

    Never a measured SoH: ``is_estimate`` is always True.
    """
    if not net_kwh or net_kwh <= 0:
        return None
    skipped = {'not_measured': 0, 'small_window': 0, 'partial': 0,
               'low_power': 0, 'dc': 0, 'outlier': 0}
    lo_ok = EST_SAMPLE_BAND[0] * net_kwh
    hi_ok = EST_SAMPLE_BAND[1] * (gross_kwh or net_kwh)
    samples = []
    for r in rows:
        e = r.get('energy_kwh')
        if e is None or e <= 0:
            skipped['not_measured'] += 1
            continue
        if (r.get('charge_type') or '').upper() == 'DC':
            skipped['dc'] += 1
            continue
        sf, st = r.get('soc_from'), r.get('soc_to')
        if sf is None or st is None or not (0 <= sf < st <= 100) or st - sf < min_dsoc:
            skipped['small_window'] += 1
            continue
        cov = r.get('coverage')
        if cov is not None and cov < EST_MIN_COVERAGE:
            skipped['partial'] += 1
            continue
        pw = r.get('avg_power_w')
        if pw is not None and pw < EST_MIN_POWER_W:
            skipped['low_power'] += 1
            continue
        dsoc = st - sf
        wall100 = e / dsoc * 100.0
        cap = wall100 * (1 - loss)
        if not lo_ok <= cap <= hi_ok:
            skipped['outlier'] += 1
            continue
        samples.append({'date': _as_date(r.get('date')), 'soc_from': sf, 'soc_to': st,
                        'dsoc': dsoc, 'energy_kwh': round(e, 2),
                        'wall100_kwh': round(wall100, 2), 'capacity_kwh': round(cap, 1)})
    samples.sort(key=lambda s: s['date'] or datetime.min)
    out = {
        'is_estimate': True,
        'state': 'too_few',
        'n_used': 0,
        'n_qualifying': len(samples),
        'n_rows': len(rows),
        'skipped': skipped,
        'min_n': min_n,
        'min_dsoc': min_dsoc,
        'loss_pct': round(loss * 100.0, 1),
        'loss_lo_pct': round(loss_band[0] * 100.0, 1),
        'loss_hi_pct': round(loss_band[1] * 100.0, 1),
        'floor_pct': floor_pct,
        'net_kwh': net_kwh,
        'samples': [],
    }
    # The recent state, like the measured SoH: the last year before the
    # newest usable charge, when that alone holds enough.
    basis = samples
    if samples and samples[-1]['date']:
        newest = samples[-1]['date']
        recent = [s for s in samples if s['date'] and (newest - s['date']).days <= EST_RECENT_DAYS]
        if len(recent) >= min_n:
            basis = recent
    out['samples'] = [dict(s, date=s['date'].date().isoformat() if s['date'] else None)
                      for s in basis]
    if len(basis) < min_n:
        return out
    q1, med, q3 = _quartiles([s['wall100_kwh'] for s in basis])
    cap = med * (1 - loss)
    cap_lo = q1 * (1 - loss_band[1])
    cap_hi = q3 * (1 - loss_band[0])
    out.update({
        'n_used': len(basis),
        'recent_only': len(basis) < len(samples),
        'basis_from': out['samples'][0]['date'],
        'basis_to': out['samples'][-1]['date'],
        'capacity_kwh': round(cap, 1),
        'capacity_lo_kwh': round(cap_lo, 1),
        'capacity_hi_kwh': round(cap_hi, 1),
        'uncertainty_pct': round((cap_hi - cap_lo) / 2.0 / cap * 100.0, 1),
    })
    if cap > EST_ABOVE_NEW * (gross_kwh or net_kwh) or cap < lo_ok:
        out['state'] = 'implausible'
        return out
    soh = int(round(cap / net_kwh * 100.0))
    soh_lo = int(round(cap_lo / net_kwh * 100.0))
    soh_hi = int(round(cap_hi / net_kwh * 100.0))
    out.update({
        'state': 'ok',
        'soh_pct': float(min(soh, 100)),
        'soh_lo_pct': float(min(soh_lo, 100)),
        'soh_hi_pct': float(soh_hi),
        'verdict': ('pass' if soh_lo >= floor_pct
                    else 'fail' if soh_hi < floor_pct else 'undecided'),
    })
    return out


def grid_columns(n):
    """Columns for drawing ``n`` cells as a block: a divisor between 10 and
    16 when there is one (98 → 14, 96 → 12, 108 → 12), else 14."""
    for c in (14, 12, 16, 10, 13, 11, 15):
        if n % c == 0:
            return c
    return 14 if n > 40 else max(4, int(math.ceil(math.sqrt(n))))
