# -*- coding: utf-8 -*-
"""The wallbox link: which reading belongs to which car, and what it may change.

The link takes a measurement made by a meter that cannot see cars and files it
against entries about cars. Everything interesting is in that seam, and almost
every way of getting it wrong is silent:

* attaching a reading to the wrong car moves someone else's kilowatt-hours into
  your running costs, and the numbers still look perfectly plausible;
* overwriting a price somebody typed in discards a decision without saying so;
* reporting "0 % sun" where nothing was measured is a claim, not a gap;
* filing the same charge twice doubles a month.

So each rule below is checked from both sides — it fires when it should AND
stays quiet when it should not.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evlink-'))

from flask import Flask                                            # noqa: E402
from models.database import db, AppConfig, Charge, Vehicle, WallboxCharge  # noqa: E402
from services import shelly_link as L                              # noqa: E402

def pruefe(name, ist, soll):
    """One checked claim. Prints it either way, fails loudly when it is wrong.

    Printing the passes too is deliberate: half of these say a rule must NOT
    fire, and a silent suite makes a check that was accidentally removed look
    exactly like one that passed.
    """
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


def _config(**kw):
    AppConfig.set(L.K_ENABLED, kw.get('enabled', '1'))
    AppConfig.set(L.K_URL, kw.get('url', 'https://box.invalid:8765'))
    AppConfig.set(L.K_TOKEN, kw.get('token', 'k'))
    AppConfig.set(L.K_APPLY, kw.get('apply_mode', 'auto'))
    AppConfig.set(L.K_TOL, kw.get('tol', L.DEFAULT_TOLERANCE_MIN))


def _car(name, key='', enabled=True, battery=64.0):
    v = Vehicle(name=name, battery_kwh=battery,
                wallbox_link_enabled=enabled, wallbox_device_key=key)
    db.session.add(v)
    db.session.commit()
    return v


def _charge(vehicle, d, h_from, h_to, kwh=20.0, ctype='AC', price=None,
            needs_review=False):
    c = Charge(vehicle_id=vehicle.id, date=d, charge_hour=h_from,
               charge_end_hour=h_to, kwh_loaded=kwh, charge_type=ctype,
               eur_per_kwh=price, needs_review=needs_review,
               soc_from=20, soc_to=70)
    c.calculate_fields(vehicle.battery_kwh, 0.88)
    db.session.add(c)
    db.session.commit()
    return c


def _payload(*charges, device='wallbox'):
    return {'wallbox': {'device_key': device, 'name': 'Wallbox'},
            'charges': list(charges)}


def _reading(sid, start: datetime, hours=2.0, kwh=20.0, solar=None,
             battery=None, grid=None, cost=0.0, model='source', device='wallbox'):
    return {'id': sid, 'device_key': device,
            'start_ts': int(start.timestamp()),
            'end_ts': int((start + timedelta(hours=hours)).timestamp()),
            'energy_kwh': kwh, 'solar_kwh': solar, 'battery_kwh': battery,
            'grid_kwh': grid, 'cost_eur': cost, 'cost_model': model,
            'coverage': 1.0, 'avg_power_w': 7000.0, 'peak_power_w': 7400.0,
            'session_count': 1}


TAG = date.today() - timedelta(days=3)
MITTAG = datetime(TAG.year, TAG.month, TAG.day, 12, 0, 0)

def test_01_one_car_one_charge_the_ordinary_case():
    print("== One car, one charge: the ordinary case ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, needs_review=True)
    L.store_charges(_payload(_reading('a1', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    t = L.match_all()
    pruefe("it is matched", t['matched'], 1)
    wc = WallboxCharge.query.filter_by(source_id='a1').first()
    pruefe("to the right car", wc.vehicle_id, kia.id)
    pruefe("and the charge points back", Charge.query.get(c.id).wallbox_charge_id, wc.id)
    pruefe("the measured kWh were taken over", Charge.query.get(c.id).kwh_loaded, 20.5)
    pruefe("and an effective price with it",
           round(Charge.query.get(c.id).eur_per_kwh, 4), round(0.45 / 20.5, 4))
    pruefe("the split survived the trip", (wc.solar_kwh, wc.battery_kwh, wc.grid_kwh),
           (18.0, 1.0, 1.5))
    pruefe("solar share", wc.solar_share, round(19.0 / 20.5, 4))

    # The next two questions are about THIS charge, so they stay in the same
    # database rather than rebuilding one and hoping it comes out the same.
    print("== The same answer twice does not make two charges ==")
    L.store_charges(_payload(_reading('a1', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    pruefe("still one reading", WallboxCharge.query.count(), 1)
    L.match_all()
    pruefe("still one charge", Charge.query.count(), 1)

    print("== Undo puts back exactly what was there ==")
    wc = WallboxCharge.query.filter_by(source_id='a1').first()
    c = Charge.query.get(c.id)
    pruefe("it was adopted", wc.applied_at is not None, True)
    L.unapply_measurement(wc, c, kia.battery_kwh, 0.88)
    db.session.commit()
    pruefe("kWh restored", Charge.query.get(c.id).kwh_loaded, 19.0)
    pruefe("and nothing is left claiming it was adopted",
           WallboxCharge.query.filter_by(source_id='a1').first().applied_at, None)
    ctx.pop()


def test_04_a_fast_charger_is_not_a_wallbox():
    print("== A fast charger is not a wallbox ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    _charge(kia, TAG, 12, 13, ctype='DC')
    L.store_charges(_payload(_reading('b1', MITTAG)))
    t = L.match_all()
    pruefe("the DC charge is not claimed", t['matched'], 0)
    pruefe("and the reading says so", t['unmatched'], 1)
    ctx.pop()


def test_05_two_cars_in_the_same_window_refuse_do_not_guess():
    print("== Two cars in the same window: refuse, do not guess ==")
    app, ctx = _app()
    _config()
    kia, skoda = _car('Kia'), _car('Skoda')
    c1 = _charge(kia, TAG, 12, 14, kwh=20.0)
    c2 = _charge(skoda, TAG, 12, 14, kwh=22.0)
    L.store_charges(_payload(_reading('c1', MITTAG, kwh=20.1)))
    t = L.match_all()
    # 🔴 The energy of the reading is nearly the Kia's. Ranking by that would have
    # produced a confident, wrong answer — and nobody would ever have looked.
    pruefe("nothing is matched", t['matched'], 0)
    pruefe("it is called ambiguous", t['ambiguous'], 1)
    pruefe("neither charge was touched",
           (Charge.query.get(c1.id).wallbox_charge_id,
            Charge.query.get(c2.id).wallbox_charge_id), (None, None))
    pruefe("and the reason is written down",
           'more than one car' in (WallboxCharge.query.first().match_note or ''), True)
    ctx.pop()


def test_06_two_cars_hours_apart_each_gets_its_own():
    print("== Two cars, hours apart: each gets its own ==")
    app, ctx = _app()
    _config()
    kia, skoda = _car('Kia'), _car('Skoda')
    c1 = _charge(kia, TAG, 8, 10)
    c2 = _charge(skoda, TAG, 18, 20)
    L.store_charges(_payload(
        _reading('d1', MITTAG.replace(hour=8)),
        _reading('d2', MITTAG.replace(hour=18)),
    ))
    t = L.match_all()
    pruefe("both matched", t['matched'], 2)
    pruefe("morning is the Kia",
           WallboxCharge.query.filter_by(source_id='d1').first().vehicle_id, kia.id)
    pruefe("evening is the Skoda",
           WallboxCharge.query.filter_by(source_id='d2').first().vehicle_id, skoda.id)
    ctx.pop()


def test_07_a_reading_waits_for_its_charge_instead_of_being_dropped():
    print("== A reading waits for its charge instead of being dropped ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    L.store_charges(_payload(_reading('e1', MITTAG)))
    t = L.match_all()
    pruefe("nothing to match yet", t['unmatched'], 1)
    pruefe("but the reading is kept", WallboxCharge.query.count(), 1)
    c = _charge(kia, TAG, 12, 14)          # the car syncs later, the entry appears
    t = L.match_all()
    pruefe("the next pass finds it", t['matched'], 1)
    pruefe("without a second reading", WallboxCharge.query.count(), 1)
    ctx.pop()


def test_08_one_reading_one_charge_a_second_reading_may_not_steal_it():
    print("== One reading, one charge — a second reading may not steal it ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 14)
    L.store_charges(_payload(_reading('f1', MITTAG)))
    L.match_all()
    L.store_charges(_payload(_reading('f2', MITTAG + timedelta(minutes=20))))
    t = L.match_all()
    pruefe("the taken charge stays with the first", 
           WallboxCharge.query.filter_by(source_id='f1').first().charge_id, c.id)
    pruefe("the second finds nothing free",
           WallboxCharge.query.filter_by(source_id='f2').first().match_state, 'unmatched')
    ctx.pop()


def test_09_how_far_the_two_clocks_may_disagree():
    print("== How far the two clocks may disagree ==")
    app, ctx = _app()
    _config(tol=90)
    kia = _car('Kia')
    _charge(kia, TAG, 12, 13)
    L.store_charges(_payload(_reading('g1', MITTAG + timedelta(minutes=80), hours=1)))
    pruefe("80 minutes late still matches", L.match_all()['matched'], 1)
    ctx.pop()

    app, ctx = _app()
    _config(tol=90)
    kia = _car('Kia')
    _charge(kia, TAG, 12, 13)
    L.store_charges(_payload(_reading('g2', MITTAG + timedelta(hours=6), hours=1)))
    pruefe("six hours later does not", L.match_all()['matched'], 0)
    ctx.pop()


def test_10_over_midnight():
    print("== Over midnight ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 23, 2)           # 23:00 -> 02:59 the next day
    s, e = L.charge_window(c)
    pruefe("the window rolls into the next day", (e - s), timedelta(hours=4))
    L.store_charges(_payload(_reading('h1', MITTAG.replace(hour=23), hours=3)))
    pruefe("and the night charge matches", L.match_all()['matched'], 1)
    ctx.pop()


def test_11_what_may_be_overwritten_and_what_may_not():
    """v3.0.129 changed one line of this table on purpose.

    "auto" used to leave a typed-in price alone, which sounds careful and
    leaves the worse number standing exactly where a better one exists: at the
    owner's own wallbox the meter sat in the wire. Robert: „zu hause/wallbox >
    alles automatisch". Every adoption stays reversible, so nothing is lost —
    and "never" still means never.
    """
    print("== What may be overwritten, and what may not ==")
    for mode, typed, soll, name in (
        ('auto', None, True, "auto adopts an entry with no price"),
        ('auto', 0.41, True, "auto now prefers the meter over a typed price"),
        ('always', 0.41, True, "always overwrites it"),
        ('never', None, False, "never changes anything"),
    ):
        app, ctx = _app()
        _config(apply_mode=mode)
        kia = _car('Kia')
        c = _charge(kia, TAG, 12, 14, kwh=19.0, price=typed)
        L.store_charges(_payload(_reading('i1', MITTAG, kwh=20.5, solar=20.0,
                                          battery=0.0, grid=0.5, cost=0.15)))
        L.match_all()
        c = Charge.query.get(c.id)
        pruefe(name, c.kwh_loaded == 20.5, soll)
        if mode != 'never':
            pruefe("  ...and it is filed either way",
                   WallboxCharge.query.first().match_state, 'matched')
        ctx.pop()


def test_12_an_unmeasured_charge_says_nothing_rather_than_zero():
    print("== An unmeasured charge says nothing rather than zero ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    _charge(kia, TAG, 12, 14)
    L.store_charges(_payload(_reading('j1', MITTAG, kwh=20.0, solar=None,
                                      battery=None, grid=None, cost=6.0,
                                      model='fixed')))
    L.match_all()
    wc = WallboxCharge.query.first()
    pruefe("the shares stay empty", (wc.solar_kwh, wc.battery_kwh, wc.grid_kwh),
           (None, None, None))
    pruefe("no share is computed from them", wc.solar_share, None)
    pruefe("and the payload marks it unmeasured", wc.to_dict()['measured'], False)
    ctx.pop()


def test_13_a_car_that_was_never_bound_is_nobody_s_candidate():
    print("== A car that was never bound is nobody's candidate ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia', enabled=False)
    _charge(kia, TAG, 12, 14)
    L.store_charges(_payload(_reading('k1', MITTAG)))
    pruefe("not matched", L.match_all()['matched'], 0)
    kia.wallbox_link_enabled = True
    db.session.commit()
    pruefe("until it is", L.match_all()['matched'], 1)
    ctx.pop()


def test_14_two_wallboxes_a_car_bound_to_one_is_not_offered_the_other():
    print("== Two wallboxes: a car bound to one is not offered the other ==")
    app, ctx = _app()
    _config()
    garage = _car('Garage-Auto', key='wb_garage')
    carport = _car('Carport-Auto', key='wb_carport')
    cg = _charge(garage, TAG, 12, 14)
    cc = _charge(carport, TAG, 12, 14)
    L.store_charges(_payload(_reading('m1', MITTAG, device='wb_garage'),
                             device='wb_garage'))
    t = L.match_all(device_key='wb_garage')
    # Both cars charged at noon, but only one of them on THIS box — so this is a
    # clean match and not the ambiguity two cars on one box would be.
    pruefe("the garage reading finds the garage car", t['matched'], 1)
    pruefe("and it is the right one",
           WallboxCharge.query.filter_by(source_id='m1').first().vehicle_id, garage.id)
    ctx.pop()


def test_15_the_settings_are_coerced_not_trusted():
    print("== The settings are coerced, not trusted ==")
    app, ctx = _app()
    AppConfig.set(L.K_ENABLED, 'yes')
    AppConfig.set(L.K_URL, 'https://box:8765/')
    AppConfig.set(L.K_TOKEN, '  k  ')
    AppConfig.set(L.K_TOL, 'soon')
    AppConfig.set(L.K_APPLY, 'whenever')
    AppConfig.set(L.K_BACKFILL, '99999')
    s = L.settings()
    pruefe("the trailing slash is gone", s['url'], 'https://box:8765')
    pruefe("the token is trimmed", s['token'], 'k')
    pruefe("nonsense tolerance falls back", s['tolerance_min'], L.DEFAULT_TOLERANCE_MIN)
    pruefe("nonsense mode falls back", s['apply_mode'], 'auto')
    pruefe("the backfill is capped", s['backfill_days'], L.MAX_DAYS)
    pruefe("and it counts as configured", L.configured(s), True)
    AppConfig.set(L.K_TOKEN, '')
    pruefe("without a token it does not", L.configured(), False)
    ctx.pop()


# ══════════════════════════════════════════════════════════════════════════
# Zu Hause heisst: alles automatisch
# ══════════════════════════════════════════════════════════════════════════

def test_20_a_measured_home_charge_stops_asking_to_be_checked():
    """Robert: „soll er nicht prüfen hinschreiben sondern es richtig eintragen".

    The flag asks "is this entry right?". For a charge at the owner's own
    wallbox the answer arrives from the wire, with a curve behind it — so the
    question is answered, not still open.
    """
    print("== A measured home charge stops asking to be checked ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, ctype='AC', needs_review=True)
    c.notes = L._PRUEFNOTIZ_ZURUECK
    db.session.commit()
    L.store_charges(_payload(_reading('h1', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    L.match_all()
    c = Charge.query.get(c.id)
    pruefe("the review flag is gone", c.needs_review, False)
    pruefe("and the note no longer asks", c.notes, L._GEMESSEN_NOTIZ)
    pruefe("92 % own power makes it a PV charge", c.charge_type, 'PV')

    print("== ...and the undo puts the question back ==")
    wc = WallboxCharge.query.filter_by(source_id='h1').first()
    L.unapply_measurement(wc, c, kia.battery_kwh, 0.88)
    db.session.commit()
    c = Charge.query.get(c.id)
    pruefe("flag back", c.needs_review, True)
    pruefe("type back", c.charge_type, 'AC')
    pruefe("note back", c.notes, L._PRUEFNOTIZ_ZURUECK)
    pruefe("kWh back", c.kwh_loaded, 19.0)
    ctx.pop()


def test_21_the_type_follows_the_measured_mix_and_only_that():
    print("== The type follows the measured mix — and only that ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')

    # (a) Mostly grid: a home charge, but not a PV one.
    c1 = _charge(kia, TAG, 3, 4, kwh=10.0, ctype='AC')
    L.store_charges(_payload(_reading('n1', datetime(TAG.year, TAG.month, TAG.day, 3),
                                      kwh=10.0, solar=0.2, battery=0.3, grid=9.5,
                                      cost=2.9)))
    L.match_all()
    pruefe("5 % own stays AC", Charge.query.get(c1.id).charge_type, 'AC')

    # (b) Exactly at the threshold — 90 % counts as PV.
    c2 = _charge(kia, TAG, 12, 13, kwh=10.0, ctype='AC')
    L.store_charges(_payload(_reading('n2', MITTAG, kwh=10.0, solar=8.0,
                                      battery=1.0, grid=1.0, cost=0.3)))
    L.match_all()
    pruefe("exactly 90 % is PV", Charge.query.get(c2.id).charge_type, 'PV')

    # (c) 🔴 No supply measurement at all: the meter knows the kWh, nothing
    #     about where they came from. Typing that as AC would turn "nobody
    #     measured" into "it was grid power".
    c3 = _charge(kia, TAG, 18, 19, kwh=10.0, ctype='PV', needs_review=True)
    L.store_charges(_payload(_reading('n3', datetime(TAG.year, TAG.month, TAG.day, 18),
                                      kwh=11.0, model='fixed', cost=3.3)))
    L.match_all()
    c3 = Charge.query.get(c3.id)
    pruefe("an unmeasured split leaves the type alone", c3.charge_type, 'PV')
    pruefe("but the measured kWh still count", c3.kwh_loaded, 11.0)
    pruefe("and the entry is still confirmed", c3.needs_review, False)
    wc3 = WallboxCharge.query.filter_by(source_id='n3').first()
    pruefe("and it says the split is NOT known", wc3.split_known, False)

    # (d) 🔴 The house with no PV and no battery: analyzer 16.86.0 reports its
    #     mix as all grid, on a flat tariff. That is a KNOWN split, not a
    #     missing one — asking cost_model here would have hidden the red bar
    #     from exactly the houses whose charge is entirely red.
    c4 = _charge(kia, TAG, 20, 21, kwh=9.0, ctype='PV', needs_review=True)
    L.store_charges(_payload(_reading('n4', datetime(TAG.year, TAG.month, TAG.day, 20),
                                      kwh=9.5, solar=0.0, battery=0.0, grid=9.5,
                                      model='fixed', cost=2.87)))
    L.match_all()
    c4 = Charge.query.get(c4.id)
    wc4 = WallboxCharge.query.filter_by(source_id='n4').first()
    pruefe("a grid-only house knows its split", wc4.split_known, True)
    pruefe("so the entry is typed AC, not left as PV", c4.charge_type, 'AC')
    pruefe("and it no longer asks to be checked", c4.needs_review, False)
    ctx.pop()


def test_22_at_home_the_meter_wins_even_over_a_typed_price():
    """„zu hause/wallbox > alles automatisch". The meter sat in the wire; the
    entry holds what the car said about its own battery. Reversible, so
    preferring the measurement costs nothing."""
    print("== At home the meter wins, even over a typed price ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, price=0.32)   # somebody typed this
    L.store_charges(_payload(_reading('p1', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    L.match_all()
    c = Charge.query.get(c.id)
    pruefe("the meter's kWh stand", c.kwh_loaded, 20.5)
    pruefe("and its effective price", round(c.eur_per_kwh, 4), round(0.45 / 20.5, 4))

    print("== ...but 'never' still means never ==")
    app2, ctx2 = _app()
    _config(apply_mode='never')
    kia2 = _car('Kia')
    c2 = _charge(kia2, TAG, 12, 13, kwh=19.0, price=0.32, needs_review=True)
    L.store_charges(_payload(_reading('p2', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    L.match_all()
    c2 = Charge.query.get(c2.id)
    pruefe("nothing was taken over", c2.kwh_loaded, 19.0)
    pruefe("the flag stays, because nothing was confirmed", c2.needs_review, True)
    pruefe("and the type stays", c2.charge_type, 'AC')
    ctx2.pop()


def test_23_a_note_somebody_typed_is_never_touched():
    print("== A note somebody typed is never touched ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, needs_review=True)
    c.notes = 'Nachbar hat mitgeladen, bitte prüfen'
    db.session.commit()
    L.store_charges(_payload(_reading('t1', MITTAG, kwh=20.5, solar=18.0,
                                      battery=1.0, grid=1.5, cost=0.45)))
    L.match_all()
    c = Charge.query.get(c.id)
    pruefe("the note is still the owner's", c.notes, 'Nachbar hat mitgeladen, bitte prüfen')
    pruefe("the flag is still cleared", c.needs_review, False)
    ctx.pop()


def test_24_an_adoption_from_before_the_rule_is_brought_along_once():
    """A reading that was already taken over never passes the matcher again —
    it only retries what is not matched. So without a one-off pass the new
    rule would reach new charges only, and the owner would keep looking at
    older home charges that still ask to be checked."""
    print("== An adoption from before the rule is brought along — once ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, ctype='AC', needs_review=True)
    L.store_charges(_payload(_reading('o1', MITTAG, kwh=20.5, solar=19.0,
                                      battery=0.5, grid=1.0, cost=0.3)))
    L.match_all()
    # Put the row back into the state a v3.0.128 installation would have it in:
    # taken over, but nothing said about the flag or the type.
    wc = WallboxCharge.query.filter_by(source_id='o1').first()
    c = Charge.query.get(c.id)
    c.needs_review = True
    c.charge_type = 'AC'
    wc.prev_needs_review = None
    wc.prev_charge_type = None
    db.session.commit()

    n = L.hole_versaeumtes_nach('auto')['retyped']
    c = Charge.query.get(c.id)
    pruefe("one row was brought along", n, 1)
    pruefe("it stops asking to be checked", c.needs_review, False)
    pruefe("and is typed from its mix", c.charge_type, 'PV')
    pruefe("the undo knows what it found", 
           WallboxCharge.query.filter_by(source_id='o1').first().prev_needs_review, True)

    print("== ...and only once ==")
    pruefe("a second pass finds nothing",
           L.hole_versaeumtes_nach('auto')['retyped'], 0)

    print("== ...and never behind 'never' ==")
    pruefe("mode never does nothing at all",
           L.hole_versaeumtes_nach('never'), {'applied': 0, 'retyped': 0})
    ctx.pop()


def test_25_a_match_the_old_rule_declined_is_settled_later():
    """The matcher retries only what is NOT matched — so a reading it filed
    but did not adopt (mode 'never', or the old 'auto' sparing a typed price)
    was stuck there forever, even after the setting changed."""
    print("== A match the rule of the day declined is settled later ==")
    app, ctx = _app()
    _config(apply_mode='never')
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 13, kwh=19.0, price=0.32, ctype='AC', needs_review=True)
    L.store_charges(_payload(_reading('s1', MITTAG, kwh=20.5, solar=19.5,
                                      battery=0.5, grid=0.5, cost=0.15)))
    L.match_all()
    pruefe("filed but not adopted", Charge.query.get(c.id).kwh_loaded, 19.0)

    # The owner switches the setting. Nothing new arrives from the analyzer.
    _config(apply_mode='auto')
    nach = L.hole_versaeumtes_nach('auto')
    c = Charge.query.get(c.id)
    pruefe("the measurement is taken over now", nach['applied'], 1)
    pruefe("with the meter's kWh", c.kwh_loaded, 20.5)
    pruefe("and the entry is typed and confirmed",
           (c.charge_type, c.needs_review), ('PV', False))

    print("== ...but an undo is a decision, not a gap ==")
    wc = WallboxCharge.query.filter_by(source_id='s1').first()
    L.unapply_measurement(wc, c, kia.battery_kwh, 0.88)
    db.session.commit()
    nach2 = L.hole_versaeumtes_nach('auto')
    c = Charge.query.get(c.id)
    pruefe("the catch-up leaves it alone", nach2['applied'], 0)
    pruefe("and what the owner restored stands", c.kwh_loaded, 19.0)

    print("== ...until they take it over themselves again ==")
    wc = WallboxCharge.query.filter_by(source_id='s1').first()
    L.apply_measurement(wc, c, kia.battery_kwh, 0.88)
    db.session.commit()
    pruefe("the undo marker is cleared",
           WallboxCharge.query.filter_by(source_id='s1').first().undone_at, None)
    ctx.pop()


# ══════════════════════════════════════════════════════════════════════════
# Der CO2-Anteil einer Mischladung
# ══════════════════════════════════════════════════════════════════════════

def test_26_the_co2_of_a_mixed_charge_is_mixed_too():
    """Robert: „wird eigentlich dann der co2 anteil auch richtig berechnet bei
    mischladungen?"

    It was not. The app books ONE intensity per charge, and a charge of 5.63
    kWh sunshine plus 0.011 kWh grid was booked at the grid mix — roughly ten
    times too much for a nearly carbon-free charge. Measured on a real
    installation before this was built.
    """
    print("== The CO2 of a mixed charge is mixed too ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 14, kwh=10.0)
    c.co2_g_per_kwh = 400          # what ENTSO-E said for that window
    c.calculate_fields(kia.battery_kwh, 0.88)
    db.session.commit()
    L.store_charges(_payload(_reading('c1', MITTAG, kwh=10.0, solar=8.0,
                                      battery=1.0, grid=1.0, cost=0.3)))
    L.match_all(pv_co2=40)          # the owner's own PV figure, g/kWh
    c = Charge.query.get(c.id)
    # (8 + 1) kWh own at 40 g + 1 kWh grid at 400 g, over 10 kWh
    pruefe("the intensity follows the mix", c.co2_g_per_kwh, 76)
    pruefe("and the kilograms follow the intensity", c.co2_kg, round(10.0 * 76 / 1000, 2))

    print("== ...and a second pass does not mix the mix ==")
    wc = WallboxCharge.query.filter_by(source_id='c1').first()
    L.apply_measurement(wc, c, kia.battery_kwh, 0.88, 40)
    db.session.commit()
    pruefe("still the same number", Charge.query.get(c.id).co2_g_per_kwh, 76)
    pruefe("and the original grid value is kept for the undo",
           wc.prev_co2_g_per_kwh, 400)

    print("== ...and the undo puts the grid value back ==")
    L.unapply_measurement(wc, c, kia.battery_kwh, 0.88)
    db.session.commit()
    pruefe("grid intensity restored", Charge.query.get(c.id).co2_g_per_kwh, 400)
    ctx.pop()


def test_27_where_a_figure_is_missing_nothing_is_invented():
    print("== Where a figure is missing, nothing is invented ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')

    # (a) No PV figure in Settings: the mix cannot be weighed, so the entry
    #     keeps the grid intensity rather than getting a made-up one.
    c1 = _charge(kia, TAG, 12, 13, kwh=10.0)
    c1.co2_g_per_kwh = 400
    db.session.commit()
    L.store_charges(_payload(_reading('m1', MITTAG, kwh=10.0, solar=9.0,
                                      battery=0.0, grid=1.0, cost=0.3)))
    L.match_all(pv_co2=None)
    pruefe("no PV figure: untouched", Charge.query.get(c1.id).co2_g_per_kwh, 400)

    # (b) No grid figure either (ENTSO-E was silent): still nothing invented,
    #     and the backfill thread will fill it in later.
    c2 = _charge(kia, TAG, 15, 16, kwh=10.0)
    db.session.commit()
    L.store_charges(_payload(_reading('m2', datetime(TAG.year, TAG.month, TAG.day, 15),
                                      kwh=10.0, solar=9.0, battery=0.0, grid=1.0,
                                      cost=0.3)))
    L.match_all(pv_co2=40)
    pruefe("no grid figure: stays empty", Charge.query.get(c2.id).co2_g_per_kwh, None)

    # (c) An unmeasured split: the meter knows the kWh, not their origin.
    c3 = _charge(kia, TAG, 18, 19, kwh=10.0)
    c3.co2_g_per_kwh = 400
    db.session.commit()
    L.store_charges(_payload(_reading('m3', datetime(TAG.year, TAG.month, TAG.day, 18),
                                      kwh=10.0, model='fixed', cost=3.0)))
    L.match_all(pv_co2=40)
    pruefe("unmeasured split: untouched", Charge.query.get(c3.id).co2_g_per_kwh, 400)

    # (d) A house without PV: all grid, so the mix IS the grid value.
    c4 = _charge(kia, TAG, 20, 21, kwh=10.0)
    c4.co2_g_per_kwh = 400
    db.session.commit()
    L.store_charges(_payload(_reading('m4', datetime(TAG.year, TAG.month, TAG.day, 20),
                                      kwh=10.0, solar=0.0, battery=0.0, grid=10.0,
                                      model='fixed', cost=3.0)))
    L.match_all(pv_co2=40)
    pruefe("grid-only house: unchanged, as it should be",
           Charge.query.get(c4.id).co2_g_per_kwh, 400)
    ctx.pop()


def test_28_an_entry_that_was_already_filed_still_gets_its_co2_fixed():
    """The rows on Robert's screen: taken over and re-typed under an older
    version, so the catch-up had already ticked them off — and their CO2 was
    never looked at again. Each job has to ask its own question."""
    print("== An entry that was already filed still gets its CO2 fixed ==")
    app, ctx = _app()
    _config()
    kia = _car('Kia')
    c = _charge(kia, TAG, 12, 14, kwh=10.0)
    c.co2_g_per_kwh = 341
    c.calculate_fields(kia.battery_kwh, 0.88)
    db.session.commit()
    L.store_charges(_payload(_reading('z1', MITTAG, kwh=10.0, solar=9.8,
                                      battery=0.1, grid=0.1, cost=0.05)))
    L.match_all()                       # no pv figure yet: CO2 untouched
    wc = WallboxCharge.query.filter_by(source_id='z1').first()
    c = Charge.query.get(c.id)
    pruefe("filed and re-typed", (wc.applied_at is not None, c.charge_type), (True, 'PV'))
    pruefe("but the CO2 is still the grid mix", c.co2_g_per_kwh, 341)
    pruefe("and the re-type job is ticked off", wc.prev_needs_review is not None, True)

    # The owner fills in their PV figure. Nothing new arrives from the
    # analyzer — and the entry must still be corrected.
    nach = L.hole_versaeumtes_nach('auto', pv_co2=40)
    c = Charge.query.get(c.id)
    pruefe("one CO2 was mixed", nach['co2'], 1)
    pruefe("and it follows the measured mix", c.co2_g_per_kwh,
           int(round((9.9 * 40 + 0.1 * 341) / 10.0)))
    pruefe("the original stays for the undo",
           WallboxCharge.query.filter_by(source_id='z1').first().prev_co2_g_per_kwh, 341)

    print("== ...and only once ==")
    pruefe("a second pass has nothing to do",
           L.hole_versaeumtes_nach('auto', pv_co2=40)['co2'], 0)
    ctx.pop()


def test_29_a_routine_poll_reaches_back_over_the_settle_window():
    """The gap that swallowed a real charge (2026-09-15, 6.2 kWh, PV surplus).

    The analyzer withholds a charge until it has been over for its settle
    window. A charge that ended a minute before our poll is withheld there;
    if the next poll asks "since that poll", the charge — now settled, ended
    before it — is never offered again. So a routine poll asks since the last
    poll MINUS the settle window (and an hour on top); filing is by id, the
    overlap costs nothing.
    """
    print("== A routine poll reaches back over the settle window ==")
    import time as _t
    # The pure rule first, from both sides.
    pruefe("the settle window and the grace hour are subtracted",
           L.since_for(10_000_000, {'settle_minutes': 20}),
           10_000_000 - 20 * 60 - L.SINCE_OVERLAP_GRACE_S)
    pruefe("an analyzer that names no settle gets the default one",
           L.since_for(10_000_000, {}), 10_000_000 - L.DEFAULT_SETTLE_MIN * 60 - L.SINCE_OVERLAP_GRACE_S)
    pruefe("nonsense in the settle field falls back too",
           L.since_for(10_000_000, {'settle_minutes': 'soon'}),
           10_000_000 - L.DEFAULT_SETTLE_MIN * 60 - L.SINCE_OVERLAP_GRACE_S)
    pruefe("never before the epoch", L.since_for(100, {'settle_minutes': 20}), 0)

    # Then the poll itself: what does sync() actually ask the analyzer?
    app, ctx = _app()
    _config()
    _car('Kia')
    letzter = int(_t.time()) - 1800                     # our last poll, half an hour ago
    AppConfig.set(L.K_LAST_TS, str(letzter))
    AppConfig.set(L.K_BACKFILL, '90')
    gefragt = []
    echt = L._get

    def _stub(cfg, route, params=None, timeout=25):
        gefragt.append((route, dict(params or {})))
        if route == 'info':
            return {'wallbox': {'device_key': 'wallbox', 'name': 'Wallbox'},
                    'settle_minutes': 20}
        return {'wallbox': {'device_key': 'wallbox', 'name': 'Wallbox'},
                'charges': [], 'pending_settle': 0}
    L._get = _stub
    try:
        res = L.sync(app)
    finally:
        L._get = echt
    pruefe("the pass ran", res['ok'], True)
    frage = dict(gefragt)['charges']
    pruefe("a routine poll asks with since, not days", 'since' in frage and 'days' not in frage, True)
    pruefe("and since lies the settle window plus an hour behind the last poll",
           frage['since'], letzter - 20 * 60 - L.SINCE_OVERLAP_GRACE_S)
    ctx.pop()
