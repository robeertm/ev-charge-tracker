# -*- coding: utf-8 -*-
"""Battery certificate: language choice, the Ø/°C glyphs, the SoH caveat and
the renamed window chart (v3.0.160).

Three things the owner asked for after reading their own certificate:
  * get it in English (a foreign buyer),
  * fix the layout (an orphan word on an empty page; "O"/"C" instead of
    "Ø"/"°C"; a long label running under its value),
  * keep the vehicle's BMS SoH but say plainly that it is a relative raw
    value that can exceed 100 %, with the reliable OBD value beside it.

And rename the window chart title "nobody understands".
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import db, Vehicle, Charge, VehicleSync, ObdReading  # noqa: E402
import services.battery_cert_service as bc  # noqa: E402
from services.battery_cert_service import latin1_safe  # noqa: E402

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')


# ── unit: the glyph map and key coverage (no PDF needed) ─────────────

def test_the_average_and_degree_glyphs_survive_latin1():
    """Ø and ° are IN latin-1 — they must pass through, not become O / (nothing)."""
    assert latin1_safe('Ø 15 °C') == 'Ø 15 °C'
    # a truly non-latin-1 char is still handled (Š -> S), so nothing crashes
    assert latin1_safe('Škoda') == 'Skoda'


def test_every_cert_string_the_new_code_uses_exists_in_all_six_languages():
    src = open(os.path.join(ROOT, 'services', 'battery_cert_service.py'), encoding='utf-8').read()
    used = set(re.findall(r"""t\(\s*['"]((?:cert)\.[a-z0-9_]+)['"]""", src))
    assert {'cert.bms_relative_note', 'cert.soh_obd_here',
            'cert.chart_soh_note', 'cert.window_chart_title'} <= used
    for lang in LANGS:
        d = json.loads(open(os.path.join(ROOT, 'translations', f'{lang}.json'),
                            encoding='utf-8').read())
        missing = sorted(k for k in used if k not in d)
        assert not missing, (lang, missing)


def test_the_cryptic_window_title_is_gone_in_every_language():
    for lang in LANGS:
        d = json.loads(open(os.path.join(ROOT, 'translations', f'{lang}.json'),
                            encoding='utf-8').read())
        title = d['cert.window_chart_title']
        assert 'Vollladen' not in title and 'displayed full charge' not in title
        # it now speaks of a full charge in plain words
        assert any(w in title.lower() for w in
                   ('volle ladung', 'full charge', 'charge complète', 'carga completa',
                    'ricarica completa', 'volle lading'))


# ── functional: render the certificate (needs fpdf + matplotlib + pymupdf) ──

@pytest.fixture(scope='module')
def rendered():
    pytest.importorskip('fpdf')
    pytest.importorskip('matplotlib')
    pymupdf = pytest.importorskip('pymupdf')
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    from services.i18n import set_language
    from services import units
    out = {}
    with app.app_context():
        db.create_all()
        v = Vehicle(name='Niro', battery_kwh=64.8, battery_kwh_gross=67.3,
                    battery_chemistry='nmc', is_archived=False, auto_sync=False,
                    first_registered_at=datetime(2021, 7, 2))
        db.session.add(v); db.session.commit()
        odo = 60000
        for i in range(60):
            d = date(2024, 1, 1) + timedelta(days=i * 14)
            if d > date(2026, 10, 15):
                break
            odo += 300
            db.session.add(Charge(vehicle_id=v.id, date=d, odometer=odo,
                charge_type='DC' if i % 4 == 0 else 'AC', soc_from=20, soc_to=80,
                kwh_loaded=36.0, loss_kwh=1.2, eur_per_kwh=0.3, total_cost=10.8))
        # BMS SoH that drifts above 100 % — the whole point of the caveat
        for i in range(120):
            ts = datetime(2026, 4, 1) + timedelta(hours=i * 20)
            if ts > datetime(2026, 10, 15, 16):
                break
            db.session.add(VehicleSync(vehicle_id=v.id, timestamp=ts,
                soc_percent=60, odometer_km=odo,
                battery_soh_percent=round(95 + i * 0.25, 1), is_charging=False))
        db.session.add(ObdReading(vehicle_id=v.id, timestamp=datetime(2026, 10, 15, 17, 16),
            source='elm327', profile='kia_hyundai_ext', soh_pct=93.0,
            pack_voltage_v=404.7, pack_current_a=0.5, aux_battery_v=14.8,
            cell_count=98, cell_min_v=4.12, cell_max_v=4.12, cell_avg_v=4.12,
            cell_delta_mv=0, temp_min_c=15, temp_max_c=15, temp_avg_c=15,
            cell_voltages_json=json.dumps([4.12] * 98),
            cell_temps_json=json.dumps([15, 15, 15])))
        db.session.commit()
        for lang in ('de', 'en'):
            set_language(lang); units.set_unit('km'); units.set_currency('EUR')
            pdf = bytes(bc.generate_certificate(v.id))
            doc = pymupdf.open(stream=pdf, filetype='pdf')
            out[lang] = {'text': '\n'.join(p.get_text() for p in doc),
                         'page_texts': [p.get_text().strip() for p in doc],
                         'pages': doc.page_count}
    set_language('de')
    return out


def test_the_certificate_renders_in_english_on_request(rendered):
    en = rendered['en']['text']
    assert 'State of Health' in en
    # a few German section headers must NOT be in the English PDF
    assert 'SoH-Bestimmung' not in en and 'Datengrundlage' not in en
    assert 'Battery Health Certificate' in en or 'State of Health' in en


def test_german_stays_german(rendered):
    de = rendered['de']['text']
    assert 'SoH-Bestimmung' in de and 'State of Health' in de  # headline label is mixed


@pytest.mark.parametrize('lang', ('de', 'en'))
def test_the_degree_and_average_signs_render(rendered, lang):
    txt = rendered[lang]['text']
    assert '°C' in txt and 'Ø' in txt
    assert '�' not in txt and 'Ã' not in txt       # no mojibake


@pytest.mark.parametrize('lang', ('de', 'en'))
def test_the_bms_soh_is_kept_but_flagged_and_the_obd_value_is_shown(rendered, lang):
    txt = rendered[lang]['text'].upper()
    assert 'RELATIV' in txt                 # the caveat word (de: RELATIVER, en: RELATIVE)
    assert 'OBD' in txt and '93.0' in rendered[lang]['text']  # reliable value present


@pytest.mark.parametrize('lang', ('de', 'en'))
def test_no_page_is_just_one_orphan_word(rendered, lang):
    for pt in rendered[lang]['page_texts']:
        words = pt.split()
        assert not (0 < len(words) <= 2 and len(pt) < 30), f'orphan page: {pt!r}'
