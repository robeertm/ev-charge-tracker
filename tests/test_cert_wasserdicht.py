# -*- coding: utf-8 -*-
"""Every calculation of the battery certificate, held to what it may claim
(v3.0.174).

An audit of the whole certificate found numbers that were computed right
but claimed too much, and a few that were plainly wrong. Each test below
pins one of them. The real Kia Niro frames come from test_obd_zaehler.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from services import battery_analysis as BA  # noqa: E402
from services.vehicle import obd_decode as od  # noqa: E402
from test_obd_zaehler import FULL, LOW, _dec, _as_read  # noqa: E402


@pytest.fixture()
def db_():
    from flask import Flask
    from models.database import db
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield db
        db.session.remove()


def _pdf_text(vid, lang='de'):
    pymupdf = pytest.importorskip('pymupdf')
    pytest.importorskip('fpdf')
    import services.battery_cert_service as bc
    from services.i18n import set_language
    from services import units
    try:
        set_language(lang); units.set_unit('km'); units.set_currency('EUR')
        pdf = bytes(bc.generate_certificate(vid))
    finally:
        set_language('de')
    return '\n'.join(p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf')).replace('\xa0', ' ')


def _niro(db, **kw):
    from models.database import Vehicle, ObdReading
    v = Vehicle(name='Niro', battery_kwh=64.0, battery_kwh_gross=67.5, battery_chemistry='nmc622',
                battery_cells_series=98, is_archived=False, auto_sync=False, **kw)
    db.session.add(v); db.session.commit()
    for ts, raw, disp in ((datetime(2026, 10, 15, 17, 16), FULL, 97.5),
                          (datetime(2026, 10, 18, 11, 11), LOW, 26.0)):
        d = _dec(raw)
        db.session.add(ObdReading(
            vehicle_id=v.id, timestamp=ts, profile='kia_hyundai_ext', soh_pct=93.0,
            soc_display_pct=disp, raw_json=json.dumps({'220101': raw}),
            **{k: d[k] for k in ('soc_bms_pct', 'pack_voltage_v', 'cumulative_charge_ah',
                                 'cumulative_discharge_ah', 'cumulative_charge_kwh',
                                 'cumulative_discharge_kwh')}))
    db.session.commit()
    return v


# ── decoding ──────────────────────────────────────────────────────────

def test_a_dropped_can_frame_drops_the_read_instead_of_shifting_it():
    """Without line '7EC 24' every byte behind it moved: 424 million Ah."""
    broken = '\r'.join(l for l in FULL.split('\r') if not l.startswith('7EC 24'))
    assert od.reassemble(broken, expect='220101') is None
    d = od.decode('kia_hyundai_ext', {'220101': broken})
    assert d.get('cumulative_charge_ah') is None


def test_a_frame_printed_twice_is_read_once():
    lines = FULL.split('\r')
    i = next(k for k, l in enumerate(lines) if l.startswith('7EC 23'))
    doubled = '\r'.join(lines[:i + 1] + [lines[i]] + lines[i + 1:])
    assert _dec(doubled)['cumulative_charge_ah'] == pytest.approx(58448.0)


def test_counters_that_cannot_be_true_are_refused():
    good = _as_read(FULL, '2026-10-15T17:16:23', 97.5)
    assert BA.counters_plausible(good)
    shifted = dict(good, cumulative_charge_ah=424463649.8)
    assert not BA.counters_plausible(shifted)


# ── the capacity from the BMS counters ────────────────────────────────

def _mid(a, b, ts, frac):
    """A read part-way along the drive from a to b."""
    out = dict(a, timestamp=ts)
    for k in ('soc_bms_pct', 'cumulative_charge_ah', 'cumulative_discharge_ah',
              'cumulative_charge_kwh', 'cumulative_discharge_kwh', 'pack_voltage_v'):
        out[k] = round(a[k] + (b[k] - a[k]) * frac, 1)
    return out


def test_one_drive_read_three_times_is_one_measurement_the_widest():
    a = _as_read(FULL, '2026-10-15T17:16:00', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:00', 26.0)
    mid = _mid(a, b, '2026-10-17T09:00:00', 0.5)
    m = BA.obd_capacity([a, mid, b])
    assert len(m) == 1
    assert m[0]['soc_from'] == 94.5 and m[0]['soc_to'] == 25.5


def test_a_second_snapshot_half_a_point_off_is_not_a_second_drive():
    a = _as_read(FULL, '2026-10-15T17:16:00', 97.5)
    a2 = dict(a, timestamp='2026-10-15T17:26:00', soc_bms_pct=94.0)
    b = _as_read(LOW, '2026-10-18T11:11:00', 26.0)
    assert len(BA.obd_capacity([a, a2, b])) == 1


def test_a_charge_between_the_reads_is_no_measurement():
    a = _as_read(FULL, '2026-10-15T17:16:00', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:00', 26.0)
    b['cumulative_charge_ah'] += 100.0           # 100 Ah charged in between
    assert BA.obd_capacity([a, b]) == []


def test_the_worked_calculation_adds_up_line_by_line():
    a = _as_read(FULL, '2026-10-15T17:16:00', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:00', 26.0)
    m = BA.obd_capacity([a, b])[0]
    assert round(m['ah_out'] - m['ah_in'], 1) == m['ah'] == 131.5
    assert round(m['kwh_out'] - m['kwh_in'], 1) == m['kwh'] == 47.8
    assert m['capacity_ah'] == round(m['ah'] / m['dsoc'] * 100, 1) == 190.6


def test_without_energy_counters_kwh_never_comes_from_one_endpoint():
    a = _as_read(FULL, '2026-10-15T17:16:00', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:00', 26.0)
    for r in (a, b):
        r.pop('cumulative_charge_kwh'); r.pop('cumulative_discharge_kwh')
    b['pack_voltage_v'] = None
    m = BA.obd_capacity([a, b])[0]
    assert m['capacity_kwh'] is None and m['kwh_basis'] is None
    m = BA.obd_capacity([a, b], 3.63, 98)[0]
    assert m['kwh_basis'] == 'nominal' and not m['kwh_counted']


def test_the_real_drive_contradicting_the_bms_soh_never_passes(db_):
    """The real drive: 69.3 kWh per 100 % BMS on a 64 / 67.5 kWh pack with
    BMS SoH 93 % — printed as 'bestanden' until v3.0.173. 93 % of the pack is
    59.5 (net) to 62.8 kWh (gross); +10.4 % off is outside the drive's ±4.9 %."""
    import services.battery_cert_service as bc
    v = _niro(db_)
    h = bc.compute_battery_health(v.id)
    chk = h['capacity_check']
    assert chk['state'] == 'conflict'
    assert chk['expect_lo'] == pytest.approx(64.0 * 0.93) and chk['expect_hi'] == pytest.approx(67.5 * 0.93)
    assert chk['tol_pct'] == 4.9 and round(chk['dev_pct'], 1) == 10.4
    text = _pdf_text(v.id)
    row = text[text.index('Kapazität aus den BMS-Zählern\n'):text.index('Die Soll-Werte')]
    assert 'NICHT PLAUSIBEL' in row and 'BESTANDEN' not in row
    assert 'kein Kapazitätsnachweis' in text and '+10.4 %' in text and '±4.9 %' in text
    assert h['certified_soh'] == 93.0                  # the grade is the SoH register


def test_above_new_respects_the_drives_own_tolerance():
    import services.battery_cert_service as bc
    m = {'capacity_kwh': 70.9, 'dsoc': 30.0, 'kwh_counted': True}     # tol 8.7 %, +5 % over gross
    assert bc._capacity_check(m, 64.0, 67.5, 100.0)['state'] == 'ok'
    m = {'capacity_kwh': 75.0, 'dsoc': 69.0, 'kwh_counted': True}     # +11 %, tol 4.9 %
    assert bc._capacity_check(m, 64.0, 67.5, None)['state'] == 'above_new'


def test_a_bms_referring_to_the_net_window_is_no_conflict():
    import services.battery_cert_service as bc
    m = {'capacity_kwh': 60.8, 'dsoc': 69.0, 'kwh_counted': True}     # 64 × 0.95
    assert bc._capacity_check(m, 64.0, 67.5, 95.0)['state'] == 'ok'


def test_gross_without_net_does_not_crash_the_pdf(db_):
    v = _niro(db_)
    v.battery_kwh = None
    db_.session.commit()
    assert 'Kapazitätsmessung' in _pdf_text(v.id)


def test_a_charge_stop_splits_the_drive_in_two():
    a = _as_read(FULL, '2026-10-15T08:00:00', 97.5)                    # 94.5 %
    b = _as_read(LOW, '2026-10-18T20:00:00', 26.0)                     # 25.5 %
    low1 = _mid(a, b, '2026-10-16T08:00:00', 0.80)                     # ~39 %
    up = dict(low1, timestamp='2026-10-16T12:00:00', soc_bms_pct=60.0,
              cumulative_charge_ah=low1['cumulative_charge_ah'] + 40.0)
    end = dict(b, cumulative_charge_ah=b['cumulative_charge_ah'] + 40.0)
    m = BA.obd_capacity([a, low1, up, end])
    assert [(x['soc_from'], x['soc_to']) for x in m] == [(94.5, low1['soc_bms_pct']), (60.0, 25.5)]


def test_a_reply_cut_short_at_the_end_keeps_its_front_fields():
    cut = '\r'.join(l for l in FULL.split('\r') if not l.startswith('7EC 28'))
    d = _dec(cut)
    assert d['soc_bms_pct'] == 94.5 and d['cumulative_charge_ah'] == pytest.approx(58448.0)


def test_one_soc_point_is_computed_not_a_fixed_one_and_a_half(db_):
    v = _niro(db_)
    text = _pdf_text(v.id)
    assert 'um 1.4 % (100 ÷ 69.0 Punkte)' in text
    assert 'rund 1,5 %' not in text


# ── SoH sources ───────────────────────────────────────────────────────

def test_another_cars_baseline_never_scales_this_car(db_):
    from models.database import Vehicle, VehicleSync
    from services.stats_service import get_vehicle_history
    ev6 = Vehicle(name='EV6', battery_kwh=74.0, battery_soh_baseline=125, is_archived=False)
    niro = Vehicle(name='Niro', battery_kwh=64.0, is_archived=False)
    db_.session.add_all([ev6, niro]); db_.session.commit()
    db_.session.add(VehicleSync(vehicle_id=niro.id, timestamp=datetime(2026, 1, 1), battery_soh_percent=92.0))
    db_.session.commit()
    assert get_vehicle_history(vehicle_id=niro.id)['summary']['last']['soh'] == 92.0   # not 73.6


def test_another_cars_capacity_is_never_borrowed(db_):
    from models.database import Vehicle
    from services.stats_service import _vehicle_field
    skoda = Vehicle(name='Enyaq', battery_kwh=77.0, is_archived=False)
    kia = Vehicle(name='Niro', battery_kwh=None, is_archived=False)
    db_.session.add_all([skoda, kia]); db_.session.commit()
    assert _vehicle_field(kia.id, 'battery_kwh', 'battery_kwh', 0.0) == 0.0


def test_the_last_sync_without_soh_does_not_hide_the_bms_value(db_):
    from models.database import Vehicle, VehicleSync
    import services.battery_cert_service as bc
    v = Vehicle(name='Niro', battery_kwh=64.0, is_archived=False)
    db_.session.add(v); db_.session.commit()
    for i, soh in enumerate((96.0, 95.0, None)):
        db_.session.add(VehicleSync(vehicle_id=v.id, timestamp=datetime(2026, 1, 1 + i), battery_soh_percent=soh))
    db_.session.commit()
    bms = bc.compute_battery_health(v.id)['bms']
    assert bms['current'] == 95.0 and bms['delta'] == -1.0


def test_a_relative_cloud_value_above_100_is_never_the_headline(db_):
    from models.database import Vehicle, VehicleSync
    import services.battery_cert_service as bc
    v = Vehicle(name='Ioniq', battery_kwh=74.0, is_archived=False)
    db_.session.add(v); db_.session.commit()
    db_.session.add(VehicleSync(vehicle_id=v.id, timestamp=datetime(2026, 1, 1), battery_soh_percent=104.3))
    db_.session.commit()
    h = bc.compute_battery_health(v.id)
    assert h['certified_soh'] is None and not h['can_certify']


def test_a_broken_obd_read_is_never_the_one_printed(db_):
    from models.database import ObdReading
    import services.battery_cert_service as bc
    v = _niro(db_)
    db_.session.add(ObdReading(vehicle_id=v.id, timestamp=datetime(2026, 10, 18, 12, 0),
                               profile='kia_hyundai_ext', soh_pct=3481.9,
                               cell_voltages_json=json.dumps([3.82] * 95 + [5.1])))
    db_.session.commit()
    h = bc.compute_battery_health(v.id)
    assert h['obd']['soh_pct'] == 93.0 and h['obd_soh'] == 93.0
    assert '3481' not in _pdf_text(v.id)


def test_estimated_and_loss_less_charges_are_no_measurement(db_):
    from models.database import Vehicle, Charge
    import services.battery_cert_service as bc
    v = Vehicle(name='Niro', battery_kwh=64.0, is_archived=False)
    db_.session.add(v); db_.session.commit()
    for i in range(12):
        # auto-detected: kWh = 60 % × 64 / 0.9, loss derived from the capacity
        db_.session.add(Charge(vehicle_id=v.id, date=date(2025, 1, 1) + timedelta(days=i * 7),
                               charge_type='AC', soc_from=20, soc_to=80, kwh_loaded=42.67,
                               loss_kwh=42.67 - 38.4, notes='Automatisch erkannt (keine App-Session)'))
        # metered at the wall, but no loss logged: wall energy ≠ battery energy
        db_.session.add(Charge(vehicle_id=v.id, date=date(2025, 1, 2) + timedelta(days=i * 7),
                               charge_type='AC', soc_from=20, soc_to=80, kwh_loaded=40.0 + i * 0.3))
    db_.session.commit()
    assert bc._measured_capacity(v.id, 64.0) is None


def test_the_headline_measurement_is_the_recent_state(db_):
    from models.database import Vehicle, Charge
    import services.battery_cert_service as bc
    v = Vehicle(name='Niro', battery_kwh=64.0, is_archived=False)
    db_.session.add(v); db_.session.commit()
    d0 = date(2022, 1, 1)
    for i in range(48):                     # four years, monthly, 64 → 59 kWh
        cap = 64.0 - 5.0 * i / 47
        db_.session.add(Charge(vehicle_id=v.id, date=d0 + timedelta(days=30 * i), charge_type='AC',
                               soc_from=20, soc_to=80, kwh_loaded=round(cap * 0.6 * 1.12, 2),
                               loss_kwh=round(cap * 0.6 * 0.12, 2)))
    db_.session.commit()
    m = bc._measured_capacity(v.id, 64.0)
    assert m['recent_only'] and m['capacity_kwh'] < 60.5        # not the lifetime median ~61.5


# ── wording and figures ───────────────────────────────────────────────

def test_differences_of_percentages_are_points(db_):
    v = _niro(db_, first_registered_at=date(2020, 3, 1))
    text = _pdf_text(v.id)
    assert '+23.0 Pkt' in text                         # 93.0 − 70 to the warranty line
    assert '+23.0 %' not in text


def test_no_age_without_first_registration_and_no_benchmark(db_):
    import services.battery_cert_service as bc
    v = _niro(db_, acquired_at=date(2024, 6, 1))
    h = bc.compute_battery_health(v.id)
    assert h['age_years'] is None and h['vs_benchmark'] is None


def test_capacity_picture_never_prints_a_negative_buffer():
    pic = BA.capacity_picture(64.8, 67.3, window_change_pct=0.0, obd_soh=93.0)
    assert pic.get('buffer_conflict') and 'buffer_left_kwh' not in pic
    pic = BA.capacity_picture(64.0, 67.5, window_change_pct=None, obd_soh=93.0)
    assert 'buffer_used_pct' not in pic                # no window measured, no verdict


def test_window_comparisons_over_different_spans_are_not_averaged():
    s = []
    for per, n, val in (('2022H1', 10, 100.0), ('2026H1', 10, 92.0)):
        s += [(per, 'AC', val)] * n
    for per, n, val in (('2025H2', 40, 100.0), ('2026H2', 40, 99.5)):
        s += [(per, 'DC', val)] * n
    w = BA.window_trend(s)
    assert w['change_pct'] == -8.0                     # the four-year comparison alone
    assert [c['combined'] for c in w['comparisons']] == [True, False]


def test_one_bms_step_is_not_an_outlier():
    st = BA.cell_stats([3.80] * 90 + [3.82] * 8, resolution_mv=20)
    assert st['outliers'] == []
    st = BA.cell_stats([3.82] * 96 + [3.84, 3.80], resolution_mv=20)
    assert st['outliers'] == []
    st = BA.cell_stats([3.82] * 97 + [3.76], resolution_mv=20)
    assert st['outliers'] == [98]


def test_the_types_table_adds_up_as_printed(db_):
    from models.database import Charge
    v = _niro(db_)
    for typ, kwh in (('AC', 100.4), ('DC', 200.4), ('PV', 0.4)):
        db_.session.add(Charge(vehicle_id=v.id, date=date(2026, 1, 1), charge_type=typ,
                               soc_from=20, soc_to=30, kwh_loaded=kwh))
    db_.session.commit()
    text = _pdf_text(v.id)
    t = text[text.index('Geladen nach Art'):]
    assert '100.4 kWh' in t and '200.4 kWh' in t and '0.4 kWh' in t and '301.2 kWh' in t
    # 33.33 / 66.53 / 0.13 %: rounded each on its own they sum to 99.9; by
    # largest remainder the tenth goes to the biggest remainder → 100.0
    assert '33.3 %' in t and '66.6 %' in t and '0.1 %' in t
    g = text[text.index('SCHNELLLADEANTEIL (DC)'):]
    assert g.split('\n')[1] == '66.6 %'               # the grid says what the table says
