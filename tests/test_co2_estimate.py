"""Tests for services/co2_estimate.py (v3.0.116 fallback estimate).

The energy analyzer keeps its CO2 tab populated during a grid-platform
outage by writing estimates that carry their own source label, so a real
value overwrites them later. This is the same idea for charges, with the
baseline taken from the install's OWN real values instead of a weather
model.

Uses a real in-memory SQLite DB via the app's own models, no network.
Run with:  python3 tests/test_co2_estimate.py
"""
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import db, Charge, AppConfig  # noqa: E402
import services.co2_estimate as est  # noqa: E402
import services.co2_backfill as bf  # noqa: E402
import services.entsoe_service as entsoe  # noqa: E402

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        AppConfig.set('entsoe_api_key', 'test-key')
    return app


def add(app, **kw):
    with app.app_context():
        c = Charge(date=kw.pop('date', date.today() - timedelta(days=2)), **kw)
        db.session.add(c)
        db.session.commit()
        return c.id


def _history(app, hour, werte, tage_ab=3):
    """Real charges at one hour of day, oldest first."""
    ids = []
    for i, v in enumerate(werte):
        ids.append(add(app, charge_type='AC', charge_hour=hour, kwh_loaded=10,
                       co2_g_per_kwh=v, date=date.today() - timedelta(days=tage_ab + i)))
    return ids


# ── 1. Das Grundprofil ───────────────────────────────────────────────────
def test_the_baseline_uses_only_real_values():
    print("test_the_baseline_uses_only_real_values")
    app = make_app()
    _history(app, 8, [300, 320, 340])          # echt
    id_geschaetzt = add(app, charge_type='AC', charge_hour=8, kwh_loaded=10,
                        co2_g_per_kwh=999)
    with app.app_context():
        c = db.session.get(Charge, id_geschaetzt)
        c.co2_estimated = True
        db.session.commit()
    _history(app, 20, [500, 520], tage_ab=10)  # nur 2 -> zu wenig fuer die Stunde

    with app.app_context():
        by_hour, overall = est.baseline_from_history()
    check(by_hour.get(8) == 320, "hour 8 is the median of the three real values")
    check(20 not in by_hour, "an hour with too few samples gets no bucket")
    check(overall == 340, "the overall median ignores the estimated row")


def test_without_history_nothing_is_invented():
    print("test_without_history_nothing_is_invented")
    app = make_app()
    add(app, charge_type='AC', charge_hour=8, kwh_loaded=10, co2_g_per_kwh=300)
    with app.app_context():
        by_hour, overall = est.baseline_from_history()
    check(overall is None, "below the minimum sample count there is no baseline")
    check(est.fill_estimates(app) == 0, "and nothing is filled")


# ── 2. Der Schaetzwert je Ladung ─────────────────────────────────────────
def test_the_hour_wins_over_the_overall_median():
    print("test_the_hour_wins_over_the_overall_median")
    app = make_app()
    _history(app, 3, [100, 110, 120])          # Nacht: wenig CO2
    _history(app, 13, [400, 420, 440], tage_ab=20)
    with app.app_context():
        by_hour, overall = est.baseline_from_history()

        class _C:
            charge_hour = 3
        check(est.estimate_for(_C(), by_hour, overall) == 110,
              "a night charge is estimated from night values")

        class _D:
            charge_hour = 7                    # keine Historie zu dieser Stunde
        check(est.estimate_for(_D(), by_hour, overall) == int(round(overall)),
              "an hour without history falls back to the overall median")


# ── 3. Fuellen ───────────────────────────────────────────────────────────
def test_open_grid_charges_are_filled_and_marked():
    print("test_open_grid_charges_are_filled_and_marked")
    app = make_app()
    _history(app, 8, [300, 320, 340])
    _history(app, 9, [360, 380], tage_ab=30)   # zusammen 5 = Mindestmenge
    id_leer = add(app, charge_type='AC', charge_hour=8, kwh_loaded=20, co2_g_per_kwh=None)
    id_null = add(app, charge_type='DC', charge_hour=8, kwh_loaded=10, co2_g_per_kwh=0)
    id_pv = add(app, charge_type='PV', charge_hour=12, kwh_loaded=10, co2_g_per_kwh=None)
    id_echt = add(app, charge_type='AC', charge_hour=11, kwh_loaded=10, co2_g_per_kwh=250)

    n = est.fill_estimates(app)
    with app.app_context():
        leer = db.session.get(Charge, id_leer)
        null = db.session.get(Charge, id_null)
        pv = db.session.get(Charge, id_pv)
        echt = db.session.get(Charge, id_echt)
    check(n == 2, "the empty and the poisoned-0 grid charge were filled")
    check(leer.co2_g_per_kwh == 320 and bool(leer.co2_estimated),
          "the value comes from the same hour and is marked as an estimate")
    check(leer.co2_kg == round(20 * 320 / 1000, 2), "co2_kg follows the estimate")
    check(null.co2_g_per_kwh == 320, "the legacy 0 marker is treated as empty")
    check(pv.co2_g_per_kwh is None, "PV is never estimated from grid history")
    check(echt.co2_g_per_kwh == 250 and not echt.co2_estimated,
          "a charge that already has a real value is untouched")


def test_an_estimate_is_not_estimated_twice():
    print("test_an_estimate_is_not_estimated_twice")
    app = make_app()
    _history(app, 8, [300, 320, 340, 360, 380])
    add(app, charge_type='AC', charge_hour=8, kwh_loaded=10, co2_g_per_kwh=None)
    check(est.fill_estimates(app) == 1, "first run fills it")
    check(est.fill_estimates(app) == 0, "second run leaves it alone")


# ── 4. Der echte Wert gewinnt ────────────────────────────────────────────
def test_an_estimate_stays_on_the_backfills_list():
    print("test_an_estimate_stays_on_the_backfills_list")
    app = make_app()
    id_e = add(app, charge_type='AC', charge_hour=8, kwh_loaded=10, co2_g_per_kwh=320)
    with app.app_context():
        c = db.session.get(Charge, id_e)
        c.co2_estimated = True
        db.session.commit()
    check(bf.get_pending_count(app) == 1,
          "a charge carrying an estimate still owes its real value")
    check(bf.get_missing_count(app) == 0,
          "but the user is not told a number is missing when one is shown")


def test_the_real_value_replaces_the_estimate():
    print("test_the_real_value_replaces_the_estimate")
    app = make_app()
    id_e = add(app, charge_type='AC', charge_hour=8, kwh_loaded=10,
               co2_g_per_kwh=320, date=date.today() - timedelta(days=2))
    with app.app_context():
        c = db.session.get(Charge, id_e)
        c.co2_estimated = True
        db.session.commit()

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity
    entsoe.get_co2_intensity_window = lambda *a, **k: (entsoe._mark_call(False), 287)[1]
    entsoe.get_co2_intensity = lambda *a, **k: (entsoe._mark_call(False), 287)[1]
    try:
        bf.backfill_co2(app)
        with app.app_context():
            c = db.session.get(Charge, id_e)
        check(c.co2_g_per_kwh == 287, "the real value overwrites the estimate")
        check(not c.co2_estimated, "and the estimate marker is cleared")
        check(c.co2_kg == round(10 * 287 / 1000, 2), "co2_kg was recomputed")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
        entsoe._mark_call(False)


# ── 5. Der gemeldete Fall, Ende zu Ende ──────────────────────────────────
def test_an_outage_leaves_the_user_with_numbers():
    print("test_an_outage_leaves_the_user_with_numbers")
    app = make_app()
    _history(app, 8, [300, 320, 340, 360, 380])   # Median 340
    id_neu = add(app, charge_type='AC', charge_hour=8, kwh_loaded=15,
                 co2_g_per_kwh=None, date=date.today() - timedelta(days=1))

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity

    def down(*a, **k):
        entsoe._mark_call(True)
        return None

    entsoe.get_co2_intensity_window = down
    entsoe.get_co2_intensity = down
    try:
        bf.backfill_co2(app)
        with app.app_context():
            c = db.session.get(Charge, id_neu)
        check(c.co2_g_per_kwh == 340, "the outage no longer leaves the charge empty")
        check(bool(c.co2_estimated), "the number is marked as an estimate")
        check((c.co2_attempts or 0) == 0, "and the outage still costs no attempt")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
        entsoe._mark_call(False)


def test_the_view_marks_an_estimate():
    print("test_the_view_marks_an_estimate")
    quelle = os.path.join(ROOT, 'templates', '_history_section.html')
    with open(quelle, encoding='utf-8') as fh:
        text = fh.read()
    check("c.co2_estimated" in text, "the history table asks whether it is an estimate")
    check("t('hist.co2_estimated')" in text, "and explains it on hover")
    import json
    for lang in ('de', 'en', 'fr', 'es', 'it', 'nl'):
        with open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8') as fh:
            check('hist.co2_estimated' in json.load(fh), f"{lang} has the wording")


if __name__ == '__main__':
    test_the_baseline_uses_only_real_values()
    test_without_history_nothing_is_invented()
    test_the_hour_wins_over_the_overall_median()
    test_open_grid_charges_are_filled_and_marked()
    test_an_estimate_is_not_estimated_twice()
    test_an_estimate_stays_on_the_backfills_list()
    test_the_real_value_replaces_the_estimate()
    test_an_outage_leaves_the_user_with_numbers()
    test_the_view_marks_an_estimate()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nAll co2_estimate tests passed")
