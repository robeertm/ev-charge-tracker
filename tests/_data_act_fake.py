# -*- coding: utf-8 -*-
"""Synthetic XPENG Data Act export in the layout the public viewers describe.

Not a real export: no real file was available. The layout (file names,
columns, units, placeholder values) follows the public format description,
so these files exercise the parser the way a real one will.
"""
import csv
import io
import zipfile

VIN = 'LXXXXTESTVIN000001'
T0 = 1_792_000_000          # a Tuesday in autumn 2026, UTC


def _row_status(t):
    return [VIN, 'G6', t, '20261010', 0, 0, 0, 0, 0, 0, 0, 0, 0, 250, 250, 250, 250]


STATUS_COLS = ['vin', 'vmodel', 'timer', 'ds', 'ldcu_driverdoorajarst',
               'rdcu_psngrdoorajarst', 'ldcu_rldoorajarst', 'rdcu_rrdoorajarst',
               'ldcu_flwinposstfb', 'frwinposstfb', 'ldcu_rlwinposstfb',
               'rrwinposstfb', 'rdm_tropenersts', 'ldcu_tpmsprfl',
               'ldcu_tpmsprfr', 'ldcu_tpmsprrl', 'ldcu_tpmsprrr']
OP_COLS = ['vin', 'vmodel', 'timer', 'ds', 'esp_vehspd', 'esp_vehlongaccel',
           'esp_vehlateralaccel', 'cdcu_totalodometer', 'eps_steeringangle',
           'eps_steeringanglespd', 'ldcu_accpedalsig', 'ldcu_brkpedalst',
           'ldcu_currentgearlev']
PW_COLS = ['vin', 'vmodel', 'timer', 'ds', 'ipuf_actrotspd', 'ipur_actrotspd',
           'ipuf_acttorq', 'ipur_acttorq', 'ipuf_rotoracttemp',
           'ipur_rotoracttemp', 'bms_battvolt', 'bms_battcurr', 'ldcu_chrgpwr',
           'ldcu_bms_soc_disp', 'ldcu_dstbatdisp_dynamic', 'bms_batttempmax_gb',
           'bms_batttempmin_gb', 'bms_celltempmaxnum_gb', 'bms_celltempminnum_gb']

# (kind, seconds, params)
DEFAULT_PLAN = [
    ('drive', 1200, dict(kmh=60)),          # 20 km
    ('sleep', 3 * 3600, {}),
    ('charge', 7200, dict(kw=7.4)),         # AC, 14.8 kWh
    ('sleep', 3600, {}),
    ('drive', 900, dict(kmh=100)),          # 25 km
    ('charge', 1200, dict(kw=100.0)),       # DC, 33.3 kWh
]


def build(plan=None, start=T0, odo=12000.0, soc=40.0, step=5):
    """Return ``(op_rows, pw_rows, status_rows, end_state)`` for a plan."""
    plan = plan or DEFAULT_PLAN
    op, pw, st = [], [], []
    t = start
    for kind, secs, p in plan:
        if kind == 'sleep':
            t += secs
            continue
        for s in range(0, secs, step):
            now = t + s
            if kind == 'drive':
                kmh = p['kmh']
                odo += kmh * step / 3600.0
                soc -= 0.0006 * kmh * step / 60.0
                op.append([VIN, 'G6', now, '20261010', kmh, 0, 0, round(odo, 1),
                           0, 0, 20, 0, 1])
                pw.append([VIN, 'G6', now, '20261010', 3000, 3000, 20, 20, 40, 40,
                           400.0, 60.0, 0.0, round(soc), 300.0, 25, 22, 14, 3])
            else:
                kw = p['kw']
                soc += kw * step / 3600.0 / 80.0 * 100.0
                op.append([VIN, 'G6', now, '20261010', 0, 0, 0, round(odo, 1),
                           0, 0, 0, 0, 4])
                pw.append([VIN, 'G6', now, '20261010', 0, 0, 0, 0, 40, 40,
                           400.0, round(-kw * 1000 / 400.0, 1), kw,
                           round(soc), 300.0, 25, 22, 14, 3])
            if s % 60 == 0:
                st.append(_row_status(now))
        t += secs
    return op, pw, st, dict(t=t, odo=odo, soc=soc)


def _csv(cols, rows):
    out = io.StringIO()
    w = csv.writer(out, lineterminator='\n')
    w.writerow(cols)
    w.writerows(rows)
    return out.getvalue().encode('utf-8')


def files(export_id='DA202610100001', plan=None, split_power=None, **kw):
    """``{file name: bytes}`` for one export."""
    op, pw, st, end = build(plan, **kw)
    base = '%s_dwd_opp_gdpr_veh_' % export_id
    out = {base + 'driving_status_di.csv': _csv(STATUS_COLS, st),
           base + 'driving_operation_di.csv': _csv(OP_COLS, op)}
    if split_power:
        a, b = pw[:split_power], pw[split_power:]
        out[base + 'driving_power_energy_di.csv'] = _csv(PW_COLS, a)
        out[base + 'driving_power_energy_di_part1.csv'] = _csv(PW_COLS, b)
    else:
        out[base + 'driving_power_energy_di.csv'] = _csv(PW_COLS, pw)
    return out


def zip_bytes(named):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for n, b in named.items():
            z.writestr(n, b)
    return buf.getvalue()
