# -*- coding: utf-8 -*-
"""A meter reading nobody claims files the charge itself — or stays silent.

Until now the wallbox link could only ever DECORATE an entry the car-side
detector had already found. So every weakness of that detector cost a whole
charge, while the measurement sat beside it holding exactly what was missing.
Three times in four days, each time a different weakness; the third one is the
fixture below:

    07:02   SoC 88 %, odometer 34286, at work
            (nothing at all for the next nine and a half hours)
    15:40   the wallbox starts, 4.956 kWh over 93 minutes
    16:40   SoC 85 %, odometer 34309, at home, is_charging FALSE
    16:55   SoC 85 %, at home, is_charging TRUE
    17:13   the wallbox stops
    20:40   SoC 87 %, at home, is_charging FALSE

The only sync before the charging flag was already an HOUR INSIDE the charge,
so the "SoC before the charge" was really a SoC during it: 85 -> 87, a 2 % gain,
under the 3 % threshold, whole charge dropped.

Both halves are checked here, and the silent half is the important one: two of
that wallbox's readings belong to a visitor's car and look exactly like a big
home charge. Filing those would move a stranger's kilowatt-hours into somebody's
running costs, and nothing afterwards would look wrong.

Every figure in this file is invented; the coordinates are nowhere.

🔴 The checks that say a reading must NOT become a charge read the counter as
``get('created', 0) or 0`` on purpose: against the state before this rule
existed they have to be GREEN. A counter-test that goes red because a tally key
is missing proves nothing about the guard it claims to check.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evfile-'))

from flask import Flask                                                # noqa: E402
from models.database import (db, AppConfig, Charge, Vehicle,           # noqa: E402
                             VehicleSync, WallboxCharge)
from services import shelly_link as L                                  # noqa: E402

HEIM = (50.500000, 10.500000)        # erfunden
FERN = (50.600000, 10.700000)        # ~17 km entfernt, also eindeutig nicht daheim


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
    AppConfig.set('home_lat', str(HEIM[0]))
    AppConfig.set('home_lon', str(HEIM[1]))
    AppConfig.set('home_label', 'Zuhause')
    AppConfig.set('battery_kwh', '58.0')
    return app, ctx


def _config(**kw):
    AppConfig.set(L.K_ENABLED, kw.get('enabled', '1'))
    AppConfig.set(L.K_URL, kw.get('url', 'https://box.invalid:8765'))
    AppConfig.set(L.K_TOKEN, kw.get('token', 'k'))
    AppConfig.set(L.K_APPLY, kw.get('apply_mode', 'auto'))
    AppConfig.set(L.K_TOL, kw.get('tol', L.DEFAULT_TOLERANCE_MIN))


def _car(name='Testwagen', key='wallbox', battery=58.0, enabled=True):
    v = Vehicle(name=name, battery_kwh=battery,
                wallbox_link_enabled=enabled, wallbox_device_key=key)
    db.session.add(v)
    db.session.commit()
    return v


def _sync(v, when, soc, km, laedt=False, ort=HEIM, kw=None):
    s = VehicleSync(vehicle_id=v.id, timestamp=when, soc_percent=soc,
                    odometer_km=km, is_charging=laedt, charge_power_kw=kw,
                    location_lat=ort[0] if ort else None,
                    location_lon=ort[1] if ort else None)
    db.session.add(s)
    db.session.commit()
    return s


def _messung(v_start, dauer_min, kwh, solar=None, sid='m1'):
    wc = WallboxCharge(
        source_id=sid, device_key='wallbox',
        start_ts=int(v_start.timestamp()),
        end_ts=int((v_start + timedelta(minutes=dauer_min)).timestamp()),
        energy_kwh=kwh, solar_kwh=solar, battery_kwh=0.0,
        grid_kwh=(None if solar is None else max(0.0, kwh - solar)),
        cost_eur=0.18, cost_model='source', coverage=1.0,
        avg_power_w=kwh / (dauer_min / 60.0) * 1000.0, peak_power_w=4725.0,
        session_count=1, match_state='unmatched',
        match_note='no charge of a linked car falls in this window')
    db.session.add(wc)
    db.session.commit()
    return wc


TAG = date.today() - timedelta(days=2)


def _t(h, m=0):
    return datetime(TAG.year, TAG.month, TAG.day, h, m, 0)


def _der_echte_fall(**kw):
    """Exactly the sequence from the top of this file."""
    app, ctx = _app()
    _config(**kw)
    v = _car()
    _sync(v, _t(7, 2), 88, 34286, ort=FERN)
    _sync(v, _t(16, 40), 85, 34309)
    _sync(v, _t(16, 55), 85, 34309, laedt=True, kw=2.0)
    _sync(v, _t(20, 40), 87, 34309)
    wc = _messung(_t(15, 40), 93, 4.956, solar=3.964)
    return app, ctx, v, wc


# ── Die Hauptsache ─────────────────────────────────────────────────────────

def test_01_die_messung_legt_die_ladung_an():
    print("== Der echte Fall: niemand holt die Messung ab, sie legt selbst an ==")
    app, ctx, v, wc = _der_echte_fall()
    pruefe("vorher keine Ladung", Charge.query.count(), 0)
    tally = L.match_all(device_key='wallbox')
    pruefe("eine angelegt", tally.get('created'), 1)
    pruefe("Messung ist jetzt zugeordnet", wc.match_state, 'matched')
    c = Charge.query.one()
    pruefe("die gemessenen kWh stehen drin", round(c.kwh_loaded, 3), 4.956)
    pruefe("Ladestand VORHER bleibt leer", c.soc_from, None)
    pruefe("Ladestand NACHHER ist gemessen", c.soc_to, 87)
    pruefe("Ort ist zuhause", c.location_name, 'Zuhause')
    pruefe("Tachostand aus dem Sync", c.odometer, 34309)
    pruefe("Messung zeigt auf die Ladung", wc.charge_id, c.id)
    pruefe("Ladung zeigt zurueck", c.wallbox_charge_id, wc.id)
    ctx.pop()


def test_02_zweiter_lauf_legt_nicht_noch_einmal_an():
    print("== Ein zweiter Durchgang darf den Tag nicht verdoppeln ==")
    app, ctx, v, wc = _der_echte_fall()
    L.match_all(device_key='wallbox')
    tally = L.match_all(device_key='wallbox')
    pruefe("beim zweiten Mal nichts Neues", tally.get('created', 0) or 0, 0)
    pruefe("es bleibt bei einer Ladung", Charge.query.count(), 1)
    ctx.pop()


def test_03_die_angelegte_ladung_ist_undurchsichtig():
    """🔑 Both car-side detectors bail out on a same-day charge without SoC
    bounds. That is what keeps them from filing the same charge a second
    time — so the empty soc_from is not a gap, it is the lock."""
    print("== Ohne SoC-Grenzen laesst die Autoseite sie in Ruhe ==")
    app, ctx, v, wc = _der_echte_fall()
    L.match_all(device_key='wallbox')
    c = Charge.query.one()
    pruefe("soc_from leer", c.soc_from is None, True)
    pruefe("Herkunft steht in der Notiz",
           'Wallbox-Messung' in (c.notes or ''), True)
    ctx.pop()


# ── Und jetzt die stille Haelfte ───────────────────────────────────────────

def test_04_ein_fremdes_auto_an_voller_batterie_wird_nicht_angelegt():
    """26.07.: the house car sat at 100 % before, during and after, odometer
    unchanged. 13.2 kWh went through the box — not into this car."""
    print("== Fremdes Auto: der Wagen stand voll da ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(12, 34), 100, 31510)
    _sync(v, _t(13, 22), 100, 31510)
    _sync(v, _t(19, 44), 100, 31510)
    wc = _messung(_t(13, 48), 131, 13.184)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("bleibt offen", wc.match_state, 'unmatched')
    pruefe("und sagt warum", 'not filed' in (wc.match_note or ''), True)
    pruefe("keine Ladung entstanden", Charge.query.count(), 0)
    ctx.pop()


def test_05_ein_fremdes_auto_waehrend_der_akku_faellt():
    """31.07.: 31.9 kWh through the box while the house car went from 37 %
    to 33 %. A battery that LOSES charge was not the one being charged."""
    print("== Fremdes Auto: der Akku ist gefallen ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(8, 37), 61, 31751, ort=FERN)
    _sync(v, _t(16, 38), 37, 31809)
    _sync(v, _t(23, 50), 33, 31826)
    wc = _messung(_t(13, 34), 305, 31.921)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("keine Ladung entstanden", Charge.query.count(), 0)
    ctx.pop()


def test_06_ein_kurzer_ansteckvorgang_ist_keine_ladung():
    print("== 1,2 kWh sind kein Ladevorgang ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(17, 30), 70, 1000)
    _sync(v, _t(18, 10), 71, 1000, laedt=True)
    _sync(v, _t(19, 0), 71, 1000)
    wc = _messung(_t(17, 43), 29, 1.248)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("die Begruendung nennt die Schwelle",
           'counts as a charge' in (wc.match_note or ''), True)
    ctx.pop()


def test_07_solange_die_autoseite_noch_dran_sein_kann_wird_gewartet():
    """Its trigger is the first sync reporting is_charging=0 after the charge.
    Before that sync exists the entry may still arrive from the car."""
    print("== Ohne Sync nach dem Fenster wird gewartet ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(16, 40), 85, 34309)
    _sync(v, _t(16, 55), 85, 34309, laedt=True)
    wc = _messung(_t(15, 40), 93, 4.956)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("und der Grund sagt es",
           'may still file it' in (wc.match_note or ''), True)
    ctx.pop()


def test_08_der_zweite_weg_stand_zuhause_und_akku_stieg():
    """No charging flag anywhere — but the car stood at home on both sides of
    the window with the same odometer and came out fuller. A battery that
    gains while the car does not move was charging."""
    print("== Zweiter Weg: stand zuhause, Akku stieg ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(14, 50), 30, 2000)
    _sync(v, _t(18, 20), 46, 2000)
    wc = _messung(_t(15, 52), 120, 9.722)
    tally = L.match_all(device_key='wallbox')
    pruefe("angelegt", tally.get('created'), 1)
    c = Charge.query.one()
    pruefe("Ladestand NACHHER uebernommen", c.soc_to, 46)
    ctx.pop()


def test_09_wer_dazwischen_gefahren_ist_beweist_nichts():
    print("== Gegenprobe zum zweiten Weg: der Wagen ist gefahren ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(14, 50), 30, 2000)
    _sync(v, _t(18, 20), 46, 2040)          # 40 km dazwischen
    wc = _messung(_t(15, 52), 120, 9.722)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    ctx.pop()


def test_10_nicht_zuhause_ist_kein_beleg():
    print("== Gegenprobe: der Wagen stand woanders ==")
    app, ctx = _app()
    _config()
    v = _car()
    _sync(v, _t(14, 50), 30, 2000, ort=FERN)
    _sync(v, _t(18, 20), 46, 2000, ort=FERN)
    wc = _messung(_t(15, 52), 120, 9.722)
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    ctx.pop()


def test_11_zwei_autos_an_einer_box_werden_nicht_geraten():
    print("== Zwei gebundene Autos: niemand raet ==")
    app, ctx, v, wc = _der_echte_fall()
    _car(name='Zweitwagen', key='wallbox')
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("und der Grund sagt es",
           'more than one car' in (wc.match_note or ''), True)
    ctx.pop()


def test_12_nur_vermerken_legt_nichts_an():
    """``apply_mode='never'`` means annotate only. A link that may not even
    take a number over must not create a whole entry."""
    print("== 'nur vermerken' legt nichts an ==")
    app, ctx, v, wc = _der_echte_fall(apply_mode='never')
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("keine Ladung entstanden", Charge.query.count(), 0)
    ctx.pop()


def test_13_eine_vorhandene_ladung_gewinnt():
    """The normal path is untouched: where an entry exists, it is matched and
    nothing new is created."""
    print("== Gibt es die Ladung schon, wird sie nur zugeordnet ==")
    app, ctx, v, wc = _der_echte_fall()
    c = Charge(vehicle_id=v.id, date=TAG, charge_hour=15, charge_end_hour=17,
               kwh_loaded=5.0, charge_type='AC', soc_from=81, soc_to=87)
    c.calculate_fields(58.0, 0.92)
    db.session.add(c)
    db.session.commit()
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("zugeordnet", tally.get('matched'), 1)
    pruefe("es bleibt bei einer Ladung", Charge.query.count(), 1)
    ctx.pop()


def test_14_ein_zweites_mal_fuer_denselben_tag_wird_nicht_angelegt():
    """🔴 Found on a live system, not here: a full re-fetch answered about a
    charge again with a window shifted by minutes. The reading no longer
    matched (an entry already tied to ANOTHER reading is no candidate), so it
    came out unmatched — and this rule filed a SECOND charge for a day that
    already had one. Two had to be deleted by hand.

    `match_one` asks which entry a reading may be attached to. Before filing,
    the question is a different one: does a charge exist at all?
    """
    print("== Dieselbe Ladung noch einmal, leicht verschobenes Fenster ==")
    app, ctx, v, wc = _der_echte_fall()
    L.match_all(device_key='wallbox')
    pruefe("eine Ladung da", Charge.query.count(), 1)
    erste = Charge.query.one()
    pruefe("sie haengt an der ersten Messung", erste.wallbox_charge_id, wc.id)

    # Der Analyzer antwortet noch einmal: vier Minuten spaeter los, weniger kWh.
    zweite = _messung(_t(15, 44), 89, 4.563, solar=3.6, sid='m2')
    tally = L.match_all(device_key='wallbox')
    pruefe("nichts angelegt", tally.get('created', 0) or 0, 0)
    pruefe("es bleibt bei einer Ladung", Charge.query.count(), 1)
    pruefe("und der Grund nennt den Eintrag",
           'already filed' in (zweite.match_note or ''), True)
    ctx.pop()


def test_15_aber_eine_echte_zweite_ladung_am_selben_tag_darf():
    """Gegenprobe: zwei Ladungen an einem Tag sind normal — morgens und
    abends. Nur ein ueberlappendes Fenster ist eine Doppelung."""
    print("== Zweite Ladung am selben Tag, anderes Fenster ==")
    app, ctx, v, wc = _der_echte_fall()
    L.match_all(device_key='wallbox')
    pruefe("eine Ladung da", Charge.query.count(), 1)
    _sync(v, _t(21, 30), 87, 34309, laedt=True)
    _sync(v, _t(23, 50), 93, 34309)
    _messung(_t(21, 20), 120, 5.2, solar=0.0, sid='m3')
    tally = L.match_all(device_key='wallbox')
    pruefe("angelegt", tally.get('created'), 1)
    pruefe("jetzt zwei Ladungen", Charge.query.count(), 2)
    ctx.pop()
