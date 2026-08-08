"""Battery-Health Certificate service (self-generated).

Robert asked for a self-created battery-health certificate he can hand a
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
cloud API (e.g. ev-mike), no OBD/ELM327 read is on file, AND there aren't
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
import os
import tempfile
from datetime import date, datetime
from statistics import median

from models.database import Charge, Vehicle, ObdReading
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

    if not samples:
        return None

    caps = [s[1] for s in samples]
    med = median(caps)
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
            'early_soh': round(early / nominal_kwh * 100, 1),
            'recent_soh': round(recent / nominal_kwh * 100, 1),
            'from': samples[0][0].isoformat() if samples[0][0] else None,
            'to': samples[-1][0].isoformat() if samples[-1][0] else None,
        }
    return {
        'capacity_kwh': round(med, 2),
        'soh_pct': round(med / nominal_kwh * 100, 1),
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

    # ── OBD/ELM327 cell snapshot (Flash-Test equivalent, direct BMS) ──
    obd = _latest_obd(vehicle_id)
    obd_soh = obd.get('soh_pct') if obd else None

    # ── Certified headline SoH. Priority:
    #   1. measured coulomb-count with a solid sample base (Premium-grade,
    #      independent of the BMS' own estimate);
    #   2. a direct OBD SoH register read (real BMS value, fresh);
    #   3. the BMS SoH sampled via the cloud sync;
    #   4. a thin measured value as a last resort. ────────────────────
    measured_soh = measured['soh_pct'] if measured else None
    certified_soh = None
    certified_source = None
    if measured and measured['sample_count'] >= 3:
        certified_soh = measured_soh
        certified_source = 'measured'
    elif obd_soh is not None:
        certified_soh = obd_soh
        certified_source = 'obd'
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
        'obd': obd,
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


def _cell_voltage_chart(cell_voltages, tmp_dir):
    """Render a per-cell voltage + deviation chart to a PNG, or None.

    Two stacked panels sharing the cell axis:
      * top    — one bar per cell = its absolute voltage, with the pack mean
                 drawn as a dashed line (y-axis tightened around the spread so
                 the small differences are actually visible);
      * bottom — one bar per cell = its deviation from the mean in mV, colour
                 coded (green ≤10 mV, amber ≤25 mV, red above) so a weak or
                 runaway cell jumps out. The title also states the worst
                 deviation in both mV and % of the mean.

    This is the "jede einzelne Zelle mit Spannung und Abweichung" view — the
    single best imbalance picture the OBD read can give.
    """
    cells = [c for c in (cell_voltages or []) if isinstance(c, (int, float))]
    if len(cells) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    n = len(cells)
    mean = sum(cells) / n
    dev_mv = [(c - mean) * 1000.0 for c in cells]
    worst_mv = max(abs(d) for d in dev_mv)
    worst_pct = (worst_mv / 1000.0 / mean * 100.0) if mean else 0.0
    x = list(range(1, n + 1))

    def _col(d):
        a = abs(d)
        return '#2e7d32' if a <= 10 else ('#f9a825' if a <= 25 else '#c62828')
    bar_cols = [_col(d) for d in dev_mv]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(7.2, 4.2), sharex=True,
        gridspec_kw={'height_ratios': [1.15, 1]})

    ax1.bar(x, cells, color=bar_cols, width=0.9)
    lo, hi = min(cells), max(cells)
    pad = max((hi - lo) * 0.25, 0.01)
    ax1.set_ylim(lo - pad, hi + pad)
    ax1.axhline(mean, color='#1565c0', linestyle='--', linewidth=1,
                label=t('cert.cellchart_mean', default='Mittel {v} V',
                        v=f'{mean:.3f}'))
    ax1.set_ylabel('V')
    ax1.set_title(t('cert.cellchart_title',
                    default='Zellspannungen je Zelle ({n} Zellen)', n=n),
                  fontweight='bold', fontsize=10)
    ax1.grid(True, axis='y', alpha=0.3)
    ax1.legend(fontsize=7, loc='lower right')

    ax2.bar(x, dev_mv, color=bar_cols, width=0.9)
    ax2.axhline(0, color='#666', linewidth=0.8)
    ax2.set_ylabel('mV')
    ax2.set_xlabel(t('cert.cellchart_xlabel', default='Zelle'))
    ax2.set_title(t('cert.cellchart_dev_title',
                    default='Abweichung vom Mittel (max +/-{mv} mV / +/-{pct} %)',
                    mv=f'{worst_mv:.0f}', pct=f'{worst_pct:.2f}'),
                  fontweight='bold', fontsize=9)
    ax2.grid(True, axis='y', alpha=0.3)

    path = os.path.join(tmp_dir, 'cert_cells.png')
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


try:
    from fpdf import FPDF

    class _CertPDF(FPDF):
        @staticmethod
        def _clean(text):
            """latin-1 safe (fpdf core fonts can't encode arbitrary Unicode)."""
            return (str(text).replace('—', '-').replace('–', '-').replace('·', '-')
                    .replace('²', '2').replace('₂', '2').replace('Ø', 'O')
                    .replace('€', 'EUR ').replace('→', '->').replace('≥', '>=')
                    .replace('±', '+/-').replace('•', '-'))

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
    cell_chart = _cell_voltage_chart(
        (data.get('obd') or {}).get('cell_voltages'), tmp_dir)

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
        'measured': t('cert.src_measured', default='gemessen (Ladedaten, Coulomb-Zaehlung)'),
        'obd': t('cert.src_obd', default='BMS-Wert per OBD (ELM327)'),
        'bms': t('cert.src_bms', default='BMS-Wert des Fahrzeugs'),
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
    else:
        pdf.kv(t('cert.meas_soh', default='Gemessene SoH (Ladedaten)'),
               t('cert.meas_none', default='zu wenige breite Ladefenster (>= 25% SoC) fuer eine Messung'))
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

        # Per-cell voltage + deviation chart (one bar per cell).
        if cell_chart:
            if pdf.get_y() + 62 > pdf.h - 20:
                pdf.add_page()
            pdf.ln(1)
            pdf.image(cell_chart, x=(pdf.w - 170) / 2, w=170)
            pdf.ln(2)

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
