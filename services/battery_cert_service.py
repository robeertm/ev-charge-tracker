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
from services import units as _U
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
            'cumulative_charge_kwh': getattr(row, 'cumulative_charge_kwh', None),
            'cumulative_discharge_kwh': getattr(row, 'cumulative_discharge_kwh', None),
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
    ax.plot(ts, vals, color='#0f6b6b', linewidth=1.8)
    ax.fill_between(ts, vals, min(vals) - 1, alpha=0.12, color='#0f6b6b')
    ax.axhline(WARRANTY_FLOOR_SOH, color='#b3261e', linestyle='--', linewidth=1,
               label=t('cert.chart_warranty', default='Garantiegrenze 70%'))
    ax.set_ylabel('SoH %')
    ax.grid(True, color='#e2e6ea')
    _chart_look(ax)
    ax.legend(fontsize=7, loc='lower left', frameon=False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m.%y'))
    import matplotlib.pyplot as _plt
    _plt.xticks(rotation=45, ha='right', fontsize=7)
    path = os.path.join(tmp_dir, 'cert_soh.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _chart_look(ax):
    """Hairline frame and muted ticks, matching the report."""
    for s in ax.spines.values():
        s.set_color('#d9dee4')
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors='#4a5561', labelsize=7)
    ax.yaxis.label.set_color('#4a5561')


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
    cb = fig.colorbar(im, ax=ax, orientation='horizontal', fraction=0.06, pad=0.04, aspect=45)
    cb.set_label(t('cert.cellmap_scale', default='Abweichung vom Mittel (mV) - blau tiefer, rot hoeher'),
                 fontsize=7)
    cb.ax.tick_params(labelsize=6.5)
    path = os.path.join(tmp_dir, 'cert_cellmap.png')
    fig.savefig(path, dpi=170, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _cell_history_heatmap(reads, tmp_dir, resolution_mv=None):
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
    # Every cell on the pack mean in every read (within half a BMS step):
    # the map would be one grey field. The caller says it in a sentence.
    if float(np.nanmax(np.abs(mat))) < (resolution_mv or 2.0) / 2.0:
        return None
    lim = max(float(np.nanmax(np.abs(mat))), 20.0)
    fig, ax = plt.subplots(figsize=(7.4, 0.32 * len(rows) + 1.2))
    im = ax.imshow(mat, cmap='RdBu_r', vmin=-lim, vmax=lim, aspect='auto', interpolation='nearest')
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=6.5)
    step = 7 if n > 50 else 2
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([str(i + 1) for i in range(0, n, step)], fontsize=6.5)
    ax.set_xlabel(t('cert.cellchart_xlabel', default='Zelle'), fontsize=7)
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
    for typ, col, off in (('AC', '#0f6b6b', -0.12), ('DC', '#b07a12', 0.12)):
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
    ax.grid(True, axis='y', color='#e2e6ea')
    _chart_look(ax)
    ax.legend(fontsize=7, loc='lower left', frameon=False)
    path = os.path.join(tmp_dir, 'cert_window.png')
    fig.savefig(path, dpi=160, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


# Characters latin-1 has no slot for but a reader expects to see in
# a particular shape. Everything NOT listed here is handled by the
# general rule below — the list is for legibility, not for safety.
_LATIN1_NICE = {
    '—': '-', '–': '-', '‑': '-', '−': '-', '·': '-', '•': '-',
    # 'Ø' (U+00D8, average) and '°' are BOTH in latin-1, so they are kept and
    # not mapped away — the certificate prints 'Ø' and '°C' as-is.
    '²': '2', '₂': '2', '€': 'EUR ', '→': '->',
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


# ── The look (v3.0.168): a test report, not a dashboard print-out. Ink on
# white, one teal accent, serif headings, mono labels — and semantic colour
# (ok / warn / crit) only where something is judged.
_INK = (27, 36, 48)
_INK2 = (74, 85, 97)
_MUTED = (122, 133, 144)
_RULE = (217, 222, 228)
_RULE_SOFT = (233, 237, 241)
_ACCENT = (15, 107, 107)
_ACCENT_SOFT = (227, 240, 239)
_TONES = {
    'ok': ((46, 125, 79), (230, 242, 234)),
    'warn': ((176, 122, 18), (251, 241, 220)),
    'crit': ((179, 38, 30), (249, 227, 225)),
    'info': (_ACCENT, _ACCENT_SOFT),
    'muted': (_MUTED, _RULE_SOFT),
}


def _tone_for_grade(letter):
    return {'A': 'ok', 'B': 'ok', 'C': 'warn', 'D': 'warn'}.get(letter, 'crit')


def _font_dir():
    """matplotlib ships the DejaVu family — a hard dependency already, so
    the certificate gets real Unicode fonts without bundling any."""
    try:
        import matplotlib
        d = os.path.join(os.path.dirname(matplotlib.__file__), 'mpl-data', 'fonts', 'ttf')
        return d if os.path.exists(os.path.join(d, 'DejaVuSans.ttf')) else None
    except Exception:
        return None


try:
    from fpdf import FPDF

    class _CertPDF(FPDF):
        def __init__(self, head_left='', head_right='', ref=''):
            super().__init__(format='A4')
            self.set_margins(18, 16, 18)
            self.head_left, self.head_right, self.ref = head_left, head_right, ref
            d = _font_dir()
            self.uni = False
            if d:
                try:
                    self.add_font('Sans', '', os.path.join(d, 'DejaVuSans.ttf'))
                    self.add_font('Sans', 'B', os.path.join(d, 'DejaVuSans-Bold.ttf'))
                    self.add_font('Serif', '', os.path.join(d, 'DejaVuSerif.ttf'))
                    self.add_font('Serif', 'B', os.path.join(d, 'DejaVuSerif-Bold.ttf'))
                    self.add_font('Mono', '', os.path.join(d, 'DejaVuSansMono.ttf'))
                    self.add_font('Mono', 'B', os.path.join(d, 'DejaVuSansMono-Bold.ttf'))
                    self.uni = True
                except Exception:
                    self.uni = False
            self.f_sans, self.f_serif, self.f_mono = (
                ('Sans', 'Serif', 'Mono') if self.uni else ('Helvetica', 'Times', 'Courier'))

        def multi_cell(self, *args, **kw):
            # Ragged right everywhere: justified text in narrow columns
            # opened wide gaps ("Kia   Niro   EV").
            kw.setdefault('align', 'L')
            return super().multi_cell(*args, **kw)

        def _clean(self, text):
            s = '' if text is None else str(text)
            return s if self.uni else latin1_safe(s)

        @property
        def cw(self):
            return self.w - self.l_margin - self.r_margin

        def color(self, rgb):
            self.set_text_color(*rgb)

        # ── page furniture
        def header(self):
            if self.page_no() == 1:
                return
            self.set_y(9)
            self.set_font(self.f_mono, '', 6.5)
            self.color(_ACCENT)
            self.set_char_spacing(0.4)
            self.cell(self.cw / 2, 4, self._clean(self.head_left.upper()))
            self.set_char_spacing(0)
            self.color(_MUTED)
            self.cell(self.cw / 2, 4, self._clean(self.head_right), align='R',
                      new_x='LMARGIN', new_y='NEXT')
            self.set_draw_color(*_RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, 14, self.w - self.r_margin, 14)
            self.set_y(18)

        def footer(self):
            self.set_y(-13)
            self.set_draw_color(*_RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y() - 1, self.w - self.r_margin, self.get_y() - 1)
            self.set_font(self.f_mono, '', 6.3)
            self.color(_MUTED)
            third = self.cw / 3
            self.cell(third, 5, self._clean(self.ref))
            self.cell(third, 5, self._clean(t(
                'cert.footer', default='Selbst erstelltes Zertifikat - EV Charge Tracker - '
                                       'keine akkreditierte Prüfung')), align='C')
            self.cell(third, 5, self._clean(t('cert.page_n', default='Seite {p} von {n}',
                                              p=self.page_no(), n='{nb}')), align='R')

        # ── type
        def eyebrow(self, text, w=0):
            self.set_font(self.f_mono, 'B', 7.2)
            self.color(_ACCENT)
            self.set_char_spacing(0.8)
            self.cell(w, 5, self._clean(text.upper()), new_x='LMARGIN', new_y='NEXT')
            self.set_char_spacing(0)

        def h1(self, text, w=0):
            self.set_font(self.f_serif, 'B', 17)
            self.color(_INK)
            self.multi_cell(w, 7.6, self._clean(text), new_x='LMARGIN', new_y='NEXT')

        def lead(self, text, w=0):
            self.set_font(self.f_sans, '', 8.8)
            self.color(_INK2)
            self.multi_cell(w, 4.6, self._clean(text), new_x='LMARGIN', new_y='NEXT')

        def section(self, title, num=None, need=30):
            if self.get_y() + need > self.h - self.b_margin:
                self.add_page()
            self.ln(2)
            self.set_font(self.f_serif, 'B', 13)
            self.color(_INK)
            label = ('%s  ·  %s' % (num, title)) if num else title
            self.cell(0, 8, self._clean(label), new_x='LMARGIN', new_y='NEXT')
            self.set_draw_color(*_RULE)
            self.set_line_width(0.3)
            self.line(self.l_margin, self.get_y() + 0.5, self.w - self.r_margin, self.get_y() + 0.5)
            self.ln(3.5)

        def sub(self, title, need=22):
            if self.get_y() + need > self.h - self.b_margin:
                self.add_page()
            self.ln(1.5)
            self.set_font(self.f_serif, 'B', 10.2)
            self.color(_INK2)
            self.cell(0, 6, self._clean(title), new_x='LMARGIN', new_y='NEXT')
            self.ln(0.5)

        def para(self, text, size=8.4, rgb=_INK2):
            self.set_font(self.f_sans, '', size)
            self.color(rgb)
            self.multi_cell(0, size * 0.52, self._clean(text), new_x='LMARGIN', new_y='NEXT')
            self.ln(1.2)

        def caption(self, text):
            self.set_font(self.f_sans, '', 7.2)
            self.color(_MUTED)
            self.multi_cell(0, 3.8, self._clean(text), new_x='LMARGIN', new_y='NEXT')
            self.ln(1.5)

        def kv(self, label, value, lw=62):
            """A label/value row with a hairline under it."""
            lbl = self._clean(label)
            self.set_font(self.f_sans, '', 8.4)
            col = lw if self.get_string_width(lbl) <= lw - 2 else self.get_string_width(lbl) + 3
            y = self.get_y()
            self.color(_MUTED)
            self.cell(col, 6, lbl)
            self.set_font(self.f_sans, 'B', 8.6)
            self.color(_INK)
            val = self._clean(value)
            if self._lines(val, self.w - self.r_margin - self.get_x(), self.f_sans, 'B', 8.6) > 1:
                # A wrapped value: tighter lines, a little air above and below.
                self.set_xy(self.get_x(), y + 1)
                self.multi_cell(0, 4.4, val, new_x='LMARGIN', new_y='NEXT')
                self.ln(1)
            else:
                self.multi_cell(0, 6, val, new_x='LMARGIN', new_y='NEXT')
            self.set_draw_color(*_RULE_SOFT)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())

        def image_full(self, path, w=None):
            w = w or self.cw
            self.image(path, x=self.l_margin + (self.cw - w) / 2, w=w)
            self.ln(1)

        # ── blocks
        def _lines(self, text, w, font, style, size):
            self.set_font(font, style, size)
            return max(1, len(self.multi_cell(w, 1, self._clean(text), dry_run=True, output='LINES')))

        def meta_grid(self, items, cols=3):
            """Master data in columns: mono caption over a bold value."""
            gap = 6
            w = (self.cw - gap * (cols - 1)) / cols
            for i in range(0, len(items), cols):
                row = items[i:i + cols]
                h = max(4 + 4.4 * self._lines(v, w, self.f_sans, 'B', 8.6) for _, v in row) + 2.5
                if self.get_y() + h > self.h - self.b_margin:
                    self.add_page()
                y = self.get_y()
                for j, (k, v) in enumerate(row):
                    x = self.l_margin + j * (w + gap)
                    self.set_xy(x, y)
                    self.set_font(self.f_mono, '', 6.3)
                    self.color(_MUTED)
                    self.set_char_spacing(0.4)
                    self.cell(w, 4, self._clean(k.upper()))
                    self.set_char_spacing(0)
                    self.set_xy(x, y + 4)
                    self.set_font(self.f_sans, 'B', 8.6)
                    self.color(_INK)
                    self.multi_cell(w, 4.4, self._clean(v))
                self.set_xy(self.l_margin, y + h)

        def cards(self, items, cols=3):
            """Key figures: caption, big value, a line of context, a bar."""
            gap, h = 4, 27
            w = (self.cw - gap * (cols - 1)) / cols
            for i in range(0, len(items), cols):
                if self.get_y() + h > self.h - self.b_margin:
                    self.add_page()
                y = self.get_y()
                for j, it in enumerate(items[i:i + cols]):
                    x = self.l_margin + j * (w + gap)
                    tone = _TONES.get(it.get('tone') or 'info')
                    self.set_draw_color(*_RULE)
                    self.set_line_width(0.25)
                    self.rect(x, y, w, h)
                    self.set_xy(x + 3, y + 2.5)
                    self.set_font(self.f_mono, '', 6.2)
                    self.color(_MUTED)
                    self.set_char_spacing(0.4)
                    self.cell(w - 6, 3.5, self._clean(it['key'].upper()))
                    self.set_char_spacing(0)
                    self.set_xy(x + 3, y + 6.5)
                    self.set_font(self.f_serif, 'B', 14.5)
                    self.color(tone[0])
                    self.cell(w - 6, 7, self._clean(it['value']))
                    self.set_xy(x + 3, y + 14)
                    self.set_font(self.f_sans, '', 6.8)
                    self.color(_INK2)
                    self.multi_cell(w - 6, 3.2, self._clean(it.get('sub') or ''), max_line_height=3.2)
                    frac = it.get('frac')
                    if frac is not None:
                        frac = max(0.0, min(1.0, frac))
                        self.set_fill_color(*_RULE_SOFT)
                        self.rect(x + 3, y + h - 3.5, w - 6, 1.4, style='F')
                        self.set_fill_color(*tone[0])
                        if frac > 0:
                            self.rect(x + 3, y + h - 3.5, (w - 6) * frac, 1.4, style='F')
                self.set_xy(self.l_margin, y + h + gap)

        def pill(self, x, y, text, tone='info', h=4.6):
            fg = _TONES.get(tone, _TONES['info'])[0]
            self.set_font(self.f_mono, 'B', 6.2)
            label = self._clean(text.upper())
            self.set_char_spacing(0.3)
            w = self.get_string_width(label) + 4.5
            self.set_draw_color(*fg)
            self.set_line_width(0.3)
            self.rect(x, y, w, h, style='D', round_corners=True, corner_radius=h / 2)
            self.set_xy(x, y)
            self.color(fg)
            self.cell(w, h, label, align='C')
            self.set_char_spacing(0)
            return w

        def table(self, headers, rows, widths, aligns=None):
            """Header in mono capitals, hairlines between rows. A cell given
            as ('pill', text, tone) is drawn as a pill."""
            aligns = aligns or ['L'] * len(headers)
            total = sum(widths)
            widths = [w * self.cw / total for w in widths]

            def head():
                self.set_font(self.f_mono, '', 6.3)
                self.color(_INK2)
                self.set_char_spacing(0.3)
                for w, h_, a in zip(widths, headers, aligns):
                    self.cell(w, 6, self._clean(h_.upper()), align=a)
                self.set_char_spacing(0)
                self.ln(6)
                self.set_draw_color(*_RULE)
                self.set_line_width(0.3)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            head()
            for row in rows:
                hs = []
                for w, c in zip(widths, row):
                    if isinstance(c, tuple):
                        hs.append(1)
                    else:
                        hs.append(self._lines(c, w - 2, self.f_sans, '', 8.2))
                rh = max(hs) * 4.4 + 2.6
                if self.get_y() + rh > self.h - self.b_margin:
                    self.add_page()
                    head()
                y = self.get_y()
                x = self.l_margin
                for w, c, a in zip(widths, row, aligns):
                    if isinstance(c, tuple):
                        self.pill(x + 1, y + (rh - 4.6) / 2, c[1], c[2])
                    else:
                        self.set_xy(x + 1, y + 1.3)
                        mono = a == 'R'
                        self.set_font(self.f_mono if mono else self.f_sans, '', 8 if mono else 8.2)
                        self.color(_INK)
                        self.multi_cell(w - 2, 4.4, self._clean(c), align=a)
                    x += w
                self.set_xy(self.l_margin, y + rh)
                self.set_draw_color(*_RULE_SOFT)
                self.set_line_width(0.2)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(2)

        def boxed(self, fg, bg, draw, bar=None, border=None, pad=3.2):
            """Draw ``draw`` inside a filled box whose height is measured
            first — the box never cuts its own text."""
            x0, w = self.l_margin, self.cw
            inner = w - 2 * pad - (2 if bar else 0)
            probe = _CertPDF()
            probe.add_page()
            probe.set_xy(probe.l_margin, 20)
            draw(probe, probe.l_margin, inner)
            h = probe.get_y() - 20 + 2 * pad
            if self.get_y() + h > self.h - self.b_margin:
                self.add_page()
            y0 = self.get_y()
            self.set_fill_color(*bg)
            if border:
                self.set_draw_color(*border)
                self.set_line_width(0.6)
                self.rect(x0, y0, w, h, style='DF')
            else:
                self.rect(x0, y0, w, h, style='F')
            if bar:
                self.set_fill_color(*bar)
                self.rect(x0, y0, 1.8, h, style='F')
            self.set_xy(x0 + pad + (2 if bar else 0), y0 + pad)
            draw(self, x0 + pad + (2 if bar else 0), inner)
            self.set_xy(self.l_margin, y0 + h + 3)

        def stamp(self, x, y, w, h, tone, big, line2, line3=None):
            fg = _TONES.get(tone, _TONES['info'])[0]
            with self.rotation(-3, x + w / 2, y + h / 2):
                self.set_draw_color(*fg)
                self.set_line_width(0.7)
                self.rect(x, y, w, h, style='D', round_corners=True, corner_radius=1.6)
                self.color(fg)
                self.set_xy(x, y + 3)
                self.set_font(self.f_mono, 'B', 11)
                self.set_char_spacing(0.6)
                self.cell(w, 6, self._clean(big), align='C')
                self.set_char_spacing(0)
                self.set_xy(x, y + 10)
                self.set_font(self.f_mono, '', 7.5)
                self.cell(w, 4.5, self._clean(line2), align='C')
                if line3:
                    self.set_xy(x, y + 14.5)
                    self.cell(w, 4.5, self._clean(line3), align='C')

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


def _probe_pdf():
    probe = _CertPDF()
    probe.set_auto_page_break(auto=True, margin=18)
    probe.add_page()
    return probe


def _measure(draw):
    """Height a block needs, drawn once into a throw-away PDF.
    None when it does not fit on one page."""
    probe = _probe_pdf()
    y0 = probe.get_y()
    draw(probe)
    if probe.page > 1:
        return None
    return probe.get_y() - y0


def _distinct_cell_reads(reads):
    """Reads that carry cells, one per moment: a snapshot saved right after
    a read (same BMS SoC, same cells, within the hour) is the same read."""
    rows = [r for r in reads if r.get('cell_voltages')]
    if not rows:
        return []
    n = len(rows[-1]['cell_voltages'])
    rows = [r for r in rows if len(r['cell_voltages']) == n]
    out = []
    for r in rows:
        p = out[-1] if out else None
        if p and p.get('soc_bms_pct') == r.get('soc_bms_pct') \
                and p['cell_voltages'] == r['cell_voltages']:
            try:
                gap = (datetime.fromisoformat(str(r['timestamp'])[:19])
                       - datetime.fromisoformat(str(p['timestamp'])[:19])).total_seconds()
            except (TypeError, ValueError):
                gap = None
            if gap is not None and 0 <= gap <= 3600:
                continue
        out.append(r)
    return out


def _d(iso):
    """dd.mm. of an ISO timestamp."""
    try:
        return datetime.fromisoformat(str(iso)[:19]).strftime('%d.%m.')
    except ValueError:
        return str(iso or '')[:10]


def _src_label(data):
    return {
        'reconciled': t('cert.src_reconciled',
                        default='gemittelt aus Ladedaten-Messung und BMS-Register (OBD)'),
        'measured': t('cert.src_measured', default='gemessen (Ladedaten, Coulomb-Zählung)'),
        'obd': t('cert.src_obd', default='BMS-Wert per OBD (ELM327)'),
        'bms': t('cert.src_bms', default='BMS-Wert des Fahrzeugs'),
        'window': t('cert.src_window',
                    default='aus dem angezeigten Fenster, Puffer als verbraucht angenommen'),
    }.get(data.get('certified_source'), t('cert.na', default='keine Datengrundlage'))


def _spread_of(data):
    stats = (data.get('cells') or {}).get('stats')
    if stats:
        return stats['spread_mv']
    return (data.get('obd') or {}).get('cell_delta_mv')


def _spread_rating(spread):
    if spread <= 30:
        return t('cert.obd_spread_ok', default='sehr gut'), 'ok'
    if spread <= 60:
        return t('cert.obd_spread_mid', default='ok'), 'warn'
    return t('cert.obd_spread_bad', default='auffällig'), 'crit'


def _last_cap(data):
    caps = data.get('obd_capacity') or []
    return caps[-1] if caps else None


# ── The topics. Each opens with its numbered heading and draws only through
# the pdf methods, so it can be measured in a probe PDF before it is placed.

def _t_cover(pdf, data, ctx):
    veh, grade = data['vehicle'], data['grade']
    soh = data['certified_soh']
    tone = _tone_for_grade(grade['letter'])
    top = pdf.get_y()
    left_w = pdf.cw - 52
    pdf.eyebrow(t('cert.eyebrow', default='Prüfbericht · Traktionsbatterie'), w=left_w)
    pdf.ln(1)
    pdf.h1(t('cert.h1', default='{name} · Batteriebericht vom {d}',
             name=veh['name'] or '—', d=data['generated_at'].strftime('%d.%m.%Y')), w=left_w)
    pdf.ln(1.5)
    pdf.lead(t('cert.lead',
               default='Selbst ermittelt aus Lade-, Fahrzeug- und OBD-Daten dieser App. '
                       'Bericht-Nr. {id}.', id=data['cert_id']), w=left_w)
    bottom = pdf.get_y()
    pdf.stamp(pdf.w - pdf.r_margin - 44, top + 1, 44, 21, tone,
              t('cert.stamp_grade', default='NOTE {g}', g=grade['letter']),
              'SoH ' + _fmt(soh, '{:.1f} %', dash='n/a'),
              t(grade['label_key'], default=''))
    pdf.set_y(max(bottom, top + 26) + 3)
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    # The red box: not an official certificate.
    def rot(p, x, w):
        p.set_x(x)
        p.set_font(p.f_sans, 'B', 9.4)
        p.color(_TONES['crit'][0])
        p.cell(w, 5.5, p._clean(t('cert.not_official_title', default='Kein offizielles Zertifikat')),
               new_x='LMARGIN', new_y='NEXT')
        p.set_x(x)
        p.set_font(p.f_sans, '', 7.9)
        p.color((120, 24, 20))
        p.multi_cell(w, 4, p._clean(t(
            'cert.not_official',
            default='Dieses Dokument ist eine Selbsteinschätzung aus den Daten dieser App – keine '
                    'akkreditierte Batterieprüfung und kein Gutachten. Für Kauf, Verkauf oder '
                    'Garantie nur zusammen mit einer unabhängigen Messung verwenden.')),
            new_x='LMARGIN', new_y='NEXT')
    pdf.boxed(_TONES['crit'][0], _TONES['crit'][1], rot, border=_TONES['crit'][0])

    # Master data
    brandmodel = ' · '.join([p for p in (veh['brand'], veh['model']) if p]) or '—'
    if veh.get('gross_kwh'):
        batt = t('cert.net_gross', default='{n:.1f} kWh netto · {g:.1f} kWh brutto',
                 n=veh['nominal_kwh'] or 0, g=veh['gross_kwh'])
    else:
        batt = _fmt(veh['nominal_kwh'], '{:.1f} kWh', dash='—')
    chem = (data.get('cells') or {}).get('chemistry') or {}
    n_cells = ((data.get('cells') or {}).get('stats') or {}).get('n') or (data.get('obd') or {}).get('cell_count')
    chem_txt = chem.get('label') if chem.get('key') not in (None, 'other') else None
    if n_cells:
        chem_txt = ((chem_txt + ' · ') if chem_txt else '') + t('cert.cells_n', default='{n} Zellen', n=n_cells)
    period = veh['acquired_at'] or '—'
    if veh['acquired_at'] and veh['retired_at']:
        period += ' → ' + veh['retired_at']
    items = [
        (t('cert.veh_brand', default='Marke / Modell'), brandmodel),
        ('VIN', veh['vin'] or t('cert.na', default='nicht hinterlegt')),
        (t('cert.veh_battery', default='Akku (Nennkapazität)'), batt),
        (t('cert.veh_odometer', default='Tachostand'),
         f"{_U.dist(data['odometer_km'], 0):,} {_U.dist_label()}".replace(',', '.')),
        (t('cert.veh_first_reg', default='Erstzulassung'),
         veh.get('first_registered_at') or t('cert.na', default='nicht hinterlegt')),
        (t('cert.veh_age', default='Batteriealter'),
         t('cert.years', default='{y} Jahre', y=data['age_years']) if data.get('age_years') is not None else '—'),
        (t('cert.chemistry', default='Zellchemie'), chem_txt or '—'),
        (t('cert.veh_period', default='Besitzzeitraum'), period),
        (t('cert.obd_time', default='Letzte OBD-Auslesung'),
         _short_ts((data.get('obd') or {}).get('timestamp')) if (data.get('obd') or {}).get('timestamp') else '—'),
    ]
    pdf.meta_grid(items, cols=3)

    pdf.section(t('cert.sec_summary', default='Zusammenfassung'), ctx['num'].get('cover'))
    m = _last_cap(data)

    def urteil(p, x, w):
        y = p.get_y()
        pw = p.pill(x, y, t(grade['label_key'], default=grade['letter']), tone)
        p.set_xy(x, y + 6.5)
        p.set_font(p.f_serif, 'B', 12.5)
        p.color(_INK)
        p.multi_cell(w, 6, p._clean(t('cert.sum_big', default='State of Health {s} – Note {g}',
                                      s=_fmt(soh, '{:.1f} %', dash='n/a'), g=grade['letter'])),
                     new_x='LMARGIN', new_y='NEXT')
        bits = [t('cert.sum_src', default='Bestimmt: {src}.', src=_src_label(data))]
        vb, ex = data.get('vs_benchmark'), data.get('expected_soh')
        if vb is not None and ex is not None:
            bits.append(t('cert.sum_bench_above' if vb >= 0 else 'cert.sum_bench_below',
                          default=('Das sind {d:.1f} Prozentpunkte über dem Flottenmittel für dieses '
                                   'Alter (~{e:.1f} %).') if vb >= 0 else
                                  ('Das sind {d:.1f} Prozentpunkte unter dem Flottenmittel für dieses '
                                   'Alter (~{e:.1f} %).'),
                          d=abs(vb), e=ex))
        if m and m.get('capacity_kwh'):
            bits.append(t('cert.sum_cap',
                          default='Eigene Kapazitätsmessung über die BMS-Zähler: {k:.1f} kWh bzw. '
                                  '{a:.1f} Ah je 100 % BMS-Ladestand (Fahrt {f} → {to}) – eine '
                                  'Einzelmessung, Details in Abschnitt {n}.',
                          k=m['capacity_kwh'], a=m['capacity_ah'], f=_d(m['from']), to=_d(m['to']),
                          n=ctx['num'].get('capmeas', '')))
        p.set_x(x)
        p.set_font(p.f_sans, '', 8.3)
        p.color(_INK2)
        p.multi_cell(w, 4.3, p._clean(' '.join(bits)), new_x='LMARGIN', new_y='NEXT')
    fg, bg = _TONES[tone]
    pdf.boxed(fg, bg, urteil, bar=fg)
    pdf.cards(_cover_cards(data, ctx), cols=3)


def _cover_cards(data, ctx):
    veh = data['vehicle']
    soh = data['certified_soh']
    out = []
    if soh is not None:
        out.append({'key': t('cert.card_soh', default='State of Health'),
                    'value': '%.1f %%' % soh, 'sub': _src_label(data), 'frac': soh / 100.0,
                    'tone': _tone_for_grade(data['grade']['letter'])})
    m = _last_cap(data)
    net = veh.get('nominal_kwh')
    if m and m.get('capacity_kwh'):
        pct = m['capacity_kwh'] / net if net else None
        out.append({'key': t('cert.card_cap', default='Kapazität (BMS-Zähler)'),
                    'value': '%.1f kWh' % m['capacity_kwh'],
                    'sub': t('cert.card_cap_sub', default='{a:.1f} Ah · BMS {f:g} → {to:g} % · {d1}–{d2}',
                             a=m['capacity_ah'], f=m['soc_from'], to=m['soc_to'],
                             d1=_d(m['from']), d2=_d(m['to'])),
                    'frac': pct, 'tone': 'ok' if (pct or 1) >= 0.7 else 'warn'})
    else:
        out.append({'key': t('cert.card_cap', default='Kapazität (BMS-Zähler)'), 'value': '—',
                    'sub': t('cert.card_cap_none', default='noch keine Messfahrt von etwa 100 auf 20 %'),
                    'tone': 'muted'})
    spread = _spread_of(data)
    if spread is not None:
        _, tone = _spread_rating(spread)
        n = ((data.get('cells') or {}).get('stats') or {}).get('n') or (data.get('obd') or {}).get('cell_count')
        out.append({'key': t('cert.card_spread', default='Zell-Spreizung'), 'value': '%.0f mV' % spread,
                    'sub': t('cert.card_spread_sub', default='{n} Zellen · BMS-Schritt {r:.0f} mV',
                             n=n or '?', r=ctx.get('res_mv') or 20),
                    'frac': 1 - spread / 100.0, 'tone': tone})
    win = data.get('window') or {}
    if win.get('change_pct') is not None:
        c = win['change_pct']
        out.append({'key': t('cert.card_window', default='Angezeigtes Fenster'), 'value': '%+.1f %%' % c,
                    'sub': t('cert.card_window_sub', default='seit {ref} · {n} gemessene Ladungen',
                             ref=_halbjahr(win['ref_period']), n=win.get('sample_count', 0)),
                    'frac': 1 + c / 20.0, 'tone': 'ok' if c >= -5 else 'warn'})
    vb, ex = data.get('vs_benchmark'), data.get('expected_soh')
    if vb is not None and ex is not None:
        out.append({'key': t('cert.card_bench', default='Gegenüber der Flotte'), 'value': '%+.1f %%' % vb,
                    'sub': t('cert.card_bench_sub', default='Erwartung ~{e:.1f} % bei {y} Jahren',
                             e=ex, y=data.get('age_years')),
                    'frac': 0.5 + vb / 20.0, 'tone': 'ok' if vb >= 0 else 'warn'})
    if soh is not None:
        head = soh - WARRANTY_FLOOR_SOH
        out.append({'key': t('cert.card_warranty', default='Abstand zur Garantiegrenze'),
                    'value': '%+.1f %%' % head,
                    'sub': t('cert.card_warranty_sub', default='Grenze {w:.0f} % SoH (übliche Herstellergarantie)',
                             w=WARRANTY_FLOOR_SOH),
                    'frac': head / 30.0, 'tone': 'ok' if head >= 5 else ('warn' if head >= 0 else 'crit')})
    return out


def _t_checks(pdf, data, ctx):
    pdf.section(t('cert.sec_checks', default='Prüfpunkte'), ctx['num'].get('checks'))
    veh = data['vehicle']
    soh = data['certified_soh']
    rows = []
    if soh is not None:
        ok = soh >= WARRANTY_FLOOR_SOH
        rows.append([t('cert.card_soh', default='State of Health'), '≥ %.0f %%' % WARRANTY_FLOOR_SOH,
                     '%.1f %%' % soh,
                     ('pill', t('cert.r_pass', default='bestanden') if ok
                      else t('cert.r_fail', default='unter Grenze'), 'ok' if ok else 'crit')])
    vb, ex = data.get('vs_benchmark'), data.get('expected_soh')
    if vb is not None and ex is not None:
        rows.append([t('cert.chk_bench', default='Vergleich mit dem Flottenmittel'), '~%.1f %%' % ex,
                     '%+.1f %%' % vb,
                     ('pill', t('cert.bench_above', default='über Durchschnitt') if vb >= 0
                      else t('cert.bench_below', default='unter Durchschnitt'), 'ok' if vb >= 0 else 'warn')])
    m = _last_cap(data)
    net = veh.get('nominal_kwh')
    if m and m.get('capacity_kwh') and net:
        pct = m['capacity_kwh'] / net * 100.0
        ok = pct >= 70
        rows.append([t('cert.chk_cap', default='Kapazität aus den BMS-Zählern'),
                     '≥ 70 %% · %.1f kWh' % (0.7 * net),
                     t('cert.chk_cap_actual', default='{k:.1f} kWh ({p:.0f} % von netto), Messungen: {n}',
                       k=m['capacity_kwh'], p=pct, n=len(data.get('obd_capacity') or [])),
                     ('pill', t('cert.r_pass', default='bestanden') if ok
                      else t('cert.r_fail', default='unter Grenze'), 'ok' if ok else 'crit')])
    spread = _spread_of(data)
    if spread is not None:
        lbl, tone = _spread_rating(spread)
        rows.append([t('cert.card_spread', default='Zell-Spreizung'), '≤ 30 mV', '%.0f mV' % spread,
                     ('pill', lbl, tone)])
    stats = (data.get('cells') or {}).get('stats')
    if stats:
        out = stats.get('outliers') or []
        rows.append([t('cert.cells_outliers', default='Auffällige Zellen (> 2 σ)'),
                     t('cert.none', default='keine'),
                     ', '.join('#%d' % c for c in out[:8]) if out else t('cert.none', default='keine'),
                     ('pill', t('cert.r_pass', default='bestanden') if not out
                      else t('cert.r_check', default='prüfen'), 'ok' if not out else 'warn')])
    win = data.get('window') or {}
    if win.get('change_pct') is not None:
        c = win['change_pct']
        rows.append([t('cert.card_window', default='Angezeigtes Fenster'), '≥ −5 %', '%+.1f %%' % c,
                     ('pill', t('cert.r_stable', default='stabil') if c >= -5
                      else t('cert.r_shrunk', default='geschrumpft'), 'ok' if c >= -5 else 'warn')])
    cap = data.get('capacity') or {}
    if cap.get('buffer_used_pct') is not None:
        u = cap['buffer_used_pct']
        rows.append([t('cert.buffer', default='Verborgener Puffer'), '—',
                     t('cert.buffer_used_val', default='etwa {u:.0f} % verbraucht', u=u),
                     ('pill', t('cert.r_watch', default='beobachten') if u >= 60
                      else t('cert.r_left', default='vorhanden'), 'warn' if u >= 60 else 'ok')])
    pdf.table([t('cert.th_check', default='Prüfpunkt'), t('cert.th_target', default='Soll'),
               t('cert.th_actual', default='Ist'), t('cert.th_rating', default='Bewertung')],
              rows, [34, 18, 30, 18], ['L', 'L', 'L', 'L'])
    pdf.caption(t('cert.checks_note',
                  default='Die Soll-Werte sind Richtwerte dieser App (übliche Garantiegrenze der Hersteller, '
                          'übliche Zell-Spreizung) – keine Normwerte.'))


def _t_capmeas(pdf, data, ctx):
    pdf.section(t('cert.sec_capmeas', default='Kapazitätsmessung über die BMS-Zähler'), ctx['num'].get('capmeas'))
    caps = data.get('obd_capacity') or []
    veh = data['vehicle']
    if not caps:
        pdf.para(t('cert.obdcap_howto',
                   default='Noch keine Messung. So geht sie ohne Labor: einmal direkt nach dem Laden auf 100 % '
                           'auslesen, dann fahren ohne zu laden und bei etwa 20 % noch einmal auslesen. Das BMS '
                           'zählt dazwischen jede entnommene Amperestunde – geteilt durch die Ladestandspunkte '
                           'ergibt das die Kapazität, ohne jede Annahme über Ladeverluste.'))
        return
    m = caps[-1]
    pdf.para(t('cert.capmeas_lead',
               default='Das Batteriemanagement zählt jede Amperestunde und Kilowattstunde, die in den Akku '
                       'hinein- und aus ihm herausfließt. Zwischen zwei Auslesungen ohne Laden ergibt die '
                       'entnommene Menge geteilt durch die Ladestandspunkte die Kapazität – ohne Annahme '
                       'über Ladeverluste.'))
    a, b = m.get('start') or {}, m.get('end') or {}

    def num(v, f='%.1f'):
        return f % v if v is not None else '—'

    def soc(e):
        return '%s / %s %%' % (num(e.get('soc_display')), num(e.get('soc_bms')))

    def pair(x, y):
        return '%s / %s' % (num(x), num(y))

    def diff(k):
        if a.get(k) is None or b.get(k) is None:
            return None
        return b[k] - a[k]
    rows = [
        [t('cert.cm_start', default='Start'), _short_ts(m['from']), soc(a),
         pair(a.get('ah_in'), a.get('ah_out')), pair(a.get('kwh_in'), a.get('kwh_out')),
         num(a.get('pack_v')) + ' V' if a.get('pack_v') else '—'],
        [t('cert.cm_end', default='Ende'), _short_ts(m['to']), soc(b),
         pair(b.get('ah_in'), b.get('ah_out')), pair(b.get('kwh_in'), b.get('kwh_out')),
         num(b.get('pack_v')) + ' V' if b.get('pack_v') else '—'],
        [t('cert.cm_delta', default='Differenz'), '',
         t('cert.pts', default='{v:+.1f} Pkt', v=(b.get('soc_bms') or 0) - (a.get('soc_bms') or 0))
         if a.get('soc_bms') is not None and b.get('soc_bms') is not None else '—',
         pair(diff('ah_in'), diff('ah_out')).replace('—', '—'),
         pair(diff('kwh_in'), diff('kwh_out')), ''],
    ]
    pdf.table([t('cert.th_point', default='Punkt'), t('cert.th_time', default='Zeitpunkt'),
               t('cert.th_soc', default='Anzeige / BMS'), t('cert.th_ah', default='Ah ein / aus'),
               t('cert.th_kwh', default='kWh ein / aus'), t('cert.th_pack', default='Pack')],
              rows, [16, 21, 21, 26, 25, 12], ['L', 'L', 'R', 'R', 'R', 'R'])
    net, gross = veh.get('nominal_kwh'), veh.get('gross_kwh')
    cards = [{'key': t('cert.cm_taken', default='Entnommen'), 'value': '%.1f Ah' % m['ah'],
              'sub': t('cert.cm_taken_sub', default='{k} kWh · Rekuperation abgezogen',
                       k=('%.1f' % m['kwh']) if m.get('kwh') is not None else '—'), 'tone': 'info'},
             {'key': t('cert.cm_capacity', default='Kapazität je 100 % BMS'),
              'value': ('%.1f kWh' % m['capacity_kwh']) if m.get('capacity_kwh') else '%.1f Ah' % m['capacity_ah'],
              'sub': '%.1f Ah' % m['capacity_ah'], 'tone': 'info'}]
    if net and m.get('capacity_kwh'):
        sub = t('cert.cm_vs_sub_net', default='von {n:.1f} kWh netto', n=net)
        if gross:
            sub += ' · ' + t('cert.cm_vs_sub_gross', default='{p:.0f} % von {g:.1f} kWh brutto',
                             p=m['capacity_kwh'] / gross * 100.0, g=gross)
        pct = m['capacity_kwh'] / net
        cards.append({'key': t('cert.cm_vs', default='Gegenüber Nennwert'), 'value': '%.0f %%' % (pct * 100),
                      'sub': sub, 'frac': pct, 'tone': 'ok' if pct >= 0.7 else 'warn'})
    pdf.ln(1)
    pdf.cards(cards, cols=3)
    pdf.para(t('cert.obdcap_note',
               default='Das BMS zählt Amperestunden und Kilowattstunden genau, seinen Ladestand schätzt es. '
                       'Ein Punkt Abweichung am Anfang oder Ende verschiebt das Ergebnis um rund 1,5 %. '
                       'Belastbar wird es erst über mehrere solche Fahrten.'))
    if len(caps) > 1:
        pdf.sub(t('cert.cm_all', default='Alle Messungen'))
        for c in caps[-6:]:
            val = '%.1f Ah' % c['capacity_ah']
            if c.get('capacity_kwh'):
                val += (' / %.1f kWh' if c.get('kwh_counted') else ' / ~%.1f kWh') % c['capacity_kwh']
            bis = _short_ts(c['to'])
            if c['from'][:10] == c['to'][:10]:
                bis = bis[-5:]           # same day: the time is enough
            pdf.kv('%s -> %s' % (_short_ts(c['from']), bis),
                   t('cert.obdcap_val', default='{v}  (BMS {a:g} -> {b:g} %, {ah} Ah)',
                     v=val, a=c['soc_from'], b=c['soc_to'], ah=c['ah']), lw=56)


def _t_soh(pdf, data, ctx):
    veh, bms, meas = data['vehicle'], data['bms'], data['measured']
    pdf.section(t('cert.sec_soh', default='SoH-Bestimmung (zwei unabhängige Signale)'), ctx['num'].get('soh'))
    obd_soh = (data.get('obd') or {}).get('soh_pct')
    if obd_soh is not None:
        pdf.kv(t('cert.soh_obd_here', default='SoH laut OBD (BMS-Register, verlässlich)'), '%.1f %%' % obd_soh)
    if meas:
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'), '%.1f %%' % meas['soh_pct'])
        pdf.kv(t('cert.meas_capacity', default='Gemessene nutzbare Kapazität'),
               f"{meas['capacity_kwh']:.1f} kWh "
               f"({t('cert.of', default='von')} {_fmt(veh['nominal_kwh'], '{:.1f}')} kWh)")
        pdf.kv(t('cert.meas_samples', default='Messgrundlage'),
               t('cert.meas_samples_val',
                 default='{n} Ladungen mit >= {w}% SoC-Fenster (Median; Streuung {lo}-{hi} kWh)',
                 n=meas['sample_count'], w=meas['window_pct'],
                 lo=f"{meas['min_capacity_kwh']:.1f}", hi=f"{meas['max_capacity_kwh']:.1f}"))
        if meas.get('trend'):
            tr = meas['trend']
            pdf.kv(t('cert.meas_trend', default='Trend (früh -> aktuell)'),
                   f"{tr['early_soh']:.1f} % -> {tr['recent_soh']:.1f} %")
    elif data.get('measured_circular'):
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               t('cert.meas_circular',
                 default='nicht unabhängig messbar - Ladeverlust aus der Kapazität abgeleitet'))
    else:
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               t('cert.meas_none', default='zu wenige breite Ladefenster (>= 25% SoC) für eine Messung'))
    win = data.get('window') or {}
    if win.get('change_pct') is not None:
        pdf.kv(t('cert.window_kv', default='Angezeigtes Fenster'),
               t('cert.window_kv_val', default='{c:+.1f} % seit {ref} - Details in der Anlage',
                 c=win['change_pct'], ref=_halbjahr(win['ref_period'])))
    # The cloud-reported value is a RELATIVE raw figure that on some cars
    # reads above 100 % — kept with a plain caveat, the OBD register above it.
    if bms['current'] is not None:
        pdf.kv(t('cert.bms_soh', default='BMS-SoH (Fahrzeug, relativ)'),
               f"{bms['current']:.1f} %"
               + (f"  (Basiswert {bms['baseline']:.0f})" if bms['baseline'] != 100 else ''))
        if bms['delta'] is not None:
            pdf.kv(t('cert.bms_delta', default='BMS-SoH Veränderung (Messzeitraum)'),
                   f"{bms['delta']:+.1f} %  ({bms['sample_count']} Syncs)")
        pdf.ln(1)
        pdf.para(t('cert.bms_relative_note',
                   default='Der vom Fahrzeug gemeldete BMS-Wert ist ein RELATIVER Rohwert und kann über 100 % '
                           'liegen – er ist KEIN direkter State of Health. Verlässlich ist der direkt aus dem '
                           'BMS-Register gelesene OBD-Wert.'))
    else:
        pdf.kv(t('cert.bms_soh', default='BMS-SoH (Fahrzeug, relativ)'),
               t('cert.bms_none', default='vom Fahrzeug nicht gemeldet'))
    if ctx.get('trend'):
        pdf.sub(t('cert.sec_trend', default='SoH-Verlauf'), need=70)
        pdf.image_full(ctx['trend'])
        pdf.caption(t('cert.chart_soh_note',
                      default='Gezeigt wird der relative BMS-Rohwert – er kann über 100 % liegen und ist kein '
                              'absoluter SoH. Aussagekräftig ist der Verlauf (die Form), nicht die Höhe.'))


def _t_cells(pdf, data, ctx):
    """Everything the OBD read says about the pack and its cells."""
    cells = data.get('cells') or {}
    obd = data.get('obd') or {}
    stats, read = cells.get('stats'), cells.get('read')
    pdf.section(t('cert.topic_cells', default='Die Zellen'), ctx['num'].get('cells'))
    res_mv = ctx.get('res_mv')
    src = read or obd
    items = []
    if src.get('timestamp'):
        items.append((t('cert.obd_time', default='Auslesezeitpunkt'), _short_ts(src['timestamp'])))
    soc_parts = []
    if src.get('soc_display_pct') is not None:
        soc_parts.append(t('cert.soc_display', default='Anzeige {v:.0f} %', v=src['soc_display_pct']))
    if src.get('soc_bms_pct') is not None:
        soc_parts.append(t('cert.soc_bms', default='BMS {v:.1f} %', v=src['soc_bms_pct']))
    if soc_parts:
        items.append((t('cert.cells_soc', default='Ladestand'), ' / '.join(soc_parts)))
    if obd.get('soh_pct') is not None:
        items.append((t('cert.obd_soh', default='BMS-SoH (OBD-Register)'), '%.1f %%' % obd['soh_pct']))
    if obd.get('pack_voltage_v') is not None:
        pv = '%.1f V' % obd['pack_voltage_v']
        if obd.get('pack_current_a') is not None:
            pv += ' @ %+.1f A' % obd['pack_current_a']
        items.append((t('cert.obd_pack', default='Packspannung / -strom'), pv))
    if obd.get('temp_min_c') is not None and obd.get('temp_max_c') is not None:
        temp = '%.0f / %.0f °C' % (obd['temp_min_c'], obd['temp_max_c'])
        if obd.get('temp_avg_c') is not None:
            temp += ' (Ø %.0f °C)' % obd['temp_avg_c']
        items.append((t('cert.obd_temp', default='Batterietemperatur min / max'), temp))
    if obd.get('aux_battery_v') is not None:
        items.append((t('cert.obd_aux', default='12V-Batterie'), '%.1f V' % obd['aux_battery_v']))
    if res_mv:
        items.append((t('cert.resolution', default='Auflösung des BMS'),
                      t('cert.resolution_val', default='{r:.0f} mV je Schritt', r=res_mv)))
    cfg = cells.get('series_configured')
    n_cells = (stats or {}).get('n') or obd.get('cell_count')
    if n_cells:
        n_txt = str(n_cells)
        if cfg and cfg != n_cells:
            n_txt += '  ' + t('cert.cells_mismatch', default='(eingetragen: {c} in Serie)', c=cfg)
        items.append((t('cert.obd_cells', default='Zellen'), n_txt))
    pdf.meta_grid(items, cols=3)
    if not stats:
        if obd.get('cell_min_v') is not None and obd.get('cell_max_v') is not None:
            pdf.kv(t('cert.obd_cell_v', default='Zellspannung min / max'),
                   '%.3f / %.3f V' % (obd['cell_min_v'], obd['cell_max_v']))
        if obd.get('cell_delta_mv') is not None:
            lbl, _ = _spread_rating(obd['cell_delta_mv'])
            pdf.kv(t('cert.obd_spread', default='Zell-Spreizung (max-min)'),
                   '%.0f mV (%s)' % (obd['cell_delta_mv'], lbl))
        return
    if ctx.get('cellmap'):
        pdf.ln(1)
        pdf.image_full(ctx['cellmap'], w=pdf.cw * 0.86)
        pdf.caption(t('cert.cellmap_title', default='{n} Zellen - Spannung in mV, Farbe = Abweichung vom Mittel',
                      n=stats['n']) + '. ' + t(
            'cert.cellmap_order', default='Die Kacheln folgen der Zellnummer des Batteriemanagements, Reihe für '
                                          'Reihe – nicht der Einbaulage im Fahrzeug.'))

    def nummern(lst):
        if len(lst) > 8:
            return t('cert.cells_many', default='{n} Zellen', n=len(lst))
        return ', '.join('#%d' % c for c in lst)
    pdf.sub(t('cert.cells_stats', default='Kennzahlen'))
    pdf.kv(t('cert.cells_mean', default='Mittel / Median'), '%.3f / %.3f V' % (stats['mean_v'], stats['median_v']))
    pdf.kv(t('cert.cells_min', default='Tiefste Zelle'), '%.3f V  (%s)' % (stats['min_v'], nummern(stats['min_cells'])))
    pdf.kv(t('cert.cells_max', default='Höchste Zelle'), '%.3f V  (%s)' % (stats['max_v'], nummern(stats['max_cells'])))
    lbl, _ = _spread_rating(stats['spread_mv'])
    pdf.kv(t('cert.obd_spread', default='Zell-Spreizung (max-min)'), '%.0f mV (%s)' % (stats['spread_mv'], lbl))
    pdf.kv(t('cert.cells_std', default='Standardabweichung'), '%.1f mV' % stats['std_mv'])
    out = stats.get('outliers') or []
    pdf.kv(t('cert.cells_outliers', default='Auffällige Zellen (> 2 σ)'),
           nummern(out) if out else t('cert.none', default='keine'))
    temps = read.get('cell_temps') or []
    if temps:
        pdf.kv(t('cert.cells_temps', default='Modultemperaturen'), ' / '.join('%g' % v for v in temps) + ' °C')
    if stats['all_equal']:
        pdf.ln(1.5)
        pdf.para(t('cert.cells_equal_note',
                   default='Alle Zellen liegen innerhalb eines Messschritts gleich auf. Das ist in der Mitte des '
                           'Ladestands typisch: dort ist die Spannungskurve flach und das BMS meldet in '
                           '{r:.0f}-mV-Schritten. Unterschiede zeigen sich bei hohem (über 95 %) oder niedrigem '
                           '(unter 15 %) Ladestand – eine Auslesung dort ist aussagekräftiger.', r=res_mv or 20))
    rows = ctx.get('cell_rows') or []
    if ctx.get('cellhist'):
        pdf.sub(t('cert.cells_history', default='Verlauf über alle Auslesungen'), need=55)
        pdf.image_full(ctx['cellhist'])
        pdf.caption(t('cert.cells_history_note',
                      default='Eine Zelle, die einmal tief liegt, ist Messrauschen. Eine Zelle, die in jeder Zeile '
                              'tief liegt, ist die schwache Zelle des Packs.'))
    elif len(rows) >= 2:
        # Nothing to colour: every cell sat on the pack mean in every read.
        # An empty grey map says nothing — one sentence says it all.
        pdf.sub(t('cert.cells_history', default='Verlauf über alle Auslesungen'))
        pdf.para(t('cert.cells_history_flat',
                   default='In allen {n} Auslesungen ({a} bis {b}) lagen alle Zellen innerhalb eines '
                           'Messschritts gleich auf – keine Zelle liegt wiederholt tief.',
                   n=len(rows), a=_short_ts(rows[0]['timestamp'])[:8], b=_short_ts(rows[-1]['timestamp'])[:8]))


def _t_buffer(pdf, data, ctx):
    cap = data.get('capacity') or {}
    buf = data.get('buffer_view') or []
    pdf.section(t('cert.buffer_sec', default='Netto, Brutto und der Puffer'), ctx['num'].get('buffer'))
    if cap:
        pdf.kv(t('cert.net', default='Netto (Anzeige 0-100 %)'), '%.1f kWh' % cap['net_kwh'])
        if cap.get('gross_kwh'):
            pdf.kv(t('cert.gross', default='Brutto (Zellen gesamt)'), '%.1f kWh' % cap['gross_kwh'])
        if cap.get('buffer_kwh'):
            pdf.kv(t('cert.buffer', default='Verborgener Puffer'),
                   '%.1f kWh (%.1f %%)' % (cap['buffer_kwh'], cap['buffer_pct']))
        if cap.get('usable_kwh') is not None:
            pdf.kv(t('cert.usable_now', default='Nutzbar heute (aus dem Fenster)'), '%.1f kWh' % cap['usable_kwh'])
        if cap.get('soh_low') is not None:
            pdf.kv(t('cert.soh_range', default='SoH ohne BMS-Wert'),
                   t('cert.soh_range_val', default='{lo:.1f} % bis {hi:.1f} %', lo=cap['soh_low'], hi=cap['soh_high']))
        elif cap.get('soh_high') is not None:
            pdf.kv(t('cert.soh_window', default='SoH aus dem Fenster'), '%.1f %%' % cap['soh_high'])
        if cap.get('gross_now_kwh') is not None:
            pdf.kv(t('cert.gross_now', default='Brutto heute (BMS-SoH)'), '%.1f kWh' % cap['gross_now_kwh'])
        if cap.get('buffer_left_kwh') is not None:
            pdf.kv(t('cert.buffer_left', default='Davon noch Puffer'),
                   t('cert.buffer_left_val', default='{k:.1f} kWh  (etwa {u:.0f} % des Puffers verbraucht)',
                     k=max(cap['buffer_left_kwh'], 0.0), u=cap['buffer_used_pct']))
        pdf.ln(1.5)
        if cap.get('soh_low') is not None:
            pdf.para(t('cert.soh_range_note',
                       default='Oben: der Puffer ist unberührt (nutzbar / netto). Unten: der Hersteller hat die '
                               'Alterung bereits mit dem Puffer ausgeglichen (nutzbar / brutto). Das angezeigte '
                               'Fenster allein kann beides nicht unterscheiden – das kann nur das BMS.'))
        elif cap.get('soh_high') is not None:
            pdf.para(t('cert.gross_missing',
                       default='Brutto nicht eingetragen: ohne Bruttogröße lässt sich nicht sagen, wie viel '
                               'Alterung der Puffer schon verdeckt. Eintragen unter Einstellungen > Fahrzeuge.'))
        if cap.get('buffer_used_pct') is not None and cap['buffer_used_pct'] >= 60:
            pdf.para(t('cert.buffer_spent_note',
                       default='Das angezeigte Fenster ist stabil, das BMS zählt aber bereits Alterung: sie wurde '
                               'mit dem Puffer ausgeglichen. Ist er aufgebraucht, sinkt die nutzbare Kapazität ab '
                               'jetzt mit jeder weiteren Alterung sichtbar.'))
    if buf:
        pdf.sub(t('cert.bufview_sec', default='Der Puffer von außen: Anzeige gegen BMS'))
        rows, prev = [], None
        for b in buf:
            # A snapshot saved right after a read is the same moment.
            if prev and prev['display'] == b['display'] and prev['bms'] == b['bms']:
                try:
                    gap = (datetime.fromisoformat(str(b['timestamp'])[:19])
                           - datetime.fromisoformat(str(prev['timestamp'])[:19])).total_seconds()
                except (TypeError, ValueError):
                    gap = None
                if gap is not None and 0 <= gap <= 3600:
                    continue
            prev = b
            top = '—'
            if b.get('cell_max_v'):
                top = '%.2f V' % b['cell_max_v']
                if b.get('headroom_mv') is not None:
                    top += ' (−%d mV)' % b['headroom_mv']
            rows.append([_short_ts(b['timestamp']), '%.1f %%' % b['display'], '%.1f %%' % b['bms'],
                         '%+.1f' % b['gap'], top])
        rows = rows[-8:]
        pdf.table([t('cert.th_time', default='Zeitpunkt'), t('cert.th_display', default='Anzeige'),
                   t('cert.th_bms', default='BMS'), t('cert.th_gap', default='Abstand'),
                   t('cert.th_topcell', default='Höchste Zelle')],
                  rows, [26, 16, 16, 14, 28], ['L', 'R', 'R', 'R', 'R'])
        if not any(b['top'] for b in buf):
            pdf.caption(t('cert.bufview_need_top',
                          default='Für die Frage, ob der Hersteller den Puffer oben oder unten freigibt, fehlt eine '
                                  'Auslesung bei 100 % Anzeige: bleibt die höchste Zellspannung dort über die Jahre '
                                  'gleich, wird das Fenster nicht oben verschoben.'))


def _t_window(pdf, data, ctx):
    win = data.get('window') or {}
    pdf.section(t('cert.window_sec', default='Das angezeigte Fenster über die Jahre'), ctx['num'].get('window'))
    if ctx.get('window'):
        pdf.image_full(ctx['window'])
        pdf.caption(t('cert.window_chart_title',
                      default='Strom für eine volle Ladung (0 → 100 %) – an der Wallbox gemessen, je Halbjahr'))
    pdf.para(t('cert.window_method',
               default='Gezählt werden nur Ladungen mit gemessener Energie (Wallbox-Zähler, Ladebeleg) und '
                       'mindestens {w} % SoC-Fenster – {n} Ladungen. Verglichen wird jedes Halbjahr mit demselben '
                       'Halbjahr eines früheren Jahres: im Winter kostet Laden mehr Energie, ein Vergleich Januar '
                       'gegen Juli würde das Wetter messen, nicht die Batterie.',
               w=BA.MIN_WINDOW, n=win.get('sample_count', 0)))
    for c in win.get('comparisons') or []:
        pdf.kv('%s - %s' % (c['type'], t('cert.half_n', default='{h}. Halbjahr', h=c['half'][-1])),
               t('cert.window_cmp', default='{a}: {ra:.1f} kWh (n={na}) -> {b}: {rb:.1f} kWh (n={nb}) = {c:+.1f} %',
                 a=c['ref_period'][:4], ra=c['ref_median'], na=c['ref_n'],
                 b=c['last_period'][:4], rb=c['last_median'], nb=c['last_n'], c=c['change_pct']), lw=42)
    if win.get('change_pct') is not None:
        pdf.kv(t('cert.window_total', default='Veränderung zusammen'),
               t('cert.window_total_val', default='{c:+.1f} %  (die Vergleiche liegen {s:.1f} Punkte auseinander)',
                 c=win['change_pct'], s=win['agreement_pct']), lw=42)
    else:
        pdf.para(t('cert.window_too_few',
                   default='Noch kein Vergleich möglich: je Halbjahr braucht es mindestens {n} gemessene Ladungen '
                           'in zwei verschiedenen Jahren.', n=BA.MIN_PERIOD_N))


def _t_use(pdf, data, ctx):
    beh = data['behaviour']
    pdf.section(t('cert.sec_behaviour', default='Nutzung & Ladeverhalten'), ctx['num'].get('use'))
    items = [
        (t('cert.beh_throughput', default='Energiedurchsatz gesamt'), _fmt(beh['throughput_kwh'], '{:.0f} kWh')),
        (t('cert.beh_cycles', default='Äquivalente Vollzyklen'), _fmt(beh['equiv_full_cycles'], '{}')),
        (t('cert.beh_dc', default='Schnellladeanteil (DC)'), _fmt(beh['dc_share_pct'], '{:.0f} %')),
        (t('cert.beh_minsoc', default='Tiefste protokollierte Entladung'), _fmt(beh['min_soc'], 'SoC {}%')),
        (t('cert.beh_window', default='Durchschn. Ladefenster'),
         t('cert.pp', default='{v:.0f} %-Punkte', v=beh['avg_soc_window'])
         if beh.get('avg_soc_window') is not None else '—'),
        (t('cert.beh_charges', default='Ladevorgänge / Fahrleistung'),
         f"{_fmt(beh['total_charges'], '{}')} / "
         f"{_fmt(_U.dist(beh['total_km'], 0), '{:,} ' + _U.dist_label()).replace(',', '.')}"),
    ]
    pdf.meta_grid(items, cols=3)


def _t_basis(pdf, data, ctx):
    basis = data['data_basis']
    pdf.section(t('cert.sec_basis', default='Datengrundlage & Methodik'), ctx['num'].get('basis'))
    pdf.para(t('cert.basis_line',
               default='Grundlage: {c} Ladevorgänge und {s} Fahrzeug-Syncs{period}. SoH = gemessene nutzbare '
                       'Kapazität / Nennkapazität.',
               c=basis['charge_count'] or 0, s=basis['sync_count'] or 0,
               period=(f", {basis['first_charge']} bis {basis['last_charge']}" if basis['first_charge'] else '')))
    if data.get('obd'):
        limits = t('cert.limits_with_obd',
                   default='Zellspannungen, Zell-Spreizung und Batterietemperaturen wurden per OBD/ELM327 direkt '
                           'aus dem BMS ausgelesen. Das Zertifikat ist eine datenbasierte Eigenermittlung und '
                           'ersetzt keine akkreditierte Batterieprüfung (z. B. TÜV).')
    else:
        limits = t('cert.limits',
                   default='Nicht enthalten: Einzelzellspannungen, Zellbalancing und Zelltemperaturen – diese '
                           'erfordern einen direkten BMS-/OBD-Zellzugriff. Das Zertifikat ist eine datenbasierte '
                           'Eigenermittlung und ersetzt keine akkreditierte Batterieprüfung (z. B. TÜV).')
    pdf.para(limits)
    # Signature lines — the end of the report.
    pdf.ln(12)
    y = pdf.get_y()
    half = (pdf.cw - 16) / 2
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, y, pdf.l_margin + half, y)
    pdf.line(pdf.w - pdf.r_margin - half, y, pdf.w - pdf.r_margin, y)
    pdf.set_y(y + 1.5)
    pdf.set_font(pdf.f_sans, '', 7.5)
    pdf.color(_INK2)
    pdf.cell(half + 16, 4.5, pdf._clean(t('cert.sig_place', default='Ort, Datum')))
    pdf.cell(half, 4.5, pdf._clean(t('cert.sig_owner', default='Unterschrift Halter')),
             new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)
    pdf.caption(t('cert.made_with', default='Erstellt mit EV Charge Tracker {v} am {d}.',
                  v=ctx.get('version', ''), d=data['generated_at'].strftime('%d.%m.%Y %H:%M')))


# Gap between two topics sharing a page.
_TOPIC_GAP = 6


def _place_topics(pdf, topics, data, ctx):
    """One topic per page. A second topic joins a page only when it fits
    there COMPLETELY (measured in a probe PDF first) — never a third, and
    never one that would break over onto the next page."""
    on_page = 0
    for draw in topics:
        h = _measure(lambda p: draw(p, data, ctx))
        room = pdf.h - pdf.b_margin - pdf.get_y()
        if on_page == 1 and h is not None and h + _TOPIC_GAP <= room:
            pdf.ln(_TOPIC_GAP)
            on_page = 2
        else:
            pdf.add_page()
            on_page = 1
        start = pdf.page
        draw(pdf, data, ctx)
        if pdf.page != start:
            on_page = 2           # ran over a page: the next topic starts fresh


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
    # Every chart is drawn once, up front: the topics are measured in a
    # probe PDF before they are placed, and both passes use the same files.
    try:
        from config import Config as _Cfg
        version = 'v' + _Cfg.APP_VERSION
    except Exception:
        version = ''
    ctx = {'trend': _soh_trend_chart(data['history'], data['vehicle']['nominal_kwh'], tmp_dir),
           'version': version}
    cells = data.get('cells') or {}
    stats, read = cells.get('stats'), cells.get('read')
    if stats and read:
        ctx['res_mv'] = _profile_resolution_mv(read.get('profile')) or stats.get('resolution_mv')
        ctx['cellmap'] = _cell_heatmap(read['cell_voltages'], stats, tmp_dir, '', ctx['res_mv'])
        ctx['cell_rows'] = _distinct_cell_reads(data.get('obd_reads') or [])
        ctx['cellhist'] = _cell_history_heatmap(ctx['cell_rows'], tmp_dir, ctx['res_mv'])
    win = data.get('window')
    if win and win.get('periods'):
        ctx['window'] = _window_chart(win, tmp_dir)

    # Most important first. Each name gets its section number.
    order = [('cover', _t_cover), ('checks', _t_checks), ('capmeas', _t_capmeas), ('soh', _t_soh)]
    if data.get('obd') or stats:
        order.append(('cells', _t_cells))
    if data.get('capacity') or data.get('buffer_view'):
        order.append(('buffer', _t_buffer))
    if win and win.get('periods'):
        order.append(('window', _t_window))
    order += [('use', _t_use), ('basis', _t_basis)]
    ctx['num'] = {name: i + 1 for i, (name, _) in enumerate(order)}

    veh = data['vehicle']
    pdf = _CertPDF(head_left=t('cert.head_left', default='Batteriebericht · {n}', n=veh['name'] or ''),
                   head_right=data['generated_at'].strftime('%d.%m.%Y'),
                   ref=data['cert_id'])
    pdf.set_auto_page_break(auto=True, margin=18)
    _place_topics(pdf, [fn for _, fn in order], data, ctx)

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


def obd_zaehler_neu_lesen():
    """Decode the four BMS counters of every stored Kia/Hyundai read again
    from its raw frames (v3.0.167). Returns how many reads changed.

    Only the counter columns are touched; a read whose frames no longer
    decode keeps what it had."""
    import json as _json
    from models.database import db, ObdReading
    from services.vehicle.obd_decode import decode
    keys = ('cumulative_charge_ah', 'cumulative_discharge_ah',
            'cumulative_charge_kwh', 'cumulative_discharge_kwh')
    n = 0
    for r in ObdReading.query.filter_by(profile='kia_hyundai_ext').all():
        try:
            frames = _json.loads(r.raw_json) if r.raw_json else None
            d = decode(r.profile, frames) if frames else None
        except Exception:
            d = None
        if not d or d.get('error') or d.get('cumulative_charge_ah') is None:
            continue
        neu = {k: d.get(k) for k in keys}
        if any(getattr(r, k) != v for k, v in neu.items()):
            for k, v in neu.items():
                setattr(r, k, v)
            n += 1
    db.session.commit()
    return n
