# -*- coding: utf-8 -*-
"""Thrown away stays thrown away — and a charge offered twice stays one charge.

Discarding a reading deleted the row and nothing else. ``store_charges`` finds a
reading by ``(device_key, source_id)``, so with the row gone the next fetch that
covers the window files it again as new. A routine sync only asks for what is
new and never noticed — but a *full* one (the backfill window, or the first sync
after a long outage) walks months, and every reading anybody ever cleaned up
comes back at once.

Ten readings cleaned out by hand on 2026-10-01 would have reappeared the first
time somebody pressed the big sync button, and nothing would have said why.

The same fetch also used to re-file a charge whose entry is already **matched**:
the twin rule skipped matched rows on purpose, so an analyzer still settling its
samples could put a ghost next to a finished entry. It is only a ghost while it
brings no more energy, though — an answer with MORE energy in it is how an
incomplete reading gets corrected, and that path has to stay open.

All figures here are invented.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evdisc-'))

from flask import Flask                                                 # noqa: E402
from models.database import (db, AppConfig, Charge, DiscardedReading,    # noqa: E402
                             Vehicle, WallboxCharge)
from services import shelly_link as L                                   # noqa: E402


def pruefe(name, ist, soll):
    print(("  OK   " if ist == soll else "  FEHL ") + name + "   ist=%r soll=%r" % (ist, soll))
    assert ist == soll, "%s: ist=%r soll=%r" % (name, ist, soll)


def _app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    return app, ctx


TAG = date.today() - timedelta(days=40)
START = datetime(TAG.year, TAG.month, TAG.day, 13, 34, 0)


def _angebot(sid, start, dauer_h=2.0, kwh=31.921, device='wallbox'):
    """What the analyzer sends for one charge."""
    return {'wallbox': {'device_key': device, 'name': 'Wallbox'},
            'charges': [{'id': sid, 'device_key': device,
                         'start_ts': int(start.timestamp()),
                         'end_ts': int((start + timedelta(hours=dauer_h)).timestamp()),
                         'energy_kwh': kwh, 'solar_kwh': None, 'battery_kwh': None,
                         'grid_kwh': None, 'cost_eur': 1.24, 'cost_model': 'source',
                         'coverage': 1.0, 'avg_power_w': 6280.0,
                         'peak_power_w': 6549.0, 'session_count': 1}]}


# ── Verworfen bleibt verworfen ─────────────────────────────────────────────

def test_01_ein_verworfenes_kommt_beim_vollabgleich_nicht_zurueck():
    print("== Der Fall: aufgeraeumt, dann Vollabgleich ==")
    app, ctx = _app()
    neu, upd = L.store_charges(_angebot('abc123', START))
    pruefe("erst einmal da", (neu, WallboxCharge.query.count()), (1, 1))

    wc = WallboxCharge.query.one()
    L.merke_verworfen(wc)
    db.session.delete(wc)
    db.session.commit()
    pruefe("weggeraeumt", WallboxCharge.query.count(), 0)
    pruefe("aber gemerkt", DiscardedReading.query.count(), 1)

    # Derselbe Vollabgleich noch einmal — genau das tut die 120-Tage-Nachlese.
    neu2, upd2 = L.store_charges(_angebot('abc123', START))
    pruefe("kommt NICHT zurueck", WallboxCharge.query.count(), 0)
    pruefe("und wird nicht als neu gezaehlt", neu2, 0)
    ctx.pop()


def test_02_auch_ein_geist_mit_anderer_kennung_bleibt_draussen():
    """The analyzer offers one charge again with a later start while its
    samples settle — different id, same end. A meter cannot end two sessions
    on one device in the same second, so that is the same charge."""
    print("== Derselbe Schluss, andere Kennung ==")
    app, ctx = _app()
    L.store_charges(_angebot('abc123', START))
    wc = WallboxCharge.query.one()
    L.merke_verworfen(wc)
    db.session.delete(wc)
    db.session.commit()

    # 30 Minuten spaeterer Start, gleiches Ende -> andere md5, selbe Ladung.
    neu, _ = L.store_charges(_angebot('zzz999', START + timedelta(minutes=30),
                                      dauer_h=1.5))
    pruefe("bleibt drausssen", WallboxCharge.query.count(), 0)
    pruefe("nichts angelegt", neu, 0)
    ctx.pop()


def test_03_eine_andere_box_ist_nicht_gemeint():
    print("== Gegenprobe: dieselbe Kennung an einer ANDEREN Box ==")
    app, ctx = _app()
    L.store_charges(_angebot('abc123', START))
    wc = WallboxCharge.query.one()
    L.merke_verworfen(wc)
    db.session.delete(wc)
    db.session.commit()
    neu, _ = L.store_charges(_angebot('abc123', START, device='garage2'))
    pruefe("kommt an, weil andere Box", WallboxCharge.query.count(), 1)
    pruefe("als neu gezaehlt", neu, 1)
    ctx.pop()


def test_04_ein_nicht_verworfenes_kommt_ganz_normal_an():
    """🔴 The counter-test that keeps the whole thing honest: without this,
    a filter that simply swallows everything would look just as green."""
    print("== Gegenprobe: nichts verworfen, alles kommt an ==")
    app, ctx = _app()
    neu, _ = L.store_charges(_angebot('abc123', START))
    pruefe("da", WallboxCharge.query.count(), 1)
    neu2, upd2 = L.store_charges(_angebot('def456', START + timedelta(days=1)))
    pruefe("zweite auch", WallboxCharge.query.count(), 2)
    pruefe("als neu gezaehlt", neu2, 1)
    ctx.pop()


def test_05_zweimal_merken_legt_nicht_zweimal_an():
    print("== Das Merken ist wiederholbar ==")
    app, ctx = _app()
    L.store_charges(_angebot('abc123', START))
    wc = WallboxCharge.query.one()
    L.merke_verworfen(wc)
    db.session.commit()
    L.merke_verworfen(wc)
    db.session.commit()
    pruefe("ein Eintrag", DiscardedReading.query.count(), 1)
    ctx.pop()


# ── Ein Geist neben einer fertigen Ladung ──────────────────────────────────

def _mit_ladung(kwh=31.921):
    app, ctx = _app()
    v = Vehicle(name='Testwagen', battery_kwh=58.0,
                wallbox_link_enabled=True, wallbox_device_key='wallbox')
    db.session.add(v)
    db.session.commit()
    L.store_charges(_angebot('abc123', START, kwh=kwh))
    wc = WallboxCharge.query.one()
    c = Charge(vehicle_id=v.id, date=TAG, charge_hour=13, charge_end_hour=15,
               kwh_loaded=kwh, charge_type='AC', soc_from=37, soc_to=90)
    c.calculate_fields(58.0, 0.92)
    db.session.add(c)
    db.session.flush()
    wc.match_state = 'matched'
    wc.charge_id = c.id
    c.wallbox_charge_id = wc.id
    db.session.commit()
    return app, ctx, wc, c


def test_06_ein_geist_neben_einer_fertigen_ladung_kommt_nicht_dazu():
    print("== Dieselbe Ladung noch einmal, Eintrag steht schon ==")
    app, ctx, wc, c = _mit_ladung()
    neu, upd = L.store_charges(_angebot('spaeter1', START + timedelta(minutes=30),
                                        dauer_h=1.5, kwh=28.0))
    pruefe("keine zweite Zeile", WallboxCharge.query.count(), 1)
    pruefe("nicht als neu gezaehlt", neu, 0)
    pruefe("die zugeordnete bleibt unberuehrt", wc.energy_kwh, 31.921)
    ctx.pop()


def test_07_aber_eine_VOLLSTAENDIGERE_antwort_darf_herein():
    """🔴 Exactly the repair path from v3.0.135: a matched reading held half
    the charge, the complete answer arrived later as a new reading. Swallowing
    that would make the half-charge permanent."""
    print("== Eine Antwort mit MEHR Energie darf herein ==")
    app, ctx, wc, c = _mit_ladung(kwh=16.337)
    neu, upd = L.store_charges(_angebot('spaeter1', START, kwh=34.763))
    pruefe("kommt als eigene Zeile an", WallboxCharge.query.count(), 2)
    pruefe("als neu gezaehlt", neu, 1)
    pruefe("die alte bleibt, wie sie war", wc.energy_kwh, 16.337)
    ctx.pop()
