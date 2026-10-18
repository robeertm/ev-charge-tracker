# -*- coding: utf-8 -*-
"""No text of the battery certificate may run into other text (v3.0.173).

v3.0.172 printed a longer footer in three fixed thirds. With a real
report number — the odometer is part of it, EVBC-001-93000-20261018 — the
number ran into "Not an official certificate". The test data had 0 km, so
the number was short and nothing touched.

The check is about the property, not about the footer: on every page, in
every language, no two pieces of horizontal text may cover each other.
"""
import json
import os
import sys
from datetime import datetime
from itertools import combinations

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from test_obd_zaehler import FULL, LOW, _dec, app  # noqa: E402,F401

pytest.importorskip('fpdf')
pytest.importorskip('matplotlib')
pymupdf = pytest.importorskip('pymupdf')

# A long report number: six-digit odometer, as a car with 193 000 km has.
LONG_REF = 'EVBC-001-193000-20261018'


def _spans(page):
    out = []
    for b in page.get_text('dict')['blocks']:
        for line in b.get('lines', []):
            if abs(line['dir'][1]) > 0.01:      # the rotated stamp
                continue
            for s in line['spans']:
                if s['text'].strip():
                    out.append((pymupdf.Rect(s['bbox']), s['text']))
    return out


def _overlaps(page):
    found = []
    for (r1, t1), (r2, t2) in combinations(_spans(page), 2):
        x = min(r1.x1, r2.x1) - max(r1.x0, r2.x0)
        y = min(r1.y1, r2.y1) - max(r1.y0, r2.y0)
        # glyph boxes of neighbouring lines touch by a hair; real overlap
        # means a visible share of both height and width
        if x > 1.0 and y > 0.4 * min(r1.height, r2.height):
            found.append((t1, t2))
    return found


@pytest.mark.parametrize('lang', ['de', 'en', 'fr', 'es', 'it', 'nl'])
def test_no_text_runs_into_other_text(app, lang, monkeypatch):
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
    real = bc.compute_battery_health
    monkeypatch.setattr(bc, 'compute_battery_health',
                        lambda vid: dict(real(vid), cert_id=LONG_REF))
    try:
        set_language(lang); units.set_unit('km'); units.set_currency('EUR')
        doc = pymupdf.open(stream=bytes(bc.generate_certificate(v.id)), filetype='pdf')
        assert LONG_REF in doc[-1].get_text()
        from services.i18n import t
        for i, p in enumerate(doc):            # "page x of n" with the real n
            assert t('cert.page_n', p=i + 1, n=len(doc)) in p.get_text(), (lang, i)
        bad = {i + 1: _overlaps(p) for i, p in enumerate(doc)}
        assert not any(bad.values()), {k: v for k, v in bad.items() if v}
    finally:
        set_language('de')
