# -*- coding: utf-8 -*-
"""The battery certificate for a pack whose BMS says nothing (LFP, no SoH).

A 2026 LFP XPENG G6 answers no SoH register and has no Ah/kWh counters. What
is left is the energy the home wallbox measured against the SoC lift. These
tests hold that estimate to what it may claim:

* under five usable charges nothing is stated;
* small lifts, unmeasured energy, partial readings do not count;
* it is never a measured SoH — "estimate" / ≈ in the PDF, the API and the page;
* a range that crosses the 70 % line passes nothing;
* it does NOT measure the setting (the app's learned loss is circular);
* what the BMS does not give is listed with a reason;
* a car with a real BMS SoH (Kia/NMC) gets exactly the old certificate.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from services import battery_analysis as BA  # noqa: E402

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')


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


def _g6(db, **kw):
    from models.database import Vehicle
    v = Vehicle(name='G6', brand='XPENG', model='G6', battery_kwh=80.0, battery_kwh_gross=80.8,
                battery_chemistry='lfp', is_archived=False, auto_sync=False,
                first_registered_at=date(2026, 3, 1), acquired_at=date(2026, 3, 1), **kw)
    db.session.add(v); db.session.commit()
    return v


def _charge(db, v, i, soc_from, soc_to, wall_kwh, *, reading=True, coverage=1.0,
            power_w=10500.0, undone=False, energy_on_reading=True, ctype='AC'):
    """One home charge. ``wall_kwh`` is what the wallbox meter saw; the entry
    gets its loss from the configured capacity exactly as the app does."""
    from models.database import Charge, WallboxCharge
    c = Charge(vehicle_id=v.id, date=date(2026, 4, 1) + timedelta(days=5 * i), charge_type=ctype,
               soc_from=soc_from, soc_to=soc_to, kwh_loaded=wall_kwh, eur_per_kwh=0.3,
               odometer=1000 + 300 * i)
    c.calculate_fields(v.battery_kwh, 0.88)
    db.session.add(c); db.session.flush()
    if reading:
        w = WallboxCharge(source_id='s%d' % i, device_key='wb', start_ts=10000 * i,
                          end_ts=10000 * i + 3600,
                          energy_kwh=wall_kwh if energy_on_reading else None,
                          coverage=coverage, avg_power_w=power_w, match_state='matched',
                          charge_id=c.id, vehicle_id=v.id,
                          undone_at=datetime(2026, 6, 1) if undone else None)
        db.session.add(w); db.session.flush()
        c.wallbox_charge_id = w.id
    db.session.commit()
    return c


def _good(db, v, n, true_cap=72.0, loss=0.12, start=0):
    """n charges of a pack holding ``true_cap`` kWh, charged at ``loss``."""
    lifts = [(20, 90), (15, 80), (30, 100), (25, 85), (10, 70), (20, 100), (35, 95), (15, 75)]
    for k in range(n):
        sf, st = lifts[k % len(lifts)]
        _charge(db, v, start + k, sf, st, round((st - sf) / 100.0 * true_cap / (1 - loss), 3))


def _pdf_text(vid, lang='de'):
    pymupdf = pytest.importorskip('pymupdf')
    pytest.importorskip('fpdf')
    import services.battery_cert_service as bc
    from services.i18n import set_language
    from services import units
    try:
        set_language(lang); units.set_unit('km'); units.set_currency('EUR')
        pdf = bc.generate_certificate(vid)
        assert pdf, 'no certificate'
        doc = pymupdf.open(stream=bytes(pdf), filetype='pdf')
        return doc, '\n'.join(p.get_text() for p in doc).replace('\xa0', ' ')
    finally:
        set_language('de')


# ── the pure estimate ─────────────────────────────────────────────────

def _row(sf, st, e, **kw):
    return dict({'date': date(2026, 5, 1), 'soc_from': sf, 'soc_to': st, 'energy_kwh': e,
                 'coverage': 1.0, 'avg_power_w': 11000.0, 'charge_type': 'AC'}, **kw)


def test_the_formula_and_the_range():
    # 60 points took 48.0 kWh at the wall → 80.0 kWh per 100 % → × 0.88 = 70.4
    est = BA.wallbox_capacity_estimate([_row(20, 80, 48.0)] * 5, 80.0, 80.8)
    assert est['state'] == 'ok' and est['is_estimate'] is True
    assert est['capacity_kwh'] == pytest.approx(70.4)
    # the range carries the loss band: 80 × 0.84 … 80 × 0.92
    assert est['capacity_lo_kwh'] == pytest.approx(67.2)
    assert est['capacity_hi_kwh'] == pytest.approx(73.6)
    assert est['soh_pct'] == 88.0 and est['soh_lo_pct'] == 84.0 and est['soh_hi_pct'] == 92.0
    assert est['verdict'] == 'pass'
    # "several percent" — the loss band alone is ±4.5 %
    assert est['uncertainty_pct'] == pytest.approx(4.5, abs=0.1)


@pytest.mark.parametrize('n,state', [(0, 'too_few'), (3, 'too_few'), (4, 'too_few'),
                                     (5, 'ok'), (8, 'ok')])
def test_nothing_is_stated_under_five_charges(n, state):
    est = BA.wallbox_capacity_estimate([_row(20, 80, 48.0)] * n, 80.0)
    assert est['state'] == state
    if state == 'too_few':
        assert 'capacity_kwh' not in est and 'soh_pct' not in est


def test_only_measured_wide_full_readings_count():
    rows = ([_row(20, 80, 48.0)] * 4
            + [_row(50, 70, 16.0)] * 3                    # 20-point lift
            + [_row(20, 80, None)] * 3                    # nobody measured
            + [_row(20, 80, 0.0)]                         # a zero is not a measurement
            + [_row(20, 80, 30.0, coverage=0.6)]          # the meter saw part of it
            + [_row(20, 80, 48.0, avg_power_w=1800.0)]    # granny charging loses more
            + [_row(20, 80, 40.0, charge_type='DC')])
    est = BA.wallbox_capacity_estimate(rows, 80.0)
    assert est['state'] == 'too_few' and est['n_qualifying'] == 4
    assert est['skipped'] == {'not_measured': 4, 'small_window': 3, 'partial': 1,
                              'low_power': 1, 'dc': 1, 'outlier': 0}
    # unknown coverage / power is not held against a reading
    est = BA.wallbox_capacity_estimate(
        [_row(20, 80, 48.0, coverage=None, avg_power_w=None)] * 5, 80.0)
    assert est['state'] == 'ok'


def test_a_range_across_the_line_passes_nothing():
    # medians around 72 % of 80 kWh, the loss band alone spans ±4.5 %
    rows = [_row(20, 80, e) for e in (38.5, 39.0, 39.3, 39.6, 40.0, 40.4)]
    est = BA.wallbox_capacity_estimate(rows, 80.0)
    assert est['soh_lo_pct'] < 70 <= est['soh_hi_pct']
    assert est['verdict'] == 'undecided'
    low = BA.wallbox_capacity_estimate([_row(20, 80, 33.0)] * 5, 80.0)
    assert low['soh_hi_pct'] < 70 and low['verdict'] == 'fail'


def test_more_than_new_is_not_graded():
    est = BA.wallbox_capacity_estimate([_row(20, 70, 52.0)] * 5, 80.0, 80.8)   # ≈ 91.5 kWh
    assert est['state'] == 'implausible' and 'soh_pct' not in est


# ── the certificate on a real database ────────────────────────────────

def test_lfp_without_soh_and_no_charges_gets_no_certificate(db_):
    import services.battery_cert_service as bc
    v = _g6(db_)
    d = bc.compute_battery_health(v.id)
    assert d['estimate']['state'] == 'too_few' and d['estimate']['n_qualifying'] == 0
    assert d['can_certify'] is False and d['certified_soh'] is None
    assert bc.generate_certificate(v.id) is None
    msg = bc.estimate_none_text(d)
    assert 'keinen SoH' in msg and 'mindestens 5' in msg and 'bisher 0' in msg


def test_lfp_with_three_charges_is_too_few(db_):
    import services.battery_cert_service as bc
    v = _g6(db_)
    _good(db_, v, 3)
    d = bc.compute_battery_health(v.id)
    assert d['estimate']['state'] == 'too_few' and d['estimate']['n_qualifying'] == 3
    assert not d['can_certify'] and bc.generate_certificate(v.id) is None
    assert 'bisher 3' in bc.estimate_none_text(d)


def test_lfp_with_eight_charges_is_certified_as_an_estimate(db_):
    import services.battery_cert_service as bc
    v = _g6(db_)
    _good(db_, v, 8, true_cap=72.0, loss=0.12)
    d = bc.compute_battery_health(v.id)
    est = d['estimate']
    assert est['state'] == 'ok' and est['n_used'] == 8
    # 🔑 the pack's own 72 kWh, not the configured 80: the loss of every row
    # was derived from 80 kWh by calculate_fields — using it would give 80.0
    assert est['capacity_kwh'] == pytest.approx(72.0, abs=0.05)
    assert d['measured'] is None and d['measured_circular'] is True
    assert d['certified_source'] == 'estimate' and d['is_estimate'] is True
    assert d['certified_soh'] == 90.0
    # graded by the lower end of the range (≈ 86 % → B), not by the 90 %
    assert est['soh_lo_pct'] == 86.0 and d['grade']['letter'] == 'B'
    assert d['vs_benchmark'] is None and d['expected_soh'] is None
    assert bc.estimate_none_text(d) is None


def test_small_lifts_and_unmeasured_energy_are_ignored_in_the_database(db_):
    import services.battery_cert_service as bc
    v = _g6(db_)
    _good(db_, v, 4)
    k = 10
    for sf, st in ((40, 60), (55, 80), (70, 95)):                 # < 30 points
        _charge(db_, v, k, sf, st, round((st - sf) / 100 * 72 / 0.88, 3)); k += 1
    _charge(db_, v, k, 20, 90, 57.3, reading=False); k += 1       # typed in, no meter
    _charge(db_, v, k, 20, 90, 57.3, energy_on_reading=False); k += 1
    _charge(db_, v, k, 20, 90, 57.3, undone=True); k += 1         # the owner took it back
    _charge(db_, v, k, 20, 90, 28.0, coverage=0.5); k += 1
    d = bc.compute_battery_health(v.id)
    est = d['estimate']
    assert est['state'] == 'too_few' and est['n_qualifying'] == 4
    assert est['skipped']['small_window'] == 3
    assert est['skipped']['not_measured'] == 3
    assert est['skipped']['partial'] == 1
    assert not d['can_certify']


def test_the_reading_counts_not_the_cars_own_figure(db_):
    """Matched but not taken over: kwh_loaded is still the car-side estimate
    (here: configured capacity × lift) — the meter's own reading is used."""
    import services.battery_cert_service as bc
    v = _g6(db_)
    _good(db_, v, 6, true_cap=72.0)
    from models.database import Charge
    for c in Charge.query.all():
        c.kwh_loaded = round(c.soc_charged / 100 * 80.0, 3)      # what the car-side path writes
    db_.session.commit()
    est = bc.compute_battery_health(v.id)['estimate']
    assert est['capacity_kwh'] == pytest.approx(72.0, abs=0.05)


def test_the_unavailable_list_names_reasons(db_):
    import services.battery_cert_service as bc
    v = _g6(db_)
    d = bc.compute_battery_health(v.id)
    reasons = {u['key']: u['reason'] for u in d['unavailable']}
    assert reasons == {'soh': 'known_silent', 'cells': 'not_supplied',
                       'spread': 'not_read', 'counters': 'not_supplied'}
    texts = dict(bc.describe_unavailable(d['unavailable']))
    assert 'liefert diesen Wert bei diesem Akku nicht' in texts['State of Health (SoH) vom Batteriemanagement']
    # once an OBD read with the min/max cells exists, the spread is no longer missing
    from models.database import ObdReading
    db_.session.add(ObdReading(vehicle_id=v.id, timestamp=datetime(2026, 10, 1, 9), profile='xpeng',
                               soc_bms_pct=55.0, cell_min_v=3.281, cell_max_v=3.290, cell_delta_mv=9.0))
    db_.session.commit()
    reasons = {u['key']: u['reason'] for u in bc.compute_battery_health(v.id)['unavailable']}
    assert reasons == {'soh': 'known_silent', 'cells': 'only_minmax', 'counters': 'not_supplied'}


def test_the_pdf_says_estimate_and_lists_what_is_missing(db_):
    v = _g6(db_)
    _good(db_, v, 8)
    doc, txt = _pdf_text(v.id)
    assert 'SoH ≈ 90 %' in txt                     # the stamp
    assert 'Schätzung' in txt and '≈ 90 %' in txt
    assert 'State of Health ≈ 90 % (Schätzung) – Note B' in txt
    assert 'Kapazitätsschätzung aus Wallbox-Energie' in txt
    assert '12 % (Spanne 8–16 %) – angenommen, nicht gemessen' in txt
    assert 'BESTANDEN (SCHÄTZUNG)' in txt
    assert 'Nicht verfügbar bei diesem Fahrzeug' in txt
    assert 'liefert diesen Wert bei diesem Akku nicht' in txt
    # LFP: flat curve, full charging, calibration
    assert 'Hinweise zur Zellchemie LFP' in txt
    assert 'Flache Spannungskurve' in txt and 'Kalibrierung' in txt
    assert '3.20 V / 3.65 V' in txt
    # the counter section asks for a measuring drive this car cannot do — gone
    assert 'Kapazitätsmessung über die BMS-Zähler' not in txt
    assert 'Messfahrt von etwa 100' not in txt
    # no measured figure, no fleet benchmark on an estimate
    assert 'Flottenmittel' not in txt
    assert 'Abstand zur Garantiegrenze'.upper() not in txt


def test_a_range_across_the_line_never_prints_passed(db_):
    v = _g6(db_)
    # 72 % of 80 kWh, scattered: the range straddles 70 %
    for i, e in enumerate((44.9, 45.4, 45.9, 46.3, 46.8, 47.2)):
        _charge(db_, v, i, 20, 90, e)
    import services.battery_cert_service as bc
    d = bc.compute_battery_health(v.id)
    assert d['estimate']['verdict'] == 'undecided' and d['can_certify']
    doc, txt = _pdf_text(v.id)
    lines = [ln.strip() for ln in txt.split('\n')]
    assert 'NICHT ENTSCHEIDBAR' in lines
    # no rating pill says passed (the prose may quote the word)
    assert not any(ln.startswith('BESTANDEN') for ln in lines)
    assert 'schneidet die Garantiegrenze' in txt


def test_a_degraded_pack_is_not_printed_as_measured(db_):
    """Wall energy below ΔSoC × configured capacity: calculate_fields fills
    the loss from the learned efficiency, not from the wall. That used to come
    out as "gemessen (Coulomb-Zählung) 68.7 %" with a hard UNTER GRENZE."""
    import services.battery_cert_service as bc
    from models.database import Charge
    v = _g6(db_)
    for i, e in enumerate((44.9, 45.4, 45.9, 46.3, 46.8, 47.2)):
        _charge(db_, v, i, 20, 90, e)
    # the precondition: every loss is the efficiency fallback, not the wall
    for c in Charge.query.all():
        assert c.loss_kwh == pytest.approx(0.70 * 80.0 * (1 / 0.88 - 1), abs=0.001)
    d = bc.compute_battery_health(v.id)
    assert d['measured'] is None and d['certified_source'] == 'estimate'
    doc, txt = _pdf_text(v.id)
    assert 'Coulomb' not in txt and 'UNTER GRENZE' not in txt


def test_the_estimate_is_marked_in_every_language(db_):
    from services.i18n import set_language, t
    v = _g6(db_)
    _good(db_, v, 8)
    for lang in LANGS:
        doc, txt = _pdf_text(v.id, lang)
        set_language(lang)
        try:
            word, na = t('cert.est_word'), t('cert.sec_unavail')
        finally:
            set_language('de')
        assert word in txt and '≈ 90 %' in txt, lang
        assert na in txt, lang


@pytest.mark.parametrize('lang', LANGS)
def test_the_lfp_report_has_no_overlapping_text(db_, lang):
    pymupdf = pytest.importorskip('pymupdf')
    from test_cert_no_overlap import _overlaps
    from services.i18n import t, set_language
    v = _g6(db_)
    _good(db_, v, 8)
    doc, _ = _pdf_text(v.id, lang)
    set_language(lang)
    try:
        for i, p in enumerate(doc):
            assert t('cert.page_n', p=i + 1, n=len(doc)) in p.get_text(), (lang, i)
    finally:
        set_language('de')
    bad = {i + 1: _overlaps(p) for i, p in enumerate(doc)}
    assert not any(bad.values()), {k: v for k, v in bad.items() if v}


# ── nothing changes for a car with a real BMS SoH ─────────────────────

def _niro(db):
    from test_cert_wasserdicht import _niro as niro
    v = niro(db)
    _good(db, v, 8, true_cap=60.0)          # metered home charges as well
    return v


def test_a_kia_with_a_bms_soh_takes_none_of_the_new_paths(db_):
    import services.battery_cert_service as bc
    v = _niro(db_)
    d = bc.compute_battery_health(v.id)
    assert d['certified_source'] == 'obd' and d['certified_soh'] == 93.0
    assert d['grade']['letter'] == 'A'
    assert d['estimate'] is None and d['unavailable'] == [] and d['is_estimate'] is False
    doc, txt = _pdf_text(v.id)
    for new in ('Schätzung', 'Nicht verfügbar bei diesem Fahrzeug', 'Wallbox-Energie',
                'Zellchemie LFP', 'SoH ≈'):
        assert new not in txt, new
    # the section order of the old certificate
    heads = [ln for ln in txt.split('\n') if '  ·  ' in ln]
    assert [h.split('  ·  ')[1] for h in heads] == [
        'Zusammenfassung', 'Prüfpunkte', 'Kapazitätsmessung über die BMS-Zähler', 'SoH-Bestimmung',
        'Die Zellen', 'Netto, Brutto und der Puffer', 'Strombedarf für eine volle Ladung über die Jahre',
        'Nutzung & Ladeverhalten', 'Datengrundlage & Methodik']
    assert 'State of Health 93.0 % – Note A' in txt


def test_a_kia_with_only_the_cloud_soh_is_untouched(db_):
    import services.battery_cert_service as bc
    from models.database import Vehicle, VehicleSync
    v = Vehicle(name='Kona', brand='Kia', battery_kwh=64.0, battery_chemistry='nmc622',
                is_archived=False, auto_sync=False)
    db_.session.add(v); db_.session.commit()
    db_.session.add(VehicleSync(vehicle_id=v.id, timestamp=datetime(2026, 5, 1, 8), soc_percent=70,
                                odometer_km=30000, battery_soh_percent=95.5))
    db_.session.commit()
    _good(db_, v, 8, true_cap=58.0)
    d = bc.compute_battery_health(v.id)
    assert d['certified_source'] == 'bms' and d['certified_soh'] == 95.5
    assert d['estimate'] is None and d['unavailable'] == []


# ── API and web page, on the real app ─────────────────────────────────

_SCRIPT = r"""
import json, sys
sys.path.insert(0, %r)
from datetime import date, datetime, timedelta
from app import create_app
from models.database import db, Vehicle, Charge, WallboxCharge
app = create_app()
c = app.test_client()
out = {}
with app.app_context():
    def g6(name):
        v = Vehicle(name=name, brand='XPENG', battery_kwh=80.0, battery_kwh_gross=80.8,
                    battery_chemistry='lfp', is_archived=False, auto_sync=False)
        db.session.add(v); db.session.commit(); return v
    full, few = g6('Voll'), g6('Wenig')
    for v, n in ((full, 8), (few, 3)):
        for i in range(n):
            sf, st = 20, 90
            e = round(0.70 * 72.0 / 0.88, 3)
            ch = Charge(vehicle_id=v.id, date=date(2026, 4, 1) + timedelta(days=4 * i), charge_type='AC',
                        soc_from=sf, soc_to=st, kwh_loaded=e)
            ch.calculate_fields(80.0, 0.88); db.session.add(ch); db.session.flush()
            w = WallboxCharge(source_id='%%d-%%d' %% (v.id, i), device_key='wb', start_ts=i * 9999,
                              end_ts=i * 9999 + 3600, energy_kwh=e, coverage=1.0, avg_power_w=11000.0,
                              match_state='matched', charge_id=ch.id, vehicle_id=v.id)
            db.session.add(w); db.session.flush(); ch.wallbox_charge_id = w.id
    db.session.commit()
    ids = (full.id, few.id)
r = c.get('/api/vehicles/%%d/battery-health' %% ids[0]); out['api_full'] = r.get_json()
r = c.get('/api/vehicles/%%d/battery-health' %% ids[1]); out['api_few'] = r.get_json()
r = c.get('/obd?vehicle=%%d' %% ids[0]); out['obd_full'] = r.get_data(as_text=True)
r = c.get('/obd?vehicle=%%d' %% ids[1]); out['obd_few'] = r.get_data(as_text=True)
r = c.get('/vehicles/%%d/certificate.pdf' %% ids[1], follow_redirects=True)
out['flash_few'] = r.get_data(as_text=True)
r = c.get('/vehicles/%%d/certificate.pdf' %% ids[0]); out['pdf_type'] = r.headers.get('Content-Type')
print(json.dumps(out))
"""


@pytest.fixture(scope='module')
def web():
    with tempfile.TemporaryDirectory() as data_dir:
        env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='lfp-test')
        proc = subprocess.run([sys.executable, '-c', _SCRIPT % ROOT], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=240)
        assert proc.returncode == 0, proc.stderr[-3000:]
        line = [ln for ln in proc.stdout.strip().split('\n') if ln.startswith('{')][-1]
        yield json.loads(line)


def test_the_api_marks_the_estimate(web):
    a = web['api_full']
    assert a['ok'] and a['is_estimate'] is True and a['kind'] == 'estimate'
    assert a['certified_source'] == 'estimate' and a['label'] == 'Schätzung'
    assert a['grade']['basis'] == 'estimate_range_low'
    assert a['estimate']['state'] == 'ok' and a['estimate']['is_estimate'] is True
    assert a['estimate']['capacity_kwh'] == pytest.approx(72.0, abs=0.05)
    assert a['bms_soh'] is None and a['obd_soh'] is None          # unmeasured = null, never 0
    keys = {u['key']: u for u in a['unavailable']}
    assert keys['soh']['reason'] == 'known_silent' and keys['soh']['reason_text']
    b = web['api_few']
    assert b['can_certify'] is False and b['certified_soh'] is None and b['kind'] is None
    assert b['estimate']['state'] == 'too_few' and 'capacity_kwh' not in b['estimate']
    assert 'bisher 3' in b['estimate_note']


def test_the_battery_page_marks_the_estimate_and_the_gaps(web):
    html = web['obd_full']
    assert 'id="obdCertBadge"' in html and '≈ 90 %' in html and 'Schätzung' in html
    assert 'id="obdCertEstimate"' in html and 'Spanne 86–' in html
    assert 'id="obdCertUnavailable"' in html and 'Nicht verfügbar bei diesem Fahrzeug' in html
    few = web['obd_few']
    assert 'Noch kein Zertifikat.' in few and 'bisher 3' in few
    # not sent to an OBD dongle that cannot read a SoH from this pack
    assert 'Lies die Batterie oben mit dem OBD-Dongle aus' not in few


def test_the_refused_certificate_says_why(web):
    assert 'bisher 3' in web['flash_few']
    # the certified car must get a real PDF; without the PDF library the route
    # falls back to the settings page, which is an environment gap, not a bug
    pytest.importorskip('fpdf', reason='fpdf2 not installed: certificate PDF cannot be rendered')
    assert web['pdf_type'] == 'application/pdf'
