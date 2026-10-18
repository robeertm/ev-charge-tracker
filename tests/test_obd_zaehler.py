# -*- coding: utf-8 -*-
"""The BMS counters of the Kia/Hyundai profile (v3.0.167).

220101 carries four 4-byte counters in a row: Ah charged, Ah discharged,
kWh charged, kWh discharged. Until 3.0.166 the decoder read the two ENERGY
counters into the Ah fields. Found on a real drive: a Kia Niro EV read at
BMS 94.5 % (2026-10-15) and again at 25.5 % after 216 km without a charge
(2026-10-18). The old fields moved 49.1 "Ah" for 69 SoC points — a 69 Ah,
26 kWh pack, on a car with a 180 Ah, 64 kWh battery. The certificate
printed exactly that.

The frames below are the two real reads, unchanged.
"""
import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.vehicle import obd_decode as od  # noqa: E402
from services import battery_analysis as BA  # noqa: E402

FULL = (  # 2026-10-15 17:16, displayed 97.5 %, BMS 94.5 %
    '7EC 10 3E 62 01 01 FF F7 E7 \r7EC 21 FF BD 00 00 00 00 83 \r'
    '7EC 22 00 05 0F CF 0F 0F 0F \r7EC 23 0F 0F 0F 00 00 10 CE \r'
    '7EC 24 1F CE 07 00 00 94 00 \r7EC 25 08 EB 20 00 08 CD FD \r'
    '7EC 26 00 03 52 D1 00 03 2B \r7EC 27 12 00 D0 56 26 0D 01 \r'
    '7EC 28 94 00 00 00 00 03 E8 \r\r')
LOW = (  # 2026-10-18 11:11, displayed 26 %, BMS 25.5 %, 216 km later
    '7EC 03 59 02 FF AA AA AA AA \r7EC 10 3E 62 01 01 FF F7 E7 \r'
    '7EC 21 FF 33 00 00 00 00 83 \r7EC 22 00 17 0D 7E 15 14 15 \r'
    '7EC 23 14 15 15 00 00 0F B0 \r7EC 24 2A B0 01 00 00 92 00 \r'
    '7EC 25 08 EB 46 00 08 D3 46 \r7EC 26 00 03 52 DE 00 03 2C \r'
    '7EC 27 FD 00 D0 84 26 0D 01 \r7EC 28 59 00 00 00 00 03 E8 \r\r')


def _dec(raw):
    return od.decode('kia_hyundai_ext', {'220101': raw})


def test_the_four_counters_land_in_the_right_fields():
    d = _dec(FULL)
    assert d['soc_bms_pct'] == 94.5 and d['pack_voltage_v'] == 404.7
    assert d['cumulative_charge_ah'] == pytest.approx(58448.0)
    assert d['cumulative_discharge_ah'] == pytest.approx(57702.1)
    assert d['cumulative_charge_kwh'] == pytest.approx(21780.9)
    assert d['cumulative_discharge_kwh'] == pytest.approx(20763.4)


def test_energy_over_charge_is_a_pack_voltage():
    """kWh / Ah over the car's whole life must come out as a pack voltage —
    the property that told the two pairs apart."""
    for raw in (FULL, LOW):
        d = _dec(raw)
        for k in ('charge', 'discharge'):
            volt = d[f'cumulative_{k}_kwh'] * 1000 / d[f'cumulative_{k}_ah']
            assert 320 < volt < 410, (k, volt)


def _as_read(raw, ts, display):
    d = _dec(raw)
    return dict(timestamp=ts, soc_display_pct=display, **{
        k: d[k] for k in ('soc_bms_pct', 'pack_voltage_v', 'cumulative_charge_ah',
                          'cumulative_discharge_ah', 'cumulative_charge_kwh',
                          'cumulative_discharge_kwh')})


def test_the_real_drive_gives_a_plausible_capacity():
    a = _as_read(FULL, '2026-10-15T17:16:23', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:23', 26.0)
    m = BA.obd_capacity([a, b])
    assert len(m) == 1
    m = m[0]
    # 135.3 Ah out, 3.8 Ah regen back in → 131.5 Ah for 69 points
    assert m['ah'] == 131.5 and m['capacity_ah'] == 190.6
    # the BMS's own energy count: 49.1 kWh out, 1.3 kWh back → 47.8 kWh
    assert m['kwh'] == 47.8 and m['capacity_kwh'] == 69.3 and m['kwh_counted']
    # a pack of this size, not the 69 Ah / 26 kWh the old fields made of it
    assert 150 < m['capacity_ah'] < 200 and 55 < m['capacity_kwh'] < 75


def test_a_snapshot_right_after_a_read_counts_once():
    """'Save snapshot' in the live view stores the same moment again —
    one drive must stay one measurement, not four."""
    a = _as_read(FULL, '2026-10-15T17:16:23', 97.5)
    a2 = dict(a, timestamp='2026-10-15T17:26:25')
    b = _as_read(LOW, '2026-10-18T11:11:23', 26.0)
    b2 = dict(b, timestamp='2026-10-18T11:12:11')
    assert len(BA.obd_capacity([a, a2, b, b2])) == 1


def test_without_energy_counters_kwh_still_comes_from_the_voltage():
    a = _as_read(FULL, '2026-10-15T17:16:23', 97.5)
    b = _as_read(LOW, '2026-10-18T11:11:23', 26.0)
    for r in (a, b):
        r.pop('cumulative_charge_kwh'); r.pop('cumulative_discharge_kwh')
    m = BA.obd_capacity([a, b])[0]
    assert not m['kwh_counted'] and m['kwh'] is None
    assert m['capacity_kwh'] == round(190.6 * (404.7 + 345.4) / 2 / 1000, 1)


# ── the stored reads are decoded again from their raw frames ──────────

@pytest.fixture()
def app():
    from flask import Flask
    from models.database import db
    a = Flask(__name__)
    a.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    a.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(a)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()


def test_stored_reads_are_repaired_from_their_raw_frames(app):
    from models.database import db, Vehicle, ObdReading
    from services.battery_cert_service import obd_zaehler_neu_lesen
    v = Vehicle(name='Niro', battery_kwh=64.8)
    db.session.add(v); db.session.commit()
    for ts, raw, wrong in ((datetime(2026, 10, 15, 17, 16), FULL, (21780.9, 20763.4)),
                           (datetime(2026, 10, 18, 11, 11), LOW, (21782.2, 20812.5))):
        db.session.add(ObdReading(vehicle_id=v.id, timestamp=ts, profile='kia_hyundai_ext',
                                  soc_bms_pct=_dec(raw)['soc_bms_pct'],
                                  cumulative_charge_ah=wrong[0], cumulative_discharge_ah=wrong[1],
                                  raw_json=json.dumps({'220101': raw})))
    # a read of another profile and one without frames stay as they are
    db.session.add(ObdReading(vehicle_id=v.id, profile='xpeng', cumulative_charge_ah=5.0,
                              raw_json=json.dumps({'220101': FULL})))
    db.session.add(ObdReading(vehicle_id=v.id, profile='kia_hyundai_ext',
                              cumulative_charge_ah=7.0, raw_json=None))
    db.session.commit()

    assert obd_zaehler_neu_lesen() == 2
    rows = ObdReading.query.order_by(ObdReading.id).all()
    assert rows[0].cumulative_charge_ah == pytest.approx(58448.0)
    assert rows[0].cumulative_discharge_kwh == pytest.approx(20763.4)
    assert rows[1].cumulative_discharge_ah == pytest.approx(57837.4)
    assert rows[1].cumulative_charge_kwh == pytest.approx(21782.2)
    assert rows[2].cumulative_charge_ah == 5.0 and rows[2].cumulative_charge_kwh is None
    assert rows[3].cumulative_charge_ah == 7.0
    assert obd_zaehler_neu_lesen() == 0          # a second run changes nothing


def test_the_certificate_prints_the_corrected_capacity(app):
    pytest.importorskip('fpdf')
    pytest.importorskip('matplotlib')
    pymupdf = pytest.importorskip('pymupdf')
    from models.database import db, Vehicle, ObdReading
    import services.battery_cert_service as bc
    from services.i18n import set_language
    from services import units
    v = Vehicle(name='Niro', battery_kwh=64.8, battery_kwh_gross=67.3,
                battery_chemistry='nmc622', is_archived=False, auto_sync=False)
    db.session.add(v); db.session.commit()
    for ts, raw, disp in ((datetime(2026, 10, 15, 17, 16), FULL, 97.5),
                          (datetime(2026, 10, 18, 11, 11), LOW, 26.0)):
        d = _dec(raw)
        db.session.add(ObdReading(
            vehicle_id=v.id, timestamp=ts, source='elm327-ble', profile='kia_hyundai_ext',
            soh_pct=93.0, soc_display_pct=disp, raw_json=json.dumps({'220101': raw}),
            **{k: d[k] for k in ('soc_bms_pct', 'pack_voltage_v', 'cumulative_charge_ah',
                                 'cumulative_discharge_ah', 'cumulative_charge_kwh',
                                 'cumulative_discharge_kwh')}))
    db.session.commit()
    try:
        for lang, note in (('de', 'schätzt es'), ('en', 'is an estimate')):
            set_language(lang); units.set_unit('km'); units.set_currency('EUR')
            pdf = bytes(bc.generate_certificate(v.id))
            # numbers and units are glued with a no-break space (v3.0.174)
            text = '\n'.join(p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf')).replace('\xa0', ' ')
            # v3.0.168: a key-figure card on the first page and its own section
            assert '69.3 kWh' in text and '190.6 Ah' in text, lang
            assert 'BMS 94.5 → 25.5 %' in text, lang       # no rounding to 94 -> 26
            # the measurement table names both ends, day and time
            assert '15.10.26 17:16' in text and '18.10.26 11:11' in text, lang
            assert '131.5 Ah' in text, lang
            assert '26.0 kWh' not in text
            assert note in text
            # v3.0.172: the printout names the app and spells out where to find it —
            # on the first page in full, and in every page's footer
            pages = [p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf')]
            assert 'https://github.com/robeertm/ev-charge-tracker' in pages[0], lang
            assert all('github.com/robeertm/ev-charge-tracker' in p for p in pages), lang
            assert 'this app' not in text and 'dieser App' not in text, lang
    finally:
        set_language('de')
