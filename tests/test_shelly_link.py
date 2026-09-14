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
    print("== What may be overwritten, and what may not ==")
    for mode, typed, soll, name in (
        ('auto', None, True, "auto adopts an entry with no price"),
        ('auto', 0.41, False, "auto leaves a price somebody typed alone"),
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
