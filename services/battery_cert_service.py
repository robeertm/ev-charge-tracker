"""Battery-Health Certificate service (self-generated).

A self-created battery-health certificate the owner can hand a
buyer when he sells / archives a vehicle — comparable to an independent
professional battery test, but derived from the *own* charge/sync history
this app has been logging for months.

## How professional battery tests determine SoH (and how we mirror it)

Independent battery testers typically offer two kinds of test:

* **Quick/flash test** — a ~3-minute OBD snapshot. Reads the BMS' own SoH
  plus per-cell voltages and derives a health grade from cell-voltage
  spread and the manufacturer capacity estimate.
* **Full-discharge test** — a real discharge run from ~100 % down to
  ~10 % with a data logger. The delivered energy over that window is
  integrated (coulomb / energy counting) to *measure* the usable capacity
  independent of the BMS, then compared against the nominal pack size:

      SoH = usable_capacity_now / nominal_capacity_when_new × 100 %

We can't force a controlled discharge, but every logged charge session
is effectively a *partial* capacity measurement: energy that actually
entered the battery divided by the SoC window it filled, extrapolated to
100 %. Averaged (median) over many sessions the noise cancels out and we
get a measured usable-capacity estimate — the same quantity a
full-discharge lab test reports, just reconstructed from historical data
instead of one lab discharge.

The certificate reports BOTH signals when available:

* **BMS-SoH** — what the car's own battery-management system claims
  (from ``VehicleSync.battery_soh_percent``, baseline-scaled). This is
  the quick/flash-test equivalent.
* **Gemessene SoH** — our coulomb-counted capacity ÷ nominal. This is the
  full-discharge-test equivalent and the headline number when we have
  enough qualifying sessions.

A battery-*health* certificate is only meaningful when it can state an
actual SoH. So the certificate is refused outright when NONE of the three
SoH sources yields a value — i.e. the car reports no BMS SoH over the
cloud API (some models report none at all), no OBD/ELM327 read is on file,
AND there aren't
enough wide-window charge sessions to coulomb-count one. In that case
``compute_battery_health`` sets ``can_certify = False`` and
``generate_certificate`` returns ``None`` instead of printing an "n/a"
grade; the UI then tells the owner to log more (near-full) charges or read
the battery with an OBD dongle. Charge/drive data alone ARE sufficient the
moment they produce a measured SoH — that is exactly the "unless" case.

Once at least one SoH signal exists, everything else still degrades
gracefully: missing secondary signals come back as ``None`` and the
certificate prints throughput, cycle-equivalents and charging-behaviour
stress factors alongside the SoH it could determine.
"""
import unicodedata
import os
import tempfile
from datetime import date, datetime
from statistics import median

from models.database import Charge, Vehicle, ObdReading
from services import battery_analysis as BA
from services.stats_service import (
    get_soh_baseline, get_summary_stats, get_vehicle_history,
    get_ac_dc_stats, _vehicle_field,
)
from services.i18n import t

# ── Tunables ──────────────────────────────────────────────────────────
# Minimum SoC window a charge must fill to count as a usable-capacity
# sample. Small windows amplify the ±1 % SoC quantisation error into a
# huge capacity error, so we ignore them (a 5 % window at ±1 % is ±20 %;
# a 30 % window at ±1 % is only ±3 %).
MIN_SOC_WINDOW = 25
# Reject implausible per-session capacities (sensor glitch, wrong battery
# size, partial/aborted session logged wrong). Keep only 40 %–115 % of
# nominal — a real pack can't measure above ~new and below 40 % it's a
# data error, not degradation.
CAP_LOW_FRAC = 0.40
CAP_HIGH_FRAC = 1.15
# Industry-average calendar+cycle degradation used only for the "better /
# worse than average" benchmark line. ~2.3 %/yr is the widely-cited fleet
# mean (Geotab 2019, ~6300 EVs). Purely informational.
AVG_DEGRADATION_PCT_PER_YEAR = 2.3

# Grade scale (SoH % → letter, label-key, hex colour). Aligned with the
# industry-common A–F resale grading (professional testers often publish
# only a SoH % and a 3-band above/average/below scale, not letters). The 70 % line is
# the typical OEM warranty floor (8 yr / 160 000 km ≥ 70 % SoH).
_GRADE_TABLE = [
    (90, 'A', 'cert.grade_a', '#2e7d32'),
    (85, 'B', 'cert.grade_b', '#558b2f'),
    (80, 'C', 'cert.grade_c', '#9e9d24'),
    (70, 'D', 'cert.grade_d', '#f9a825'),
    (60, 'E', 'cert.grade_e', '#ef6c00'),
    (0,  'F', 'cert.grade_f', '#c62828'),
]
# OEM warranty floor most manufacturers guarantee (SoH ≥ this at 8yr/160k).
WARRANTY_FLOOR_SOH = 70.0


def grade_for_soh(soh):
    """Return (letter, label_key, hex_color) for a SoH percentage."""
    if soh is None:
        return ('?', 'cert.grade_unknown', '#9e9e9e')
    for threshold, letter, label_key, color in _GRADE_TABLE:
        if soh >= threshold:
            return (letter, label_key, color)
    return _GRADE_TABLE[-1][1:]


def _measured_capacity(vehicle_id, nominal_kwh):
    """Coulomb-count usable capacity from wide-window charge sessions.

    For every charge that filled at least ``MIN_SOC_WINDOW`` SoC points we
    take the energy that actually entered the battery (gross kWh minus the
    logged charging loss) and extrapolate to a full 0→100 % pack:

        session_capacity = net_kwh / (soc_window / 100)

    Returns a dict with the median capacity (robust to outliers), the
    sample count, and the samples split early-vs-recent so the certificate
    can show a degradation trend. Returns ``None`` when nothing qualifies.
    """
    if not nominal_kwh or nominal_kwh <= 0:
        return None
    q = (Charge.query
         .filter(Charge.soc_from.isnot(None),
                 Charge.soc_to.isnot(None),
                 Charge.kwh_loaded.isnot(None))
         .order_by(Charge.date.asc()))
    if vehicle_id is not None:
        q = q.filter(Charge.vehicle_id == vehicle_id)

    lo = CAP_LOW_FRAC * nominal_kwh
    hi = CAP_HIGH_FRAC * nominal_kwh
    samples = []  # (date, capacity_kwh)
    gross_per_window = []
    for c in q.all():
        window = (c.soc_to or 0) - (c.soc_from or 0)
        if window < MIN_SOC_WINDOW:
            continue
        gross = c.kwh_loaded or 0
        if gross <= 0:
            continue
        # Energy actually stored = gross from the wall minus charging loss.
        # loss_kwh is None on legacy rows → treat as 0 (slightly optimistic
        # for AC, negligible for DC where the loss is small anyway).
        net = gross - (c.loss_kwh or 0)
        if net <= 0:
            continue
        cap = net / (window / 100.0)
        if cap < lo or cap > hi:
            continue
        samples.append((c.date, round(cap, 2)))
        gross_per_window.append(gross / window)

    if not samples:
        return None

    caps = [s[1] for s in samples]
    med = median(caps)

    # 🔴 THE MEASUREMENT THAT MEASURED THE SETTING (v3.0.154)
    # ``Charge.calculate_fields`` fills a missing loss as
    # ``kwh_loaded - soc_charged/100 * battery_kwh`` — so ``net / window`` is
    # the configured capacity again, charge after charge. On a real car with
    # four years of metered charges every single sample read 64.00 kWh while
    # the energy per SoC point at the wall ranged over 15 %. A capacity that
    # stays constant to the third digit while its raw input scatters is not
    # a measurement; it is refused here and the certificate says why.
    # Measured on that car: 284 of 323 samples were 64.00 to the cent, the
    # rest came from a handful of hand-entered losses — so the test is "most
    # samples share ONE value", not "all of them".
    if len(samples) >= 3:
        from collections import Counter
        gross_pw = [g for g in gross_per_window if g > 0]
        dominant, count = Counter(round(c, 2) for c in caps).most_common(1)[0]
        raw_spread = (max(gross_pw) - min(gross_pw)) / median(gross_pw) if gross_pw else 0
        if count / len(caps) > 0.5 and raw_spread > 0.02:
            return {'circular': True, 'capacity_kwh': dominant,
                    'sample_count': len(samples)}

    # SoH is capped at 100 %: a *measured* usable capacity above nominal is a
    # measurement artefact (charging-loss under-counting on legacy rows, ±1 %
    # SoC quantisation), not a battery that is healthier than new. Without the
    # cap an optimistic median reads e.g. "109 %", which is physically
    # meaningless and inflates the grade. Capacity is still shown as measured.
    def _soh(cap_kwh):
        return round(min(cap_kwh / nominal_kwh * 100, 100.0), 1)

    # Trend: compare the earliest third to the most-recent third when we
    # have enough samples to make the split meaningful.
    trend = None
    if len(samples) >= 6:
        third = max(1, len(samples) // 3)
        early = median([s[1] for s in samples[:third]])
        recent = median([s[1] for s in samples[-third:]])
        trend = {
            'early_capacity_kwh': round(early, 2),
            'recent_capacity_kwh': round(recent, 2),
            'early_soh': _soh(early),
            'recent_soh': _soh(recent),
            'from': samples[0][0].isoformat() if samples[0][0] else None,
            'to': samples[-1][0].isoformat() if samples[-1][0] else None,
        }
    return {
        'capacity_kwh': round(med, 2),
        'soh_pct': _soh(med),
        'sample_count': len(samples),
        'min_capacity_kwh': round(min(caps), 2),
        'max_capacity_kwh': round(max(caps), 2),
        'trend': trend,
        'window_pct': MIN_SOC_WINDOW,
    }


def _latest_obd(vehicle_id):
    """Return the most recent OBD/ELM327 cell snapshot for the vehicle as a
    plain dict, or ``None``. This is the Flash-Test-grade data the pure
    charge-history certificate can't see: real BMS SoH, cell-voltage spread
    and pack temperatures read straight from the battery-management system.
    """
    import json as _json
    q = ObdReading.query.order_by(ObdReading.timestamp.desc())
    if vehicle_id is not None:
        q = q.filter(ObdReading.vehicle_id == vehicle_id)
    row = q.first()
    if row is None:
        return None

    def _arr(blob):
        try:
            return _json.loads(blob) if blob else None
        except (ValueError, TypeError):
            return None

    return {
        'timestamp': row.timestamp.isoformat() if row.timestamp else None,
        'soh_pct': row.soh_pct,
        'soc_bms_pct': row.soc_bms_pct,
        'pack_voltage_v': row.pack_voltage_v,
        'pack_current_a': row.pack_current_a,
        'aux_battery_v': row.aux_battery_v,
        'cell_count': row.cell_count,
        'cell_min_v': row.cell_min_v,
        'cell_max_v': row.cell_max_v,
        'cell_avg_v': row.cell_avg_v,
        'cell_delta_mv': row.cell_delta_mv,
        'temp_min_c': row.temp_min_c,
        'temp_max_c': row.temp_max_c,
        'temp_avg_c': row.temp_avg_c,
        # Full per-cell arrays — the certificate renders one bar per cell with
        # its deviation from the pack mean.
        'cell_voltages': _arr(row.cell_voltages_json),
        'cell_temps': _arr(row.cell_temps_json),
    }


def _obd_reads(vehicle_id):
    """Every OBD read of the vehicle that is physically plausible, oldest
    first, as plain dicts — for the cell history, the buffer view and the
    capacity from the BMS counters.

    🔴 A broken decode must not become a data point: the first Kia read of
    2026-08-08 decoded to a 5.10 V phantom cell and a SoH of 3481.9 %. Reads
    with any cell outside the plausible band or a SoH above 110 % are left
    out (they stay in the database, with their raw frames)."""
    import json as _json
    q = ObdReading.query.order_by(ObdReading.timestamp.asc())
    if vehicle_id is not None:
        q = q.filter(ObdReading.vehicle_id == vehicle_id)
    lo, hi = BA_CELL_BAND
    raus = []
    for row in q.all():
        try:
            cells = _json.loads(row.cell_voltages_json) if row.cell_voltages_json else None
        except (ValueError, TypeError):
            cells = None
        try:
            temps = _json.loads(row.cell_temps_json) if row.cell_temps_json else None
        except (ValueError, TypeError):
            temps = None
        if cells and any((not isinstance(v, (int, float))) or v < lo or v > hi for v in cells):
            continue
        if row.soh_pct is not None and row.soh_pct > 110:
            continue
        raus.append({
            'timestamp': row.timestamp.isoformat() if row.timestamp else None,
            'profile': row.profile,
            'soc_bms_pct': row.soc_bms_pct, 'soc_display_pct': row.soc_display_pct,
            'soh_pct': row.soh_pct, 'pack_voltage_v': row.pack_voltage_v,
            'cell_max_v': row.cell_max_v, 'cell_min_v': row.cell_min_v,
            'cumulative_charge_ah': row.cumulative_charge_ah,
            'cumulative_discharge_ah': row.cumulative_discharge_ah,
            'cell_voltages': cells, 'cell_temps': temps,
        })
    return raus


# The same band the decoder uses for optional cells.
try:
    from services.vehicle.obd_decode import CELL_PLAUSIBLE as BA_CELL_BAND
except Exception:  # pragma: no cover - decoder always ships with the app
    BA_CELL_BAND = (2.5, 4.4)

# Typical charge efficiencies an auto-detected charge may have been filled
# with — a wall-energy value equal to capacity/one of these is an estimate.
_EST_EFFICIENCIES = (0.85, 0.88, 0.90, 0.92, 0.95, 1.0)


def _window_analysis(vehicle_id, nominal_kwh):
    """The displayed window over time, from METERED charges only."""
    q = Charge.query.order_by(Charge.date.asc())
    if vehicle_id is not None:
        q = q.filter(Charge.vehicle_id == vehicle_id)
    samples = BA.window_samples(q.all(), (nominal_kwh,) if nominal_kwh else (),
                                _EST_EFFICIENCIES)
    if not samples:
        return None
    return BA.window_trend(samples)


def compute_battery_health(vehicle_id):
    """Assemble every battery-health metric for one vehicle.

    Returns a dict consumed by the PDF generator (and easily JSON-able for
    a future API / preview). Never raises on sparse data — missing signals
    come back as ``None`` and the caller renders "n/a".
    """
    vehicle = Vehicle.query.get(vehicle_id)
    if vehicle is None:
        return None

    # SoH is *always* referenced to the usable NET capacity — the coulomb
    # count and the BMS both measure the usable window, so the nominal must
    # be net (e.g. 64 kWh), never the gross pack (67.3). Gross is carried
    # only for display on the certificate.
    nominal_kwh = _vehicle_field(vehicle_id, 'battery_kwh', 'battery_kwh', 0.0) or None
    gross_kwh = vehicle.battery_kwh_gross or None
    stats = get_summary_stats(vehicle_id=vehicle_id) or {}
    history = get_vehicle_history(vehicle_id=vehicle_id)
    acdc = get_ac_dc_stats(vehicle_id=vehicle_id) or {}

    # ── BMS-reported SoH (Flash-Test equivalent) ─────────────────────
    bms = {'current': None, 'first': None, 'delta': None,
           'baseline': get_soh_baseline(vehicle_id), 'sample_count': 0}
    if history:
        sm = history['summary']
        bms['current'] = sm['last'].get('soh')
        bms['first'] = None
        # first non-null scaled soh
        for v in history['series']['soh']:
            if v is not None:
                bms['first'] = v
                break
        bms['delta'] = sm.get('soh_delta')
        bms['sample_count'] = sum(1 for v in history['series']['soh'] if v is not None)

    # ── Measured SoH (Premium-Test equivalent) ───────────────────────
    measured = _measured_capacity(vehicle_id, nominal_kwh)
    # A coulomb count that only reproduced the configured capacity is not a
    # measurement (see _measured_capacity) — keep the fact, drop the figure.
    measured_circular = bool(measured and measured.get('circular'))
    if measured_circular:
        measured = None

    # ── The displayed window over the years, and what it means ──────
    window = _window_analysis(vehicle_id, nominal_kwh)

    # ── OBD/ELM327 cell snapshot (Flash-Test equivalent, direct BMS) ──
    obd = _latest_obd(vehicle_id)
    obd_soh = obd.get('soh_pct') if obd else None
    if obd_soh is not None and obd_soh > 110:
        obd_soh = None                    # a broken decode, not a battery
    reads = _obd_reads(vehicle_id)
    chem_key, chem_label, chem_nom_v, chem_max_v = BA.chemistry(
        getattr(vehicle, 'battery_chemistry', None))
    cells_series = getattr(vehicle, 'battery_cells_series', None)
    latest_cells = None
    for rd in reversed(reads):
        if rd.get('cell_voltages'):
            latest_cells = rd
            break
    capacity = BA.capacity_picture(
        nominal_kwh, gross_kwh,
        window.get('change_pct') if window else None, obd_soh)
    obd_cap = BA.obd_capacity(reads, chem_nom_v, cells_series)
    buffer_rows = BA.buffer_view(reads, chem_max_v)

    # ── Certified headline SoH. Priority:
    #   1. BOTH a solid coulomb-count AND a direct OBD BMS-register read →
    #      reconcile them (mean of the two independent estimates). The
    #      coulomb-count is optimistically biased and noisy (loss modelling,
    #      SoC quantisation); the BMS register is the car's own considered
    #      estimate. Averaging the two independent signals cancels part of the
    #      coulomb bias and is more defensible than trusting either alone.
    #      Deliberately NOT folded in: the cloud-API BMS SoH — it is
    #      baseline-scaled and can read >100 % (unphysical), so it would only
    #      corrupt the mean. It is still printed separately for reference.
    #   2. measured coulomb-count with a solid sample base (independent of the
    #      BMS' own estimate);
    #   3. a direct OBD SoH register read (real BMS value, fresh);
    #   4. the BMS SoH sampled via the cloud sync;
    #   5. a thin measured value as a last resort. ────────────────────
    measured_soh = measured['soh_pct'] if measured else None
    certified_soh = None
    certified_source = None
    if measured and measured['sample_count'] >= 3 and obd_soh is not None:
        certified_soh = round((measured_soh + obd_soh) / 2, 1)
        certified_source = 'reconciled'
    elif measured and measured['sample_count'] >= 3:
        certified_soh = measured_soh
        certified_source = 'measured'
    elif obd_soh is not None:
        certified_soh = obd_soh
        certified_source = 'obd'
    elif capacity and capacity.get('soh_high') is not None:
        # 🔑 Conservative on purpose: with the gross size known, the lower
        # bound assumes the hidden buffer is already spent — the displayed
        # window cannot tell, and a certificate handed to a buyer must not
        # promise what the data cannot show.
        certified_soh = capacity.get('soh_low', capacity['soh_high'])
        certified_source = 'window'
    elif bms['current'] is not None:
        certified_soh = bms['current']
        certified_source = 'bms'
    elif measured_soh is not None:
        certified_soh = measured_soh
        certified_source = 'measured'

    letter, label_key, color = grade_for_soh(certified_soh)

    # ── Odometer / age ───────────────────────────────────────────────
    odometer = stats.get('total_km') or 0
    if history and history['summary']['last'].get('odometer_km'):
        odometer = max(odometer, history['summary']['last']['odometer_km'] or 0)

    acquired = vehicle.acquired_at
    first_reg = vehicle.first_registered_at
    end_ref = vehicle.retired_at or date.today()
    # Ownership span — how long WE have had the car (and data for it).
    ownership_years = None
    if acquired:
        ownership_years = round((end_ref - acquired).days / 365.25, 1)
    # Battery calendar age — from Erstzulassung, the real degradation clock.
    # Falls back to the ownership start when no Erstzulassung is entered
    # (bought-new case, where the two coincide anyway).
    age_basis = first_reg or acquired
    age_years = None
    if age_basis:
        age_years = round((end_ref - age_basis).days / 365.25, 1)

    # ── Charging behaviour / stress factors ──────────────────────────
    ac_kwh = acdc.get('AC', {}).get('total_kwh', 0) or 0
    dc_kwh = acdc.get('DC', {}).get('total_kwh', 0) or 0
    pv_kwh = acdc.get('PV', {}).get('total_kwh', 0) or 0
    throughput_kwh = ac_kwh + dc_kwh + pv_kwh
    dc_share = round(dc_kwh / throughput_kwh * 100, 1) if throughput_kwh > 0 else None

    # Equivalent full cycles = total energy throughput / nominal pack.
    equiv_cycles = None
    if nominal_kwh:
        equiv_cycles = int(round(throughput_kwh / nominal_kwh)) if throughput_kwh else stats.get('charge_cycles')

    # Deepest observed discharge / average SoC window from charge logs.
    soc_windows = []
    min_soc = None
    q = Charge.query.filter(Charge.soc_from.isnot(None))
    if vehicle_id is not None:
        q = q.filter(Charge.vehicle_id == vehicle_id)
    for c in q.all():
        if c.soc_from is not None:
            min_soc = c.soc_from if min_soc is None else min(min_soc, c.soc_from)
        if c.soc_from is not None and c.soc_to is not None:
            soc_windows.append(c.soc_to - c.soc_from)
    avg_window = round(sum(soc_windows) / len(soc_windows), 1) if soc_windows else None

    # ── Degradation benchmark (informational) ────────────────────────
    expected_soh = None
    vs_benchmark = None
    if age_years is not None and certified_soh is not None:
        expected_soh = round(100 - age_years * AVG_DEGRADATION_PCT_PER_YEAR, 1)
        vs_benchmark = round(certified_soh - expected_soh, 1)

    # Data-basis window
    first_charge = stats.get('first_charge')
    last_charge = stats.get('last_charge')

    # Stable certificate id — deterministic (no RNG), so re-running for the
    # same car/day/odometer yields the same reference number.
    cert_id = 'EVBC-{vid:03d}-{km}-{d}'.format(
        vid=vehicle_id, km=int(odometer or 0),
        d=date.today().strftime('%Y%m%d'))

    return {
        'cert_id': cert_id,
        'generated_at': datetime.now(),
        'vehicle': {
            'id': vehicle.id,
            'name': vehicle.name,
            'brand': vehicle.brand,
            'model': vehicle.model,
            'vin': vehicle.api_vin,
            'color': vehicle.color,
            'is_archived': vehicle.is_archived,
            'first_registered_at': first_reg.isoformat() if first_reg else None,
            'acquired_at': acquired.isoformat() if acquired else None,
            'retired_at': vehicle.retired_at.isoformat() if vehicle.retired_at else None,
            'nominal_kwh': nominal_kwh,
            'gross_kwh': gross_kwh,
        },
        'odometer_km': int(odometer or 0),
        'age_years': age_years,
        'ownership_years': ownership_years,
        'certified_soh': certified_soh,
        'certified_source': certified_source,
        # A certificate is only issued when SoH is actually determinable —
        # from a measured coulomb-count, an OBD read, or the API/BMS value.
        # No source → no health figure → no certificate (see module docstring).
        'can_certify': certified_soh is not None,
        'grade': {'letter': letter, 'label_key': label_key, 'color': color},
        'bms': bms,
        'measured': measured,
        'measured_circular': measured_circular,
        'obd': obd,
        'window': window,
        'capacity': capacity,
        'obd_capacity': obd_cap,
        'buffer_view': buffer_rows,
        'obd_reads': reads,
        'cells': {
            'read': latest_cells,
            'stats': BA.cell_stats(latest_cells['cell_voltages']) if latest_cells else None,
            'series_configured': cells_series,
            'chemistry': {'key': chem_key, 'label': chem_label,
                          'nominal_v': chem_nom_v, 'max_v': chem_max_v},
        },
        'expected_soh': expected_soh,
        'vs_benchmark': vs_benchmark,
        'behaviour': {
            'throughput_kwh': round(throughput_kwh, 1),
            'ac_kwh': round(ac_kwh, 1),
            'dc_kwh': round(dc_kwh, 1),
            'pv_kwh': round(pv_kwh, 1),
            'dc_share_pct': dc_share,
            'equiv_full_cycles': equiv_cycles,
            'min_soc': min_soc,
            'avg_soc_window': avg_window,
            'total_charges': stats.get('total_charges'),
            'total_km': stats.get('total_km'),
        },
        'data_basis': {
            'charge_count': stats.get('total_charges'),
            'sync_count': history['summary']['count'] if history else 0,
            'first_charge': first_charge.isoformat() if first_charge else None,
            'last_charge': last_charge.isoformat() if last_charge else None,
        },
        'history': history,
    }


# ─────────────────────────────────────────────────────────────────────
# PDF rendering
# ─────────────────────────────────────────────────────────────────────

def _fmt(value, fmt='{}', dash='—'):
    """Format ``value`` with ``fmt`` or return ``dash`` when it's None."""
    return fmt.format(value) if value is not None else dash


def _soh_trend_chart(history, nominal_kwh, tmp_dir):
    """Render the baseline-scaled BMS SoH time series to a PNG, or None."""
    if not history:
        return None
    series = history['series']
    ts, vals = [], []
    for iso, soh in zip(series['timestamps'], series['soh']):
        if soh is not None:
            try:
                ts.append(datetime.fromisoformat(iso))
                vals.append(soh)
            except ValueError:
                pass
    if len(vals) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        # matplotlib is a hard dep in production; if it's ever missing the
        # certificate should still render, just without the trend chart.
        return None
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    ax.plot(ts, vals, color='#1565c0', linewidth=1.8)
    ax.fill_between(ts, vals, min(vals) - 1, alpha=0.12, color='#1565c0')
    ax.axhline(WARRANTY_FLOOR_SOH, color='#c62828', linestyle='--', linewidth=1,
               label=t('cert.chart_warranty', default='Garantiegrenze 70%'))
    ax.set_ylabel('SoH %')
    ax.set_title(t('cert.chart_soh_title', default='SoH-Verlauf (BMS)'), fontweight='bold', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='lower left')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m.%y'))
    import matplotlib.pyplot as _plt
    _plt.xticks(rotation=45, ha='right', fontsize=7)
    path = os.path.join(tmp_dir, 'cert_soh.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _mpl():
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        return matplotlib, plt
    except ImportError:
        return None, None


def _short_ts(iso):
    try:
        return datetime.fromisoformat(str(iso)[:19]).strftime('%d.%m.%y %H:%M')
    except ValueError:
        return str(iso or '')[:16]


def _cell_heatmap(voltages, stats, tmp_dir, title, resolution_mv=None):
    """Every cell as a tile in a block — colour = deviation from the pack
    mean, text = cell number and voltage in mV.

    🔑 A HEAT MAP, not bars: 98 bars of 3.82 V are 98 identical lines; a
    block shows at one glance whether the weak cells cluster (one module, one
    corner of the pack) or scatter. The colour scale is symmetric around the
    mean and never narrower than one BMS step, so a perfectly even pack reads
    as one calm colour instead of noise blown up to full scale.
    """
    mpl, plt = _mpl()
    if not plt or not stats:
        return None
    import numpy as np
    n = stats['n']
    cols = BA.grid_columns(n)
    rows = -(-n // cols)
    dev = np.full((rows, cols), np.nan)
    for i, d in enumerate(stats['deviation_mv']):
        dev[i // cols, i % cols] = d
    step = resolution_mv or stats.get('resolution_mv') or 20.0
    lim = max(max(abs(d) for d in stats['deviation_mv']), step)
    cmap = mpl.colormaps['RdBu_r'].with_extremes(bad='#eeeeee')
    fig, ax = plt.subplots(figsize=(7.4, 0.56 * rows + 1.5))
    im = ax.imshow(dev, cmap=cmap, vmin=-lim, vmax=lim, aspect='auto')
    for i, v in enumerate(voltages):
        rr, cc = i // cols, i % cols
        # On a strong tile dark text disappears — white there.
        kraeftig = abs(stats['deviation_mv'][i]) / lim > 0.55
        ax.text(cc, rr - 0.22, str(i + 1), ha='center', va='center',
                fontsize=5, color='#f0f0f0' if kraeftig else '#555555')
        ax.text(cc, rr + 0.16, '%.0f' % (v * 1000), ha='center', va='center',
                fontsize=6.2, fontweight='bold', color='white' if kraeftig else '#222222')
    if not stats['all_equal']:
        # Framed only when they are few: 17 cells sharing the top value are a
        # plateau, and framing "the first six" of them would point at nothing.
        for idx_list, col in ((stats['min_cells'], '#0d47a1'), (stats['max_cells'], '#b71c1c')):
            if len(idx_list) > 6:
                continue
            for c in idx_list:
                rr, cc = (c - 1) // cols, (c - 1) % cols
                ax.add_patch(mpl.patches.Rectangle((cc - 0.5, rr - 0.5), 1, 1, fill=False,
                                                   edgecolor=col, linewidth=1.8))
    ax.set_xticks(np.arange(-0.5, cols, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    ax.grid(which='minor', color='white', linewidth=1.4)
    ax.tick_params(which='both', bottom=False, left=False, labelbottom=False, labelleft=False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, fontweight='bold', fontsize=9.5)
    cb = fig.colorbar(im, ax=ax, orientation='horizontal', fraction=0.06, pad=0.04, aspect=45)
    cb.set_label(t('cert.cellmap_scale', default='Abweichung vom Mittel (mV) - blau tiefer, rot hoeher'),
                 fontsize=7)
    cb.ax.tick_params(labelsize=6.5)
    path = os.path.join(tmp_dir, 'cert_cellmap.png')
    fig.savefig(path, dpi=170, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _cell_history_heatmap(reads, tmp_dir):
    """Cells across reads — does the same cell sit low every time?

    One row per read (with its time and SoC), one column per cell, colour =
    deviation from THAT read's mean. A cell that is low once is noise; a
    cell that is low in every row is the weak cell. Needs two reads with the
    same cell count."""
    mpl, plt = _mpl()
    if not plt:
        return None
    rows = [r for r in reads if r.get('cell_voltages')]
    if len(rows) < 2:
        return None
    n = len(rows[-1]['cell_voltages'])
    rows = [r for r in rows if len(r['cell_voltages']) == n][-12:]
    if len(rows) < 2:
        return None
    import numpy as np
    mat = []
    labels = []
    for r in rows:
        cells = r['cell_voltages']
        m = sum(cells) / n
        mat.append([(v - m) * 1000.0 for v in cells])
        soc = r.get('soc_display_pct') if r.get('soc_display_pct') is not None else r.get('soc_bms_pct')
        labels.append(_short_ts(r['timestamp']) + ('  %.0f %%' % soc if soc is not None else ''))
    mat = np.array(mat)
    lim = max(float(np.nanmax(np.abs(mat))), 20.0)
    fig, ax = plt.subplots(figsize=(7.4, 0.32 * len(rows) + 1.2))
    im = ax.imshow(mat, cmap='RdBu_r', vmin=-lim, vmax=lim, aspect='auto', interpolation='nearest')
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=6.5)
    step = 7 if n > 50 else 2
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([str(i + 1) for i in range(0, n, step)], fontsize=6.5)
    ax.set_xlabel(t('cert.cellchart_xlabel', default='Zelle'), fontsize=7)
    ax.set_title(t('cert.cellhist_title', default='Abweichung je Zelle ueber die Auslesungen (mV)'),
                 fontweight='bold', fontsize=9.5)
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cb.ax.tick_params(labelsize=6.5)
    path = os.path.join(tmp_dir, 'cert_cellhist.png')
    fig.savefig(path, dpi=160, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _window_chart(window, tmp_dir):
    """Energy at the wall per displayed 100 %, per half-year, AC and DC.

    Bars are the middle half of the charges (25 %–75 %), the dot their
    median, the number the charge count. A falling line would be a shrinking
    window; a saw-tooth between first and second half-year is the season."""
    mpl, plt = _mpl()
    if not plt or not window or not window.get('periods'):
        return None
    alle = sorted({p['period'] for p in window['periods']})
    x = {per: i for i, per in enumerate(alle)}
    fig, ax = plt.subplots(figsize=(7.4, 2.9))
    for typ, col, off in (('AC', '#1565c0', -0.12), ('DC', '#ef6c00', 0.12)):
        pts = [p for p in window['periods'] if p['type'] == typ and p['n'] >= 3]
        if not pts:
            continue
        xs = [x[p['period']] + off for p in pts]
        ax.errorbar(xs, [p['median'] for p in pts],
                    yerr=[[p['median'] - p['q1'] for p in pts], [p['q3'] - p['median'] for p in pts]],
                    fmt='o-', color=col, ecolor=col, elinewidth=1, capsize=2, markersize=4,
                    linewidth=1.2, label=typ)
        for xx, p in zip(xs, pts):
            ax.annotate('n=%d' % p['n'], (xx, p['q3']), textcoords='offset points',
                        xytext=(0, 3), ha='center', fontsize=5.5, color=col)
    ax.set_xticks(range(len(alle)))
    ax.set_xticklabels([a.replace('H1', ' I').replace('H2', ' II') for a in alle], fontsize=7)
    ax.set_ylabel(t('cert.window_axis', default='kWh je 100 % Anzeige'), fontsize=7.5)
    ax.tick_params(axis='y', labelsize=7)
    ax.grid(True, axis='y', alpha=0.3)
    ax.legend(fontsize=7, loc='lower left')
    ax.set_title(t('cert.window_chart_title',
                   default='Energie je angezeigtem Vollladen - an der Wand gemessen, je Halbjahr'),
                 fontweight='bold', fontsize=9.5)
    path = os.path.join(tmp_dir, 'cert_window.png')
    fig.savefig(path, dpi=160, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


# Characters latin-1 has no slot for but a reader expects to see in
# a particular shape. Everything NOT listed here is handled by the
# general rule below — the list is for legibility, not for safety.
_LATIN1_NICE = {
    '—': '-', '–': '-', '‑': '-', '−': '-', '·': '-', '•': '-',
    '²': '2', '₂': '2', 'Ø': 'O', '€': 'EUR ', '→': '->',
    '≥': '>=', '≤': '<=', '±': '+/-', '≈': '~',
    '“': '"', '”': '"', '„': '"', '‘': "'", '’': "'", '…': '...',
}

def latin1_safe(text):
    """latin-1 safe BY CONSTRUCTION (fpdf core fonts are latin-1 only).

    This used to be a hand-kept list of replacements with a
    docstring claiming latin-1 safety. It was not safe: any
    character outside the list and outside latin-1 reached
    ``pdf.cell()`` and raised UnicodeEncodeError, which killed the
    whole certificate. A vehicle called "Škoda Enyaq" was enough —
    U+0160 is not in latin-1 — so no Škoda owner could ever
    generate a battery certificate.

    Now anything left over is decomposed to its base letter
    (Š -> S, Č -> C) and only replaced with '?' if even that has
    no latin-1 form. Characters latin-1 DOES have (ä, é, ë, ß)
    are kept exactly as they are — decomposing everything would
    have quietly turned "Prüfung" into "Prufung".
    """
    s = ''.join(_LATIN1_NICE.get(ch, ch) for ch in str(text))
    try:
        s.encode('latin-1')
        return s                      # the common case, untouched
    except UnicodeEncodeError:
        pass
    out = []
    for ch in s:
        try:
            ch.encode('latin-1')
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        base = ''.join(c for c in unicodedata.normalize('NFKD', ch)
                       if not unicodedata.combining(c))
        try:
            base.encode('latin-1')
        except UnicodeEncodeError:
            base = ''
        out.append(base or '?')
    return ''.join(out)


try:
    from fpdf import FPDF

    class _CertPDF(FPDF):
        _clean = staticmethod(latin1_safe)

        def footer(self):
            self.set_y(-15)
            self.set_font('Helvetica', 'I', 7)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, self._clean(t(
                'cert.footer',
                default='Selbst erstelltes Zertifikat - EV Charge Tracker - '
                        'keine akkreditierte Prüfung')),
                align='C')

        def kv(self, label, value, lw=55):
            self.set_font('Helvetica', '', 9)
            self.set_text_color(110, 110, 110)
            self.cell(lw, 6, self._clean(label))
            self.set_font('Helvetica', 'B', 9)
            self.set_text_color(33, 33, 33)
            self.cell(0, 6, self._clean(value), new_x='LMARGIN', new_y='NEXT')

        def section(self, title):
            self.ln(2)
            self.set_font('Helvetica', 'B', 11)
            self.set_text_color(21, 101, 192)
            self.cell(0, 8, self._clean(title), new_x='LMARGIN', new_y='NEXT')
            self.set_draw_color(21, 101, 192)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(2)

    _HAVE_FPDF = True
except ImportError:  # pragma: no cover - fpdf2 is a hard dependency
    _HAVE_FPDF = False


def _hex_rgb(h):
    h = (h or '#000000').lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _halbjahr(period):
    """'2023H1' → '1. Hj. 2023' (localised)."""
    if not period or len(period) < 6:
        return period or '-'
    return t('cert.half_year', default='{h}. Hj. {y}', h=period[-1], y=period[:4])


def _profile_resolution_mv(profile):
    """The step a BMS reports a single cell in, from the decoder profile —
    1 byte × 0.02 V is 20 mV. None when unknown."""
    try:
        from services.vehicle.obd_decode import PROFILES
        spec = (PROFILES.get(profile) or {}).get('cells') or []
        if spec:
            return round(spec[0]['scale'] * 1000.0, 1)
    except Exception:
        pass
    return None


def _abschnitt(pdf, title, need=34):
    """A section heading that never stands alone at the foot of a page."""
    if pdf.get_y() + need > pdf.h - 20:
        pdf.add_page()
    pdf.section(title)


def _para(pdf, text, size=8):
    pdf.set_font('Helvetica', '', size)
    pdf.set_text_color(70, 70, 70)
    pdf.multi_cell(0, 4.2, pdf._clean(text))
    pdf.ln(1)


def _anlage_kopf(pdf, title, sub):
    pdf.add_page()
    pdf.set_fill_color(21, 101, 192)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(0, 10, pdf._clean(title), fill=True, align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    pdf.set_fill_color(33, 118, 210)
    pdf.cell(0, 5.5, pdf._clean(sub), fill=True, align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)


def _page_cells(pdf, data, tmp_dir):
    """Anlage 1 — every cell: a heat map, the statistics, the history."""
    cells = data.get('cells') or {}
    stats, read = cells.get('stats'), cells.get('read')
    if not stats or not read:
        return
    chem = cells.get('chemistry') or {}
    _anlage_kopf(pdf, t('cert.page_cells', default='Anlage 1 - Batterie im Detail: die Zellen'),
                 t('cert.page_cells_sub', default='Direkt aus dem Batteriemanagement gelesen (OBD / ELM327)'))
    res_mv = _profile_resolution_mv(read.get('profile')) or stats.get('resolution_mv')

    _abschnitt(pdf, t('cert.cells_read', default='Auslesung'))
    pdf.kv(t('cert.obd_time', default='Auslesezeitpunkt'), _short_ts(read['timestamp']))
    soc_parts = []
    if read.get('soc_display_pct') is not None:
        soc_parts.append(t('cert.soc_display', default='Anzeige {v:.0f} %', v=read['soc_display_pct']))
    if read.get('soc_bms_pct') is not None:
        soc_parts.append(t('cert.soc_bms', default='BMS {v:.1f} %', v=read['soc_bms_pct']))
    if soc_parts:
        pdf.kv(t('cert.cells_soc', default='Ladestand'), ' / '.join(soc_parts))
    cfg = cells.get('series_configured')
    n_txt = str(stats['n'])
    if cfg and cfg != stats['n']:
        n_txt += '  ' + t('cert.cells_mismatch', default='(eingetragen: {c} in Serie)', c=cfg)
    pdf.kv(t('cert.obd_cells', default='Zellen'), n_txt)
    if chem.get('label') and chem.get('key') != 'other':
        chem_txt = chem['label']
        if chem.get('max_v'):
            chem_txt += t('cert.chem_max', default='  (Ladeschluss {v:.2f} V je Zelle)', v=chem['max_v'])
        pdf.kv(t('cert.chemistry', default='Zellchemie'), chem_txt)
    if res_mv:
        pdf.kv(t('cert.resolution', default='Aufloesung des BMS'),
               t('cert.resolution_val', default='{r:.0f} mV je Schritt', r=res_mv))

    title = t('cert.cellmap_title', default='{n} Zellen - Spannung in mV, Farbe = Abweichung vom Mittel',
              n=stats['n'])
    img = _cell_heatmap(read['cell_voltages'], stats, tmp_dir, title, res_mv)
    if img:
        pdf.ln(1)
        pdf.image(img, x=(pdf.w - 178) / 2, w=178)
        pdf.ln(1)
        _para(pdf, t('cert.cellmap_order',
                     default='Die Kacheln folgen der Zellnummer des Batteriemanagements, Reihe fuer Reihe - '
                             'nicht der Einbaulage im Fahrzeug.'), size=7)

    _abschnitt(pdf, t('cert.cells_stats', default='Kennzahlen'))

    def nummern(lst):
        if len(lst) > 8:
            return t('cert.cells_many', default='{n} Zellen', n=len(lst))
        return ', '.join('#%d' % c for c in lst)
    pdf.kv(t('cert.cells_mean', default='Mittel / Median'),
           '%.3f / %.3f V' % (stats['mean_v'], stats['median_v']))
    pdf.kv(t('cert.cells_min', default='Tiefste Zelle'),
           '%.3f V  (%s)' % (stats['min_v'], nummern(stats['min_cells'])))
    pdf.kv(t('cert.cells_max', default='Hoechste Zelle'),
           '%.3f V  (%s)' % (stats['max_v'], nummern(stats['max_cells'])))
    spread = stats['spread_mv']
    verdict = (t('cert.obd_spread_ok', default='sehr gut') if spread <= 30
               else t('cert.obd_spread_mid', default='ok') if spread <= 60
               else t('cert.obd_spread_bad', default='auffaellig'))
    pdf.kv(t('cert.obd_spread', default='Zell-Spreizung (max-min)'), '%.0f mV (%s)' % (spread, verdict))
    pdf.kv(t('cert.cells_std', default='Standardabweichung'), '%.1f mV' % stats['std_mv'])
    out = stats.get('outliers') or []
    pdf.kv(t('cert.cells_outliers', default='Auffaellige Zellen (> 2 sigma)'),
           nummern(out) if out else t('cert.none', default='keine'))
    temps = read.get('cell_temps') or []
    if temps:
        pdf.kv(t('cert.cells_temps', default='Modultemperaturen'),
               ' / '.join('%g' % v for v in temps) + ' C')
    if stats['all_equal']:
        _para(pdf, t('cert.cells_equal_note',
                     default='Alle Zellen liegen innerhalb eines Messschritts gleich auf. Das ist in der '
                             'Mitte des Ladestands typisch: dort ist die Spannungskurve flach und das BMS '
                             'meldet in {r:.0f}-mV-Schritten. Unterschiede zeigen sich bei hohem (ueber 95 %) '
                             'oder niedrigem (unter 15 %) Ladestand - eine Auslesung dort ist aussagekraeftiger.',
                     r=res_mv or 20))

    hist = _cell_history_heatmap(data.get('obd_reads') or [], tmp_dir)
    if hist:
        if pdf.get_y() + 50 > pdf.h - 20:
            pdf.add_page()
        _abschnitt(pdf, t('cert.cells_history', default='Verlauf ueber alle Auslesungen'))
        pdf.image(hist, x=(pdf.w - 178) / 2, w=178)
        _para(pdf, t('cert.cells_history_note',
                     default='Eine Zelle, die einmal tief liegt, ist Messrauschen. Eine Zelle, die in jeder '
                             'Zeile tief liegt, ist die schwache Zelle des Packs.'))


def _page_capacity(pdf, data, tmp_dir):
    """Anlage 2 — capacity without a lab: the displayed window over the
    years, net vs. gross, the buffer, and the BMS's own counters."""
    win = data.get('window')
    cap = data.get('capacity') or {}
    obd_cap = data.get('obd_capacity') or []
    buf = data.get('buffer_view') or []
    if not win and not cap and not obd_cap and not buf:
        return
    _anlage_kopf(pdf, t('cert.page_capacity', default='Anlage 2 - Kapazitaet, Netto, Brutto und Puffer'),
                 t('cert.page_capacity_sub',
                   default='Aus den gemessenen Ladungen der ganzen Besitzzeit und den BMS-Zaehlern'))

    if win and win.get('periods'):
        _abschnitt(pdf, t('cert.window_sec', default='Das angezeigte Fenster ueber die Jahre'))
        img = _window_chart(win, tmp_dir)
        if img:
            pdf.image(img, x=(pdf.w - 178) / 2, w=178)
            pdf.ln(1)
        _para(pdf, t('cert.window_method',
                     default='Gezaehlt werden nur Ladungen mit gemessener Energie (Wallbox-Zaehler, Ladebeleg) '
                             'und mindestens {w} % SoC-Fenster - {n} Ladungen. Verglichen wird jedes Halbjahr mit '
                             'demselben Halbjahr eines frueheren Jahres: im Winter kostet Laden mehr Energie, ein '
                             'Vergleich Januar gegen Juli wuerde das Wetter messen, nicht die Batterie. Der '
                             'Ladeverlust derselben Wallbox in derselben Jahreszeit aendert sich kaum - aendert '
                             'sich die Energie je Prozent, hat sich das Fenster geaendert.',
                     w=BA.MIN_WINDOW, n=win.get('sample_count', 0)))
        for c in win.get('comparisons') or []:
            pdf.kv('%s - %s' % (c['type'], t('cert.half_n', default='{h}. Halbjahr', h=c['half'][-1])),
                   t('cert.window_cmp',
                     default='{a}: {ra:.1f} kWh (n={na}) -> {b}: {rb:.1f} kWh (n={nb}) = {c:+.1f} %',
                     a=c['ref_period'][:4], ra=c['ref_median'], na=c['ref_n'],
                     b=c['last_period'][:4], rb=c['last_median'], nb=c['last_n'], c=c['change_pct']),
                   lw=40)
        if win.get('change_pct') is not None:
            pdf.kv(t('cert.window_total', default='Veraenderung zusammen'),
                   t('cert.window_total_val', default='{c:+.1f} %  (die Vergleiche liegen {s:.1f} Punkte auseinander)',
                     c=win['change_pct'], s=win['agreement_pct']), lw=40)
        else:
            _para(pdf, t('cert.window_too_few',
                         default='Noch kein Vergleich moeglich: je Halbjahr braucht es mindestens {n} gemessene '
                                 'Ladungen in zwei verschiedenen Jahren.', n=BA.MIN_PERIOD_N))

    if cap:
        _abschnitt(pdf, t('cert.buffer_sec', default='Netto, Brutto und der Puffer'))
        pdf.kv(t('cert.net', default='Netto (Anzeige 0-100 %)'), '%.1f kWh' % cap['net_kwh'], lw=60)
        if cap.get('gross_kwh'):
            pdf.kv(t('cert.gross', default='Brutto (Zellen gesamt)'), '%.1f kWh' % cap['gross_kwh'], lw=60)
        if cap.get('buffer_kwh'):
            pdf.kv(t('cert.buffer', default='Verborgener Puffer'),
                   '%.1f kWh (%.1f %%)' % (cap['buffer_kwh'], cap['buffer_pct']), lw=60)
        if cap.get('usable_kwh') is not None:
            pdf.kv(t('cert.usable_now', default='Nutzbar heute (aus dem Fenster)'),
                   '%.1f kWh' % cap['usable_kwh'], lw=60)
        if cap.get('soh_low') is not None:
            pdf.kv(t('cert.soh_range', default='SoH ohne BMS-Wert'),
                   t('cert.soh_range_val', default='{lo:.1f} % bis {hi:.1f} %', lo=cap['soh_low'], hi=cap['soh_high']),
                   lw=60)
            _para(pdf, t('cert.soh_range_note',
                         default='Oben: der Puffer ist unberuehrt (nutzbar / netto). Unten: der Hersteller hat die '
                                 'Alterung bereits mit dem Puffer ausgeglichen (nutzbar / brutto). Das angezeigte '
                                 'Fenster allein kann beides nicht unterscheiden - das kann nur das BMS.'))
        elif cap.get('soh_high') is not None:
            pdf.kv(t('cert.soh_window', default='SoH aus dem Fenster'), '%.1f %%' % cap['soh_high'], lw=60)
            _para(pdf, t('cert.gross_missing',
                         default='Brutto nicht eingetragen: ohne Bruttogroesse laesst sich nicht sagen, wie viel '
                                 'Alterung der Puffer schon verdeckt. Eintragen unter Einstellungen > Fahrzeuge.'))
        if cap.get('gross_now_kwh') is not None:
            pdf.kv(t('cert.gross_now', default='Brutto heute (BMS-SoH)'),
                   '%.1f kWh' % cap['gross_now_kwh'], lw=60)
        if cap.get('buffer_left_kwh') is not None:
            pdf.kv(t('cert.buffer_left', default='Davon noch Puffer'),
                   t('cert.buffer_left_val', default='{k:.1f} kWh  (etwa {u:.0f} % des Puffers verbraucht)',
                     k=max(cap['buffer_left_kwh'], 0.0), u=cap['buffer_used_pct']), lw=60)
            if cap['buffer_used_pct'] >= 60:
                _para(pdf, t('cert.buffer_spent_note',
                             default='Das angezeigte Fenster ist stabil, das BMS zaehlt aber bereits Alterung: sie '
                                     'wurde mit dem Puffer ausgeglichen. Ist er aufgebraucht, sinkt die nutzbare '
                                     'Kapazitaet ab jetzt mit jeder weiteren Alterung sichtbar.'))

    _abschnitt(pdf, t('cert.obdcap_sec', default='Kapazitaet aus den BMS-Zaehlern'))
    if obd_cap:
        for m in obd_cap[-4:]:
            val = '%.1f Ah' % m['capacity_ah']
            if m.get('capacity_kwh'):
                val += ' / ~%.1f kWh' % m['capacity_kwh']
            pdf.kv('%s -> %s' % (_short_ts(m['from']), _short_ts(m['to'])[-5:]),
                   t('cert.obdcap_val', default='{v}  (BMS {a:.0f} -> {b:.0f} %, {ah} Ah)',
                     v=val, a=m['soc_from'], b=m['soc_to'], ah=m['ah']), lw=48)
    else:
        _para(pdf, t('cert.obdcap_howto',
                     default='Noch keine Messung. So geht sie ohne Labor: einmal direkt nach dem Laden auf 100 % '
                             'auslesen, dann fahren ohne zu laden und bei etwa 20 % noch einmal auslesen. Das BMS '
                             'zaehlt dazwischen jede entnommene Amperestunde - geteilt durch die Ladestandspunkte '
                             'ergibt das die Kapazitaet, ohne jede Annahme ueber Ladeverluste.'))

    if buf:
        _abschnitt(pdf, t('cert.bufview_sec', default='Der Puffer von aussen: Anzeige gegen BMS'))
        for b in buf[-6:]:
            line = t('cert.bufview_row', default='Anzeige {d:.0f} %  /  BMS {b:.1f} %  (Abstand {g:+.1f})',
                     d=b['display'], b=b['bms'], g=b['gap'])
            if b.get('cell_max_v'):
                line += t('cert.bufview_top', default='  -  hoechste Zelle {v:.2f} V', v=b['cell_max_v'])
                if b.get('headroom_mv') is not None:
                    line += t('cert.bufview_head', default=' ({h} mV unter Ladeschluss)', h=b['headroom_mv'])
            pdf.kv(_short_ts(b['timestamp']), line, lw=30)
        if not any(b['top'] for b in buf):
            _para(pdf, t('cert.bufview_need_top',
                         default='Fuer die Frage, ob der Hersteller den Puffer oben oder unten freigibt, fehlt eine '
                                 'Auslesung bei 100 % Anzeige: bleibt die hoechste Zellspannung dort ueber die Jahre '
                                 'gleich, wird das Fenster nicht oben verschoben.'))


def generate_certificate(vehicle_id):
    """Build the battery-health certificate PDF. Returns bytes or None."""
    data = compute_battery_health(vehicle_id)
    if data is None or not _HAVE_FPDF:
        return None
    # Refuse to issue a battery-health certificate with no health figure.
    # SoH could not be derived from the API/BMS, an OBD read, or the charge
    # history — a certificate would be a blank "n/a" grade, which is worse
    # than none. The caller surfaces the "use an OBD dongle" hint instead.
    if not data.get('can_certify'):
        return None

    tmp_dir = tempfile.mkdtemp()
    chart = _soh_trend_chart(data['history'], data['vehicle']['nominal_kwh'], tmp_dir)

    veh = data['vehicle']
    grade = data['grade']
    bms = data['bms']
    meas = data['measured']
    beh = data['behaviour']
    basis = data['data_basis']

    pdf = _CertPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ── Title banner ─────────────────────────────────────────────────
    pdf.set_fill_color(21, 101, 192)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 16)
    pdf.cell(0, 11, pdf._clean(t('cert.title', default='Batterie-Gesundheitszertifikat')),
             fill=True, align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.set_fill_color(33, 118, 210)
    pdf.cell(0, 6, pdf._clean(t(
        'cert.subtitle',
        default='Selbst ermittelt aus der Lade- und Fahrzeughistorie (Coulomb-Zaehlung)')),
        fill=True, align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Certificate id + timestamp line
    pdf.set_text_color(110, 110, 110)
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 5, pdf._clean(
        f"{t('cert.id', default='Zertifikat-Nr.')}: {data['cert_id']}    "
        f"{t('cert.generated', default='Erstellt')}: "
        f"{data['generated_at'].strftime('%d.%m.%Y %H:%M')}"),
        align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)

    # ── Vehicle identity ─────────────────────────────────────────────
    pdf.section(t('cert.sec_vehicle', default='Fahrzeug'))
    name = veh['name'] or '—'
    brandmodel = ' · '.join([p for p in (veh['brand'], veh['model']) if p]) or '—'
    pdf.kv(t('cert.veh_name', default='Fahrzeug'), name)
    pdf.kv(t('cert.veh_brand', default='Marke / Modell'), brandmodel)
    pdf.kv('VIN', veh['vin'] or t('cert.na', default='nicht hinterlegt'))
    # Battery: net (SoH reference) plus gross when known.
    batt_val = _fmt(veh['nominal_kwh'], '{:.1f} kWh netto', dash='—')
    if veh.get('gross_kwh'):
        batt_val += f" / {veh['gross_kwh']:.1f} kWh brutto"
    pdf.kv(t('cert.veh_battery', default='Akku (Nennkapazitaet)'), batt_val)
    pdf.kv(t('cert.veh_odometer', default='Tachostand'),
           f"{data['odometer_km']:,} km".replace(',', '.'))
    pdf.kv(t('cert.veh_first_reg', default='Erstzulassung'),
           veh.get('first_registered_at') or t('cert.na', default='nicht hinterlegt'))
    pdf.kv(t('cert.veh_age', default='Batteriealter'),
           _fmt(data['age_years'], '{} Jahre'))
    period = '—'
    if veh['acquired_at']:
        period = veh['acquired_at']
        if veh['retired_at']:
            period += f" -> {veh['retired_at']}"
    pdf.kv(t('cert.veh_period', default='Besitzzeitraum'), period)

    # ── Headline grade + SoH ─────────────────────────────────────────
    pdf.section(t('cert.sec_result', default='Ergebnis'))
    y0 = pdf.get_y()
    # Grade badge (left)
    r, g, b = _hex_rgb(grade['color'])
    pdf.set_fill_color(r, g, b)
    pdf.set_text_color(255, 255, 255)
    pdf.rect(pdf.l_margin, y0, 30, 26, style='F')
    pdf.set_xy(pdf.l_margin, y0 + 3)
    pdf.set_font('Helvetica', 'B', 26)
    pdf.cell(30, 14, grade['letter'], align='C')
    pdf.set_xy(pdf.l_margin, y0 + 18)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(30, 5, pdf._clean(t(grade['label_key'], default='')), align='C')
    # SoH number (right of badge)
    soh = data['certified_soh']
    pdf.set_xy(pdf.l_margin + 36, y0 + 1)
    pdf.set_text_color(33, 33, 33)
    pdf.set_font('Helvetica', 'B', 30)
    pdf.cell(0, 14, pdf._clean(_fmt(soh, '{:.1f} %', dash='n/a')),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_x(pdf.l_margin + 36)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(110, 110, 110)
    src = data['certified_source']
    src_label = {
        'reconciled': t('cert.src_reconciled',
                        default='gemittelt aus Ladedaten-Messung und BMS-Register (OBD)'),
        'measured': t('cert.src_measured', default='gemessen (Ladedaten, Coulomb-Zaehlung)'),
        'obd': t('cert.src_obd', default='BMS-Wert per OBD (ELM327)'),
        'bms': t('cert.src_bms', default='BMS-Wert des Fahrzeugs'),
        'window': t('cert.src_window',
                    default='aus dem angezeigten Fenster, Puffer als verbraucht angenommen'),
    }.get(src, t('cert.na', default='keine Datengrundlage'))
    pdf.cell(0, 6, pdf._clean(f"State of Health - {src_label}"),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_y(max(pdf.get_y(), y0 + 28))
    pdf.ln(1)

    # ── Two SoH signals ──────────────────────────────────────────────
    pdf.section(t('cert.sec_soh', default='SoH-Bestimmung (zwei unabhaengige Signale)'))
    # Measured (Premium-equivalent)
    if meas:
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               f"{meas['soh_pct']:.1f} %")
        pdf.kv(t('cert.meas_capacity', default='Gemessene nutzbare Kapazitaet'),
               f"{meas['capacity_kwh']:.1f} kWh "
               f"({t('cert.of', default='von')} {_fmt(veh['nominal_kwh'], '{:.1f}')} kWh)")
        pdf.kv(t('cert.meas_samples', default='Messgrundlage'),
               t('cert.meas_samples_val',
                 default='{n} Ladungen mit >= {w}% SoC-Fenster (Median; Streuung {lo}-{hi} kWh)',
                 n=meas['sample_count'], w=meas['window_pct'],
                 lo=f"{meas['min_capacity_kwh']:.1f}", hi=f"{meas['max_capacity_kwh']:.1f}"))
        if meas.get('trend'):
            tr = meas['trend']
            pdf.kv(t('cert.meas_trend', default='Trend (frueh -> aktuell)'),
                   f"{tr['early_soh']:.1f} % -> {tr['recent_soh']:.1f} %")
    elif data.get('measured_circular'):
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               t('cert.meas_circular',
                 default='nicht unabhaengig messbar - Ladeverlust aus der Kapazitaet abgeleitet'))
    else:
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               t('cert.meas_none', default='zu wenige breite Ladefenster (>= 25% SoC) fuer eine Messung'))
    win = data.get('window') or {}
    if win.get('change_pct') is not None:
        pdf.kv(t('cert.window_kv', default='Angezeigtes Fenster'),
               t('cert.window_kv_val', default='{c:+.1f} % seit {ref} - Details in der Anlage',
                 c=win['change_pct'], ref=_halbjahr(win['ref_period'])))
    # BMS (Flash-equivalent)
    if bms['current'] is not None:
        pdf.kv(t('cert.bms_soh', default='BMS-SoH (Fahrzeug)'),
               f"{bms['current']:.1f} %"
               + (f"  (Basiswert {bms['baseline']:.0f})" if bms['baseline'] != 100 else ''))
        if bms['delta'] is not None:
            pdf.kv(t('cert.bms_delta', default='BMS-SoH Veraenderung (Messzeitraum)'),
                   f"{bms['delta']:+.1f} %  ({bms['sample_count']} Syncs)")
    else:
        pdf.kv(t('cert.bms_soh', default='BMS-SoH (Fahrzeug)'),
               t('cert.bms_none', default='vom Fahrzeug nicht gemeldet'))

    # ── OBD cell snapshot (direct BMS read) ──────────────────────────
    obd = data.get('obd')
    if obd:
        pdf.section(t('cert.sec_obd', default='Zelldaten (OBD / ELM327, direkter BMS-Zugriff)'))
        if obd.get('timestamp'):
            ts_disp = obd['timestamp'].replace('T', ' ')[:16]
            pdf.kv(t('cert.obd_time', default='Auslesezeitpunkt'), ts_disp)
        if obd.get('soh_pct') is not None:
            pdf.kv(t('cert.obd_soh', default='BMS-SoH (OBD-Register)'),
                   f"{obd['soh_pct']:.1f} %")
        if obd.get('cell_count'):
            pdf.kv(t('cert.obd_cells', default='Zellen'), f"{obd['cell_count']}")
        if obd.get('cell_min_v') is not None and obd.get('cell_max_v') is not None:
            pdf.kv(t('cert.obd_cell_v', default='Zellspannung min / max / O'),
                   f"{obd['cell_min_v']:.3f} / {obd['cell_max_v']:.3f}"
                   + (f" / {obd['cell_avg_v']:.3f} V" if obd.get('cell_avg_v') is not None else " V"))
        if obd.get('cell_delta_mv') is not None:
            # Spread is THE imbalance indicator — small is healthy.
            spread = obd['cell_delta_mv']
            verdict = (t('cert.obd_spread_ok', default='sehr gut') if spread <= 30
                       else t('cert.obd_spread_mid', default='ok') if spread <= 60
                       else t('cert.obd_spread_bad', default='auffaellig'))
            pdf.kv(t('cert.obd_spread', default='Zell-Spreizung (max-min)'),
                   f"{spread:.0f} mV ({verdict})")
        if obd.get('temp_min_c') is not None and obd.get('temp_max_c') is not None:
            pdf.kv(t('cert.obd_temp', default='Batterietemperatur min / max'),
                   f"{obd['temp_min_c']:.0f} / {obd['temp_max_c']:.0f} C"
                   + (f" (O {obd['temp_avg_c']:.0f} C)" if obd.get('temp_avg_c') is not None else ""))
        if obd.get('pack_voltage_v') is not None:
            pv = f"{obd['pack_voltage_v']:.1f} V"
            if obd.get('pack_current_a') is not None:
                pv += f" @ {obd['pack_current_a']:+.1f} A"
            pdf.kv(t('cert.obd_pack', default='Packspannung / -strom'), pv)
        if obd.get('aux_battery_v') is not None:
            pdf.kv(t('cert.obd_aux', default='12V-Batterie'), f"{obd['aux_battery_v']:.1f} V")

        # The cells themselves have their own page now (Anlage).
        if (data.get('cells') or {}).get('stats'):
            pdf.kv(t('cert.obd_cells_more', default='Einzelzellen'),
                   t('cert.obd_cells_more_val', default='Heatmap aller Zellen in der Anlage'))

    # ── Benchmark ────────────────────────────────────────────────────
    if data['expected_soh'] is not None:
        pdf.section(t('cert.sec_benchmark', default='Einordnung'))
        vb = data['vs_benchmark']
        verdict = (t('cert.bench_above', default='ueber Durchschnitt') if vb is not None and vb >= 0
                   else t('cert.bench_below', default='unter Durchschnitt'))
        pdf.kv(t('cert.bench_expected', default='Erwartung fuer das Alter'),
               t('cert.bench_expected_val',
                 default='~{e:.1f} % (Flottenmittel {r} %/Jahr)',
                 e=data['expected_soh'], r=AVG_DEGRADATION_PCT_PER_YEAR))
        if vb is not None:
            pdf.kv(t('cert.bench_delta', default='Abweichung'),
                   f"{vb:+.1f} %  ({verdict})")
        if soh is not None:
            head = round(soh - WARRANTY_FLOOR_SOH, 1)
            pdf.kv(t('cert.bench_warranty', default='Abstand zur Garantiegrenze (70%)'),
                   f"{head:+.1f} %")

    # ── Charging behaviour / stress ──────────────────────────────────
    pdf.section(t('cert.sec_behaviour', default='Nutzung & Ladeverhalten'))
    pdf.kv(t('cert.beh_throughput', default='Energiedurchsatz gesamt'),
           _fmt(beh['throughput_kwh'], '{:.0f} kWh'))
    pdf.kv(t('cert.beh_cycles', default='Aequivalente Vollzyklen'),
           _fmt(beh['equiv_full_cycles'], '{}'))
    pdf.kv(t('cert.beh_dc', default='Schnellladeanteil (DC)'),
           _fmt(beh['dc_share_pct'], '{:.0f} %'))
    pdf.kv(t('cert.beh_minsoc', default='Tiefste protokollierte Entladung'),
           _fmt(beh['min_soc'], 'SoC {}%'))
    pdf.kv(t('cert.beh_window', default='Durchschn. Ladefenster'),
           _fmt(beh['avg_soc_window'], '{:.0f} %-Punkte'))
    pdf.kv(t('cert.beh_charges', default='Ladevorgaenge / Fahrleistung'),
           f"{_fmt(beh['total_charges'], '{}')} / "
           f"{_fmt(beh['total_km'], '{:,} km').replace(',', '.')}")

    # ── SoH trend chart ──────────────────────────────────────────────
    if chart:
        if pdf.get_y() + 60 > pdf.h - 20:
            pdf.add_page()
        pdf.section(t('cert.sec_trend', default='SoH-Verlauf'))
        pdf.image(chart, x=(pdf.w - 170) / 2, w=170)
        pdf.ln(2)

    # ── Data basis + limitations ─────────────────────────────────────
    if pdf.get_y() + 55 > pdf.h - 20:
        pdf.add_page()
    pdf.section(t('cert.sec_basis', default='Datengrundlage & Methodik'))
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(70, 70, 70)
    basis_line = t(
        'cert.basis_line',
        default='Grundlage: {c} Ladevorgaenge und {s} Fahrzeug-Syncs'
                '{period}. SoH = gemessene nutzbare Kapazitaet / Nennkapazitaet. '
                'Die nutzbare Kapazitaet wird je Ladung aus geladener Energie '
                '(abzueglich Ladeverlust) geteilt durch das gefuellte SoC-Fenster '
                'ermittelt und ueber viele Ladungen gemittelt (Median) - dieselbe '
                'Groesse, die ein professioneller Batterietest per Vollentladung misst.',
        c=basis['charge_count'] or 0, s=basis['sync_count'] or 0,
        period=(f", {basis['first_charge']} bis {basis['last_charge']}"
                if basis['first_charge'] else ''))
    pdf.multi_cell(0, 4.2, pdf._clean(basis_line))
    pdf.ln(1)
    if data.get('obd'):
        # Cell voltages / temperatures ARE included (read via OBD/ELM327).
        limits = t(
            'cert.limits_with_obd',
            default='Zellspannungen, Zell-Spreizung und Batterietemperaturen '
                    'wurden per OBD/ELM327 direkt aus dem BMS ausgelesen und sind '
                    'oben aufgefuehrt. Das Zertifikat ist eine datenbasierte '
                    'Eigenermittlung und ersetzt keine akkreditierte '
                    'Batterieprüfung (z. B. TUV).')
    else:
        limits = t(
            'cert.limits',
            default='Nicht enthalten: Einzelzellspannungen, Zellbalancing und '
                    'Zelltemperaturen - diese erfordern einen direkten BMS-/OBD-'
                    'Zellzugriff. Mit einem ELM327-Dongle koennen diese Werte in '
                    'der App ausgelesen und dann hier ergaenzt werden. Das '
                    'Zertifikat ist eine datenbasierte Eigenermittlung und ersetzt '
                    'keine akkreditierte Batterieprüfung (z. B. TUV).')
    pdf.multi_cell(0, 4.2, pdf._clean(limits))

    # ── Signature line ───────────────────────────────────────────────
    pdf.ln(6)
    pdf.set_draw_color(120, 120, 120)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.l_margin + 70, y)
    pdf.line(pdf.w - pdf.r_margin - 70, y, pdf.w - pdf.r_margin, y)
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(70, 5, pdf._clean(t('cert.sig_place', default='Ort, Datum')))
    pdf.cell(0, 5, pdf._clean(t('cert.sig_owner', default='Unterschrift Halter')),
             align='R', new_x='LMARGIN', new_y='NEXT')

    # ── Anlage: the battery on its own pages ────────────────────────
    _page_cells(pdf, data, tmp_dir)
    _page_capacity(pdf, data, tmp_dir)

    # Cleanup temp files
    for f in os.listdir(tmp_dir):
        try:
            os.remove(os.path.join(tmp_dir, f))
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass

    return pdf.output()
