"""Tests for services/co2_backfill.py (v3.0.92 self-healing backfill).

Uses a real in-memory SQLite DB via the app's own SQLAlchemy models,
and stubs services.entsoe_service so no network is touched. Run with:
  python3 tests/test_co2_backfill.py
Exit code is non-zero if any check fails.
"""
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import db, Charge, AppConfig  # noqa: E402
import services.entsoe_service as entsoe  # noqa: E402
import services.co2_backfill as bf  # noqa: E402

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


def add_charge(app, **kw):
    with app.app_context():
        c = Charge(date=kw.pop('date', date(2026, 3, 1)), **kw)
        db.session.add(c)
        db.session.commit()
        return c.id


def test_missing_filter():
    print("test_missing_filter")
    app = make_app()
    id_null = add_charge(app, charge_type='AC', kwh_loaded=10, co2_g_per_kwh=None)
    id_zero = add_charge(app, charge_type='DC', kwh_loaded=10, co2_g_per_kwh=0)
    id_ok = add_charge(app, charge_type='AC', kwh_loaded=10, co2_g_per_kwh=350)
    id_pv = add_charge(app, charge_type='PV', kwh_loaded=10, co2_g_per_kwh=None)
    with app.app_context():
        missing = {c.id for c in Charge.query.filter(bf.missing_co2_filter(Charge)).all()}
    check(id_null in missing, "NULL grid charge counts as missing")
    check(id_zero in missing, "poisoned 0 grid charge counts as missing")
    check(id_ok not in missing, "filled grid charge is not missing")
    check(id_pv not in missing, "PV charge is never counted missing")
    check(bf.get_missing_count(app) == 2, "get_missing_count = 2")


def test_lookup_fallback():
    print("test_lookup_fallback")
    app = make_app()
    # window returns None, single-hour returns None → daily average used
    calls = {'window': 0, 'hour': [], }
    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity

    def fake_window(key, s, e, **k):
        calls['window'] += 1
        return None

    def fake_hour(key, d, hour=None, **k):
        calls['hour'].append(hour)
        return 411 if hour is None else None  # only the daily avg has data

    entsoe.get_co2_intensity_window = fake_window
    entsoe.get_co2_intensity = fake_hour
    try:
        with app.app_context():
            c = Charge(date=date(2026, 3, 1), charge_type='AC',
                       charge_hour=8, charge_end_hour=10, kwh_loaded=10)
            got = bf._lookup_co2('k', c)
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
    check(got == 411, "falls back to daily average when hour/window empty")
    check(calls['window'] == 1, "window tried first")
    check(calls['hour'] == [8, None], "then start-hour, then daily average")


def test_backfill_heals_and_bounds():
    print("test_backfill_heals_and_bounds")
    app = make_app()
    id_poison = add_charge(app, charge_type='AC', charge_hour=9, kwh_loaded=20,
                           co2_g_per_kwh=0)          # previously poisoned
    id_nodata = add_charge(app, charge_type='DC', charge_hour=14, kwh_loaded=30,
                           co2_g_per_kwh=None, date=date(2026, 3, 2))

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity
    entsoe.get_co2_intensity_window = lambda *a, **k: None

    def fake_hour(key, d, hour=None, **k):
        # data exists for the poison charge's day, never for the nodata day
        if d.date() == date(2026, 3, 1):
            return 380
        return None

    entsoe.get_co2_intensity = fake_hour
    try:
        bf.backfill_co2(app)  # runs to completion (terminates: skip-set)
        with app.app_context():
            poison = db.session.get(Charge, id_poison)
            nodata = db.session.get(Charge, id_nodata)
            check(poison.co2_g_per_kwh == 380, "poisoned 0 row healed to real value")
            check(poison.co2_kg == round(20 * 380 / 1000, 2), "co2_kg recomputed")
            check(poison.co2_attempts == 0, "healed row attempts reset to 0")
            check(nodata.co2_g_per_kwh is None,
                  "no-data row NOT poisoned to 0 (stays NULL)")
            check(nodata.co2_attempts >= 1, "no-data row attempt counted")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h


def test_retry_ceiling():
    print("test_retry_ceiling")
    app = make_app()  # fresh DB so only the exhausted row is pending
    id_exhausted = add_charge(app, charge_type='AC', charge_hour=1, kwh_loaded=5,
                              co2_g_per_kwh=None, date=date(2026, 4, 1))
    with app.app_context():
        ex = db.session.get(Charge, id_exhausted)
        ex.co2_attempts = bf.CO2_MAX_ATTEMPTS
        db.session.commit()
    check(bf.get_missing_count(app) == 1, "exhausted row still counts as missing")

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity
    entsoe.get_co2_intensity_window = lambda *a, **k: None
    hit = {'n': 0}

    def counting_hour(*a, **k):
        hit['n'] += 1
        return None
    entsoe.get_co2_intensity = counting_hour
    try:
        bf.backfill_co2(app)
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
    check(hit['n'] == 0, "row at CO2_MAX_ATTEMPTS is not polled again")


def test_start_backfill_no_double_spawn():
    """start_backfill claims the running flag synchronously, so a second
    call in the same tick (boot self-heal firing right after the v3.0.65
    cleanup already kicked) is a no-op instead of a duplicate thread."""
    print("test_start_backfill_no_double_spawn")
    app = make_app()
    add_charge(app, charge_type='AC', charge_hour=9, kwh_loaded=20, co2_g_per_kwh=0)

    orig_thread = bf.threading.Thread
    spawned = {'n': 0}

    class _NoRunThread:
        def __init__(self, *a, **k):
            spawned['n'] += 1  # count the spawn but never run the target

        def start(self):
            pass

    bf._backfill_running = False
    bf.threading.Thread = _NoRunThread
    try:
        first = bf.start_backfill(app)
        second = bf.start_backfill(app)  # same tick, thread target hasn't run
    finally:
        bf.threading.Thread = orig_thread
        bf._backfill_running = False
    check(first is True, "first start_backfill kicks")
    check(second is False, "second start_backfill in same tick is a no-op")
    check(spawned['n'] == 1, "only one thread spawned")


if __name__ == '__main__':
    test_missing_filter()
    test_lookup_fallback()
    test_backfill_heals_and_bounds()
    test_retry_ceiling()
    test_start_backfill_no_double_spawn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nAll co2_backfill tests passed")
