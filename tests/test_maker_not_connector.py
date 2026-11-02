# -*- coding: utf-8 -*-
"""The car's make is shown, never the integration that reports the car.

A car connected through Home Assistant carries ``api_brand =
'homeassistant'``, and the battery certificate printed "homeassistant · G6"
under "Make / Model". Home Assistant is home-automation software, not a car
maker. These tests pin the rule in ``services/vehicle/maker.py`` and every
place that shows the make: the certificate, the read-only API and the
vehicle pickers / fleet table.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from services.vehicle.catalog import BRANDS  # noqa: E402
from services.vehicle.maker import (  # noqa: E402
    display_maker, integration_keys, is_integration, maker_model_label,
)


def car(**kw):
    base = dict(name=None, brand=None, model=None, api_brand=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── which keys are integrations ───────────────────────────────────────

def test_home_assistant_is_the_only_registered_integration():
    assert integration_keys() == ['homeassistant']


def test_every_other_brand_key_names_a_maker():
    for b in BRANDS:
        if b.key == 'homeassistant':
            assert b.maker is None
        else:
            assert b.maker, b.key
            assert '(' not in b.maker and '#' not in b.maker, b.maker


@pytest.mark.parametrize('word', ['homeassistant', 'Homeassistant', 'Home Assistant',
                                  'home-assistant', 'Enode', 'OBD', 'Smartcar', 'Tronity'])
def test_integration_names_are_recognised(word):
    assert is_integration(word)


@pytest.mark.parametrize('word', ['Kia', 'Škoda', 'XPENG', 'Renault', 'BYD'])
def test_makers_are_not_integrations(word):
    assert not is_integration(word)


# ── the rule ──────────────────────────────────────────────────────────

def test_home_assistant_car_with_a_catalogue_model_gets_the_catalogue_maker():
    v = car(api_brand='homeassistant', brand='homeassistant', model='G6')
    assert display_maker(v) == 'XPENG'
    assert maker_model_label(v) == 'XPENG · G6'


def test_model_with_a_variant_still_finds_the_catalogue_model():
    v = car(api_brand='homeassistant', model='G6 Long Range')
    assert display_maker(v) == 'XPENG'


def test_home_assistant_car_named_after_its_maker_gets_that_maker():
    v = car(api_brand='homeassistant', brand='Homeassistant', name='XPENG G6')
    assert display_maker(v) == 'XPENG'
    assert maker_model_label(v) == 'XPENG'


def test_home_assistant_car_with_nothing_to_go_on_shows_the_model_alone():
    v = car(api_brand='homeassistant', brand='homeassistant', name='Mein Auto',
            model='Kombi 3000')
    assert display_maker(v) is None
    label = maker_model_label(v)
    assert label == 'Kombi 3000'
    assert 'homeassistant' not in label.lower()
    assert 'home assistant' not in label.lower()


def test_nothing_at_all_is_an_empty_label_not_the_integration():
    v = car(api_brand='homeassistant', brand='homeassistant')
    assert display_maker(v) is None
    assert maker_model_label(v) == ''


def test_maker_is_not_repeated_when_the_model_starts_with_it():
    assert maker_model_label(car(api_brand='xpeng', model='XPENG G6')) == 'XPENG G6'
    assert maker_model_label(car(brand='Kia', model='Kia Niro EV')) == 'Kia Niro EV'


@pytest.mark.parametrize('kw, maker, label', [
    (dict(brand='Kia', model='Niro EV', api_brand='kia'), 'Kia', 'Kia · Niro EV'),
    (dict(brand='Renault', model='Zoe', api_brand='renault'), 'Renault', 'Renault · Zoe'),
    (dict(brand='Škoda', model='Enyaq', api_brand='skoda_api'), 'Škoda', 'Škoda · Enyaq'),
    (dict(brand='Hyundai', model='Kona'), 'Hyundai', 'Hyundai · Kona'),
    (dict(brand='Volkswagen', model='ID.3', api_brand='vw'), 'Volkswagen', 'Volkswagen · ID.3'),
    (dict(brand='BYD', model='Dolphin'), 'BYD', 'BYD · Dolphin'),
])
def test_correct_makers_stay_exactly_as_they_were(kw, maker, label):
    v = car(**kw)
    assert display_maker(v) == maker
    assert maker_model_label(v) == label


@pytest.mark.parametrize('kw, maker', [
    # brand keys copied into the free-text field by the legacy seed
    (dict(brand='Kia_pleos', model='EV6'), 'Kia'),
    (dict(brand='Hyundai_pleos'), 'Hyundai'),
    (dict(brand='Skoda_api', model='Enyaq'), 'Škoda'),
    (dict(brand='Xpeng', model='G6'), 'XPENG'),
    # no brand typed: the connector key decides when it stands for one make
    (dict(api_brand='kia_pleos', model='EV6'), 'Kia'),
    (dict(api_brand='genesis_pleos'), 'Genesis'),
    (dict(api_brand='skoda_api'), 'Škoda'),
    (dict(api_brand='xpeng'), 'XPENG'),
    (dict(api_brand='smart'), 'Smart'),
    (dict(api_brand='renault'), 'Renault'),
])
def test_brand_keys_are_shown_as_the_maker(kw, maker):
    assert display_maker(car(**kw)) == maker


def test_works_on_a_plain_mapping_too():
    assert display_maker({'api_brand': 'homeassistant', 'model': 'G6'}) == 'XPENG'
    assert maker_model_label({'brand': 'homeassistant', 'model': 'G6'}) == 'XPENG · G6'


# ── the certificate ───────────────────────────────────────────────────

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


def _pdf_to_text(pdf: bytes) -> str:
    try:
        import pymupdf
        return '\n'.join(p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf'))
    except ImportError:
        pass
    exe = shutil.which('pdftotext')
    if not exe:
        pytest.skip('neither pymupdf nor pdftotext available')
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / 'c.pdf'
        p.write_bytes(pdf)
        out = subprocess.run([exe, '-enc', 'UTF-8', str(p), '-'], capture_output=True,
                             timeout=60)
        return out.stdout.decode('utf-8', 'replace')


def _seed_car(db, **kw):
    """A car with two real OBD reads, enough for a certificate."""
    from models.database import Vehicle, ObdReading
    from test_obd_zaehler import FULL, LOW, _dec
    fields = dict(name='Daily', battery_kwh=64.0, battery_kwh_gross=67.5,
                  battery_chemistry='nmc622', battery_cells_series=98,
                  is_archived=False, auto_sync=False)
    fields.update(kw)
    v = Vehicle(**fields)
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


def _cert_text(vid):
    pytest.importorskip('fpdf')
    import services.battery_cert_service as bc
    from services.i18n import set_language
    from services import units
    try:
        set_language('de'); units.set_unit('km'); units.set_currency('EUR')
        pdf = bc.generate_certificate(vid)
    finally:
        set_language('de')
    assert pdf, 'certificate did not render'
    return _pdf_to_text(bytes(pdf)).replace('\xa0', ' ')


def test_certificate_prints_the_maker_not_home_assistant(db_):
    v = _seed_car(db_, api_brand='homeassistant', brand='homeassistant', model='G6')
    import services.battery_cert_service as bc
    data = bc.compute_battery_health(v.id)
    assert data['vehicle']['maker'] == 'XPENG'
    text = _cert_text(v.id)
    assert 'homeassistant' not in text.lower()
    assert 'XPENG · G6' in text


def test_certificate_derives_the_maker_from_the_name(db_):
    v = _seed_car(db_, name='XPENG G6', api_brand='homeassistant')
    text = _cert_text(v.id)
    assert 'homeassistant' not in text.lower()
    assert 'XPENG' in text


def test_certificate_keeps_a_kia_a_kia(db_):
    v = _seed_car(db_, api_brand='kia', brand='Kia', model='Niro EV')
    text = _cert_text(v.id)
    assert 'Kia · Niro EV' in text


# ── the read-only API ────────────────────────────────────────────────

def test_readonly_api_keeps_brand_and_adds_the_maker(db_):
    from models.database import Vehicle
    from services import readonly_api as ro
    db_.session.add(Vehicle(name='G6', brand='homeassistant', model='G6',
                            api_brand='homeassistant', is_archived=False))
    db_.session.add(Vehicle(name='Daily', brand='Kia', model='Niro EV',
                            api_brand='kia', is_archived=False))
    db_.session.add(Vehicle(name='Mystery', api_brand='homeassistant', is_archived=False))
    db_.session.commit()
    cars = ro.build_payload('test')['vehicles']
    g6, kia, mystery = cars
    # ``brand`` is the stored field, unchanged for existing consumers
    assert g6['brand'] == 'homeassistant' and kia['brand'] == 'Kia'
    assert g6['maker'] == 'XPENG'
    assert kia['maker'] == 'Kia'
    assert mystery['maker'] is None


# ── templates ────────────────────────────────────────────────────────

def test_jinja_globals_show_the_maker():
    from flask import Flask, render_template_string
    from services import i18n
    app = Flask(__name__)
    i18n.init_app(app)
    v = car(api_brand='homeassistant', brand='homeassistant', model='G6', name='Daily')
    with app.test_request_context('/'):
        assert render_template_string('{{ display_maker(v) }}', v=v) == 'XPENG'
        assert render_template_string('{{ maker_model_label(v) }}', v=v) == 'XPENG · G6'
        assert render_template_string("{{ maker_model_label(v, ' ') }}", v=v) == 'XPENG G6'
        nothing = car(api_brand='homeassistant', brand='homeassistant')
        assert render_template_string('{{ display_maker(v) }}', v=nothing) == ''


@pytest.mark.parametrize('tpl', ['input.html', 'obd.html', 'settings.html'])
def test_no_template_prints_the_raw_brand_field_as_the_maker(tpl):
    src = (pathlib.Path(ROOT) / 'templates' / tpl).read_text(encoding='utf-8')
    assert '{{ v.brand' not in src
    assert 'v.brand or' not in src
