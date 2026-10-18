"""Battery analysis (v3.0.154) — the window, the buffer, the cells.

The numbers in here are not invented to make the code pass: the seasonal
pattern and the circular coulomb count are the ones measured on a real
Niro EV with four years of metered charges (AC ~73 kWh per displayed 100 %
in the second half-year, ~76-77 kWh in the first; 88 % of all coulomb
samples exactly the configured 64.00 kWh).
"""
import json
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import battery_analysis as BA  # noqa: E402


def _charge(d, typ, soc_from, soc_to, kwh, **kw):
    return SimpleNamespace(date=d, charge_type=typ, soc_from=soc_from, soc_to=soc_to,
                           kwh_loaded=kwh, loss_kwh=kw.get('loss'), notes=kw.get('notes', ''),
                           needs_review=kw.get('needs_review', False),
                           wallbox_charge_id=kw.get('wallbox'))


def _season_fleet(fade_per_year=0.0):
    """Two charges a month for three years, AC at home: 76.5 kWh per 100 %
    in the first half-year, 73.0 in the second — the measured season — and
    an optional real fade on top."""
    out = []
    for year in (2023, 2024, 2025):
        factor = 1 - fade_per_year * (year - 2023)
        for month in range(1, 13):
            base = 76.5 if month <= 6 else 73.0
            for day in (5, 20):
                per100 = base * factor
                out.append(_charge(date(year, month, day), 'AC', 20, 80, per100 * 0.6))
    return out


# ── 1. which charges count ─────────────────────────────────────────────
def test_an_auto_detected_estimate_is_not_a_measurement():
    est = _charge(date(2024, 1, 1), 'AC', 20, 80, 43.64,
                  notes='Automatisch erkannt (keine App-Session) — bitte prüfen')
    assert not BA.is_metered(est)


def test_a_wallbox_reading_counts_even_when_detected_automatically():
    c = _charge(date(2024, 1, 1), 'AC', 20, 80, 44.0,
                notes='Automatisch erkannt · an der Wallbox gemessen', wallbox=7)
    assert BA.is_metered(c)


def test_a_value_equal_to_capacity_over_efficiency_is_an_estimate_whatever_its_note():
    # 64 kWh / 0.88 = 72.727 per 100 % → over a 60 % window 43.636 kWh.
    c = _charge(date(2024, 1, 1), 'AC', 20, 80, 64.0 * 0.6 / 0.88)
    assert not BA.is_metered(c, (64.0,), (0.88,))


# ── 2. the window over the years ───────────────────────────────────────
def test_the_season_alone_is_not_read_as_ageing():
    t = BA.window_trend(BA.window_samples(_season_fleet()))
    assert t['change_pct'] == 0.0
    # Without the season match, January vs July would have claimed -4.6 %.
    assert {c['half'] for c in t['comparisons']} == {'H1', 'H2'}


def test_a_real_fade_is_found_in_every_season():
    t = BA.window_trend(BA.window_samples(_season_fleet(fade_per_year=0.02)))
    assert -4.2 < t['change_pct'] < -3.8          # 2 %/yr over 2 years
    assert t['agreement_pct'] < 0.5


def test_too_few_charges_give_no_verdict():
    few = [c for c in _season_fleet() if c.date.day == 5][:10]
    t = BA.window_trend(BA.window_samples(few))
    assert t['change_pct'] is None


def test_narrow_windows_do_not_count():
    narrow = [_charge(date(2024, m, 1), 'AC', 50, 60, 7.3) for m in range(1, 13)]
    assert BA.window_samples(narrow) == []


# ── 3. net, gross, buffer ──────────────────────────────────────────────
def test_the_measured_niro_buffer_picture():
    # Window stable (-0.6 %), BMS register 94.7 %, 64 net / 67.5 gross.
    p = BA.capacity_picture(64.0, 67.5, -0.63, 94.7)
    assert p['buffer_kwh'] == 3.5 and p['buffer_pct'] == 5.19
    assert p['soh_high'] == 99.4 and p['soh_low'] == 94.2
    assert p['gross_now_kwh'] == 63.92
    assert p['buffer_used_pct'] >= 85            # almost spent


def test_without_gross_there_is_no_range_and_no_buffer_claim():
    p = BA.capacity_picture(64.0, None, -0.63, 94.7)
    assert 'soh_low' not in p and 'buffer_used_pct' not in p
    assert p['soh_high'] == 99.4


def test_an_unchanged_window_never_reads_above_100():
    assert BA.capacity_picture(64.0, 67.5, +2.0)['soh_high'] == 100.0


# ── 4. capacity from the BMS counters ──────────────────────────────────
def _read(ts, soc, chg, dis, v=380.0, **kw):
    return dict(timestamp=ts, soc_bms_pct=soc, cumulative_charge_ah=chg,
                cumulative_discharge_ah=dis, pack_voltage_v=v, **kw)


def test_two_reads_far_apart_give_a_capacity_without_any_loss_assumption():
    a = _read('2026-10-14T08:00:00', 95.0, 21000.0, 20100.0, 405.0)
    b = _read('2026-10-14T18:00:00', 25.0, 21000.0, 20100.0 + 120.4, 365.0)
    m = BA.obd_capacity([b, a])                    # order does not matter
    assert len(m) == 1
    assert m[0]['capacity_ah'] == 172.0            # 120.4 Ah / 70 points
    assert m[0]['capacity_kwh'] == 66.2            # x mean 385 V


def test_reads_too_close_in_soc_or_too_far_in_time_give_nothing():
    a = _read('2026-08-08T19:04:00', 66.0, 20993.6, 20028.9)
    b = _read('2026-08-08T19:35:00', 64.0, 20993.6, 20029.0)
    assert BA.obd_capacity([a, b]) == []
    c = _read('2026-09-30T08:00:00', 20.0, 20993.6, 20200.0)
    assert BA.obd_capacity([a, c]) == []


def test_counters_and_soc_that_disagree_on_direction_are_refused():
    a = _read('2026-10-14T08:00:00', 90.0, 21000.0, 20100.0)
    b = _read('2026-10-14T12:00:00', 30.0, 21050.0, 20100.0)   # "charged" while SoC fell
    assert BA.obd_capacity([a, b]) == []


def test_the_buffer_view_marks_a_full_read_with_its_head_room():
    rows = BA.buffer_view([dict(timestamp='x', soc_display_pct=100.0, soc_bms_pct=95.5,
                                cell_max_v=4.18)], max_cell_v=4.2)
    assert rows[0]['top'] and rows[0]['headroom_mv'] == 20 and rows[0]['gap'] == 4.5


# ── 5. the cells ───────────────────────────────────────────────────────
def test_an_even_pack_is_reported_as_even_not_as_noise():
    s = BA.cell_stats([3.82] * 98)
    assert s['all_equal'] and s['spread_mv'] == 0 and s['outliers'] == []


def test_a_weak_cell_is_named_by_its_number():
    cells = [4.16] * 98
    cells[40] = 4.10
    s = BA.cell_stats(cells)
    assert s['min_cells'] == [41] and s['outliers'] == [41]
    # Two distinct values do not reveal the BMS step — guessing it from
    # them would make the weak cell's own deviation the threshold.
    assert s['spread_mv'] == 60.0 and s['resolution_mv'] is None


def test_the_grid_fits_the_pack():
    assert BA.grid_columns(98) == 14 and BA.grid_columns(96) == 12


# ── 6. the certificate, end to end ─────────────────────────────────────
@pytest.fixture()
def app_ctx():
    from flask import Flask
    from models.database import db
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield db


def _seed(db, circular=True, gross=67.5):
    from models.database import Vehicle, Charge, ObdReading
    v = Vehicle(name='Test Niro', brand='Kia', model='Niro EV', battery_kwh=64.0,
                battery_kwh_gross=gross, battery_chemistry='nmc622', battery_cells_series=98,
                battery_soh_baseline=100, is_archived=False, acquired_at=date(2021, 7, 2),
                first_registered_at=date(2021, 7, 2))
    db.session.add(v)
    db.session.commit()
    for c in _season_fleet():
        # The app derives the loss from the configured capacity — exactly the
        # trap: (kwh - loss) / window is 64.0 again, every time.
        loss = c.kwh_loaded - 0.6 * 64.0 if circular else 0.0
        db.session.add(Charge(vehicle_id=v.id, date=c.date, charge_type='AC', soc_from=20,
                              soc_to=80, kwh_loaded=c.kwh_loaded, loss_kwh=loss))
    cells = [4.16] * 98
    cells[40] = 4.10
    db.session.add(ObdReading(vehicle_id=v.id, timestamp=datetime(2026, 8, 8, 18, 37),
                              profile='kia_hyundai_ext', soh_pct=3481.9, cell_count=96,
                              cell_voltages_json=json.dumps([3.82] * 95 + [5.1])))
    db.session.add(ObdReading(vehicle_id=v.id, timestamp=datetime(2026, 10, 14, 8, 0),
                              profile='kia_hyundai_ext', soh_pct=94.7, soc_bms_pct=95.5,
                              soc_display_pct=100.0, cell_max_v=4.16, cell_min_v=4.10,
                              cell_count=98, cell_voltages_json=json.dumps(cells),
                              cell_temps_json=json.dumps([24, 24, 25, 24, 24])))
    db.session.commit()
    return v


def test_the_coulomb_count_that_only_returns_the_setting_is_refused(app_ctx):
    from services import battery_cert_service as bc
    v = _seed(app_ctx, circular=True)
    h = bc.compute_battery_health(v.id)
    assert h['measured'] is None and h['measured_circular'] is True
    assert h['certified_source'] == 'obd' and h['certified_soh'] == 94.7


def test_a_real_coulomb_count_is_still_used(app_ctx):
    from services import battery_cert_service as bc
    v = _seed(app_ctx, circular=False)
    h = bc.compute_battery_health(v.id)
    assert h['measured_circular'] is False and h['measured'] is not None


def test_a_broken_decode_never_becomes_a_data_point(app_ctx):
    from services import battery_cert_service as bc
    v = _seed(app_ctx)
    h = bc.compute_battery_health(v.id)
    assert len(h['obd_reads']) == 1                 # the 5.10 V / 3481.9 % read is out
    assert h['cells']['stats']['n'] == 98
    assert h['cells']['stats']['min_cells'] == [41]


def test_without_obd_the_headline_is_the_conservative_end_of_the_range(app_ctx):
    from models.database import ObdReading
    from services import battery_cert_service as bc
    v = _seed(app_ctx)
    ObdReading.query.delete()
    app_ctx.session.commit()
    h = bc.compute_battery_health(v.id)
    assert h['certified_source'] == 'window'
    assert h['certified_soh'] == h['capacity']['soh_low'] == round(64.0 / 67.5 * 100, 1)


@pytest.mark.parametrize('lang', ['de', 'en', 'es', 'fr', 'it', 'nl'])
def test_the_certificate_with_its_battery_pages_renders_in_every_language(app_ctx, lang):
    from services import battery_cert_service as bc
    from services.i18n import set_language
    if not bc._HAVE_FPDF:
        pytest.skip('fpdf not installed')
    set_language(lang)
    try:
        v = _seed(app_ctx)
        pdf = bc.generate_certificate(v.id)
    finally:
        set_language('de')
    assert pdf and len(pdf) > 50_000                # images are in there
    try:
        import pymupdf
    except ImportError:
        return
    doc = pymupdf.open(stream=bytes(pdf), filetype='pdf')
    text = '\n'.join(p.get_text() for p in doc)
    assert doc.page_count >= 4
    assert '98' in text and '#41' in text            # the weak cell is named
    # v3.0.168: no annex any more — each topic has its own numbered section,
    # headed in the reader's language.
    tr = json.load(open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8'))
    for key in ('cert.topic_cells', 'cert.sec_capmeas', 'cert.sec_checks', 'cert.not_official_title'):
        assert tr[key] in text, (lang, key)


# ── v3.0.168: the certificate as a test report ─────────────────────────
def test_every_page_opens_with_a_topic_and_the_cover_carries_the_red_box(app_ctx):
    """One topic per page (two only when both fit completely): no page may
    start with the tail of the previous topic. And the first page says in a
    red box that this is not an official certificate."""
    import re
    from services import battery_cert_service as bc
    from services.i18n import set_language
    if not bc._HAVE_FPDF:
        pytest.skip('fpdf not installed')
    pymupdf = pytest.importorskip('pymupdf')
    set_language('de')
    v = _seed(app_ctx)
    doc = pymupdf.open(stream=bytes(bc.generate_certificate(v.id)), filetype='pdf')
    pages = [[ln for ln in p.get_text().splitlines() if ln.strip()] for p in doc]
    assert 'Kein offizielles Zertifikat' in pages[0]
    assert any(ln.startswith('Seite 1 von %d' % doc.page_count) for ln in pages[0])
    for i, lines in enumerate(pages[1:], start=2):
        # running head (title, date), then the numbered section heading
        assert re.match(r'^\d+\s+·\s+\S', lines[2]), (i, lines[:4])


def test_a_snapshot_is_one_read_and_a_flat_history_draws_no_empty_map(tmp_path):
    from services import battery_cert_service as bc
    cells = [3.52] * 98
    reads = [dict(timestamp='2026-10-15T17:16:23', soc_bms_pct=94.5, cell_voltages=[4.12] * 98),
             dict(timestamp='2026-10-15T17:26:25', soc_bms_pct=94.5, cell_voltages=[4.12] * 98),
             dict(timestamp='2026-10-18T11:11:23', soc_bms_pct=25.5, cell_voltages=cells),
             dict(timestamp='2026-10-18T11:12:11', soc_bms_pct=25.5, cell_voltages=cells)]
    rows = bc._distinct_cell_reads(reads)
    assert [r['timestamp'][11:16] for r in rows] == ['17:16', '11:11']
    # every cell on the mean in every read: a grey field says nothing
    assert bc._cell_history_heatmap(rows, str(tmp_path), 20.0) is None
    if bc._mpl()[1]:
        uneven = [dict(rows[1], cell_voltages=cells[:40] + [3.48] + cells[41:]),
                  dict(rows[0], cell_voltages=[4.12] * 40 + [4.08] + [4.12] * 57)]
        assert bc._cell_history_heatmap(uneven, str(tmp_path), 20.0)


def test_the_certificate_says_how_much_went_in_by_ac_and_by_dc(app_ctx):
    from datetime import timedelta
    from models.database import db, Charge
    from services import battery_cert_service as bc
    from services.i18n import set_language
    if not bc._HAVE_FPDF:
        pytest.skip('fpdf not installed')
    pymupdf = pytest.importorskip('pymupdf')
    set_language('de')
    v = _seed(app_ctx)
    for i in range(4):
        db.session.add(Charge(vehicle_id=v.id, date=date(2025, 3, 1) + timedelta(days=9 * i),
                              charge_type='DC', soc_from=15, soc_to=80, kwh_loaded=50.0))
    db.session.commit()
    beh = bc.compute_battery_health(v.id)['behaviour']
    assert beh['dc_count'] == 4 and beh['dc_kwh'] == 200.0 and beh['ac_count'] > 0
    text = '\n'.join(p.get_text() for p in
                     pymupdf.open(stream=bytes(bc.generate_certificate(v.id)), filetype='pdf'))
    assert 'Geladen nach Art' in text
    assert 'DC – Schnelllader' in text and '200 kWh' in text
    assert 'AC – Wallbox und Ladesäule' in text and 'Gesamt' in text
