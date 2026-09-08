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


def test_today_costs_no_attempt():
    """v3.0.114: ENTSO-E publishes late, so a charge from TODAY without
    data is not a failed lookup. Counting it would spend the whole retry
    budget within hours now that every sync kicks the backfill."""
    print("test_today_costs_no_attempt")
    from datetime import timedelta
    heute = date.today()
    gestern = heute - timedelta(days=1)
    app = make_app()
    id_today = add_charge(app, charge_type='AC', charge_hour=10, kwh_loaded=10,
                          co2_g_per_kwh=None, date=heute)
    id_yesterday = add_charge(app, charge_type='AC', charge_hour=10, kwh_loaded=10,
                              co2_g_per_kwh=None, date=gestern)

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity
    entsoe.get_co2_intensity_window = lambda *a, **k: None
    entsoe.get_co2_intensity = lambda *a, **k: None
    try:
        bf.backfill_co2(app)
        with app.app_context():
            t = db.session.get(Charge, id_today)
            y = db.session.get(Charge, id_yesterday)
            check((t.co2_attempts or 0) == 0, "today's charge costs no attempt")
            check(t.co2_g_per_kwh is None, "today's charge stays NULL, not poisoned")
            check((y.co2_attempts or 0) == 1, "yesterday's charge does count")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h


def test_kick_is_rate_limited():
    """The per-sync kick must not burn the retry budget when someone
    hammers "sync now" — but the deliberate kicks ignore the limit."""
    print("test_kick_is_rate_limited")
    app = make_app()
    add_charge(app, charge_type='AC', kwh_loaded=10, co2_g_per_kwh=None)

    spawned = {'n': 0}

    class _NoRunThread:
        def __init__(self, *a, **k):
            spawned['n'] += 1

        def start(self):
            bf._backfill_running = False      # thread target never runs

    orig_thread = bf.threading.Thread
    bf.threading.Thread = _NoRunThread
    bf._backfill_running = False
    bf._last_kick_ts = 0.0
    try:
        first = bf.start_backfill(app)                 # unforced, first ever
        second = bf.start_backfill(app)                # unforced, too soon
        forced = bf.start_backfill(app, force=True)    # deliberate kick
        wide = bf.start_backfill(app, min_interval_s=0)
    finally:
        bf.threading.Thread = orig_thread
        bf._backfill_running = False
        bf._last_kick_ts = 0.0
    check(first is True, "first unforced kick runs")
    check(second is False, "second unforced kick is rate limited")
    check(forced is True, "force=True ignores the rate limit")
    check(wide is True, "an explicit interval of 0 always runs")
    check(spawned['n'] == 3, "exactly the three allowed kicks spawned")


def test_reset_attempts_for_missing():
    """v3.0.114 one-off: rows written off by the cap get their budget back."""
    print("test_reset_attempts_for_missing")
    app = make_app()
    id_stuck = add_charge(app, charge_type='AC', kwh_loaded=10, co2_g_per_kwh=None)
    id_done = add_charge(app, charge_type='AC', kwh_loaded=10, co2_g_per_kwh=380)
    id_pv = add_charge(app, charge_type='PV', kwh_loaded=10, co2_g_per_kwh=None)
    with app.app_context():
        for cid in (id_stuck, id_done, id_pv):
            c = db.session.get(Charge, cid)
            c.co2_attempts = bf.CO2_MAX_ATTEMPTS
        db.session.commit()

    n = bf.reset_attempts_for_missing(app)
    with app.app_context():
        stuck = db.session.get(Charge, id_stuck)
        done = db.session.get(Charge, id_done)
        pv = db.session.get(Charge, id_pv)
        check(n == 1, "exactly the one missing grid charge was unfrozen")
        check(stuck.co2_attempts == 0, "written-off grid charge may retry again")
        check(done.co2_attempts == bf.CO2_MAX_ATTEMPTS,
              "a charge that already has CO2 is left alone")
        check(pv.co2_attempts == bf.CO2_MAX_ATTEMPTS,
              "PV never asks ENTSO-E, so it is left alone")
    check(bf.reset_attempts_for_missing(app) == 0, "a second run finds nothing")


def test_every_sync_kicks_the_backfill():
    """The gap this release closes: nothing retried between restarts."""
    print("test_every_sync_kicks_the_backfill")
    src = os.path.join(ROOT, 'services', 'vehicle', 'sync_service.py')
    with open(src, encoding='utf-8') as fh:
        text = fh.read()
    head = text.split('def _do_sync(')[0]          # inside _sync_one_vehicle
    check('from services.co2_backfill import start_backfill' in head,
          "the per-vehicle sync imports the backfill")
    check('start_backfill(app)' in head,
          "and kicks it (unforced, so it stays rate limited)")


def test_outage_costs_no_attempt():
    """v3.0.115: while ENTSO-E is unreachable every lookup returns None.
    Counting that as a failed lookup spent the whole retry budget during
    the platform outage that began on 2026-08-31, so the charges of the
    whole period would have stayed empty even after it came back."""
    print("test_outage_costs_no_attempt")
    from datetime import timedelta
    gestern = date.today() - timedelta(days=1)
    app = make_app()
    id_a = add_charge(app, charge_type='AC', charge_hour=8, kwh_loaded=10,
                      co2_g_per_kwh=None, date=gestern)
    id_b = add_charge(app, charge_type='AC', charge_hour=9, kwh_loaded=10,
                      co2_g_per_kwh=None, date=gestern - timedelta(days=1))

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity

    def down_window(*a, **k):
        entsoe._mark_call(True)          # wie der echte Dienst im Ausfall
        return None

    def down_hour(*a, **k):
        entsoe._mark_call(True)
        return None

    entsoe.get_co2_intensity_window = down_window
    entsoe.get_co2_intensity = down_hour
    try:
        bf.backfill_co2(app)             # muss abbrechen, nicht durchlaufen
        with app.app_context():
            a = db.session.get(Charge, id_a)
            b = db.session.get(Charge, id_b)
            check((a.co2_attempts or 0) == 0, "outage costs the first charge no attempt")
            check((b.co2_attempts or 0) == 0, "and the run stops before the next one")
            check(a.co2_g_per_kwh is None and b.co2_g_per_kwh is None,
                  "nothing was poisoned during the outage")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
        entsoe._mark_call(False)


def test_an_honest_no_data_still_counts():
    """The outage guard must not disarm the retry ceiling: when ENTSO-E
    answers and simply has nothing, that is still an attempt."""
    print("test_an_honest_no_data_still_counts")
    from datetime import timedelta
    app = make_app()
    id_x = add_charge(app, charge_type='AC', charge_hour=8, kwh_loaded=10,
                      co2_g_per_kwh=None, date=date.today() - timedelta(days=3))

    orig_w, orig_h = entsoe.get_co2_intensity_window, entsoe.get_co2_intensity

    def answered_empty(*a, **k):
        entsoe._mark_call(False)         # erreicht, nur ohne Daten
        return None

    entsoe.get_co2_intensity_window = answered_empty
    entsoe.get_co2_intensity = answered_empty
    try:
        bf.backfill_co2(app)
        with app.app_context():
            x = db.session.get(Charge, id_x)
            check((x.co2_attempts or 0) == 1, "an answered-but-empty lookup counts")
    finally:
        entsoe.get_co2_intensity_window = orig_w
        entsoe.get_co2_intensity = orig_h
        entsoe._mark_call(False)


def test_the_service_marks_its_own_failures():
    """The flag is set by the service itself, not by the caller."""
    print("test_the_service_marks_its_own_failures")
    from datetime import datetime as _dt
    entsoe._mark_call(False)
    # entsoe-py is not installed in the test environment, so the import
    # inside the service fails — the ImportError path must mark it too,
    # because a missing library is not "no data" either.
    entsoe.get_co2_intensity('no-key', _dt(2026, 3, 1), hour=8)
    check(entsoe.last_call_failed() is True,
          "a transport/import failure is marked")
    entsoe._mark_call(False)
    check(entsoe.last_call_failed() is False, "and can be cleared again")


if __name__ == '__main__':
    test_missing_filter()
    test_lookup_fallback()
    test_backfill_heals_and_bounds()
    test_retry_ceiling()
    test_start_backfill_no_double_spawn()
    test_today_costs_no_attempt()
    test_kick_is_rate_limited()
    test_reset_attempts_for_missing()
    test_every_sync_kicks_the_backfill()
    test_outage_costs_no_attempt()
    test_an_honest_no_data_still_counts()
    test_the_service_marks_its_own_failures()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nAll co2_backfill tests passed")
