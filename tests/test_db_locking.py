"""Tests for the v3.0.111 SQLite locking fixes.

Two independent regressions are covered:

1. ``services.trips_service.geocode_missing_events`` must never hold a
   write transaction open while it is out on the network. The old
   version assigned ``evt.address`` inside the loop and committed once
   at the end, so the next ``reverse()`` call autoflushed the pending
   UPDATE and opened a write transaction that stayed open across
   Nominatim's rate-limit sleep + HTTP timeout — for up to 200 events
   in a row.

2. ``models.database.init_sqlite_pragmas`` must put the database into
   WAL mode, so a reader is never blocked by a writer at all.

Uses real on-disk SQLite (WAL and lock escalation are meaningless for
``:memory:``) and stubs the network. Run with:
  python3 tests/test_db_locking.py
Exit code is non-zero if any check fails.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import (  # noqa: E402
    db, AppConfig, GeocodeCache, ParkingEvent, init_sqlite_pragmas,
)

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def make_app(tmpdir, name, pragmas=False, busy_timeout_s=None):
    """A real file-backed app DB. ``pragmas=False`` reproduces the
    pre-v3.0.111 default: a rollback journal, where a writer that spills
    the page cache locks readers out entirely.

    ``busy_timeout_s`` shortens how long a blocked connection waits
    before raising. Production uses 30 s; the control test below dials
    it right down so it doesn't have to sit through the real timeout to
    prove the point."""
    path = os.path.join(tmpdir, f'{name}.db')
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if busy_timeout_s is not None:
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {'timeout': busy_timeout_s},
        }
    db.init_app(app)
    with app.app_context():
        if pragmas:
            init_sqlite_pragmas(app, busy_timeout_ms=30000)
        db.create_all()
        AppConfig.set('app_language', 'de')
        AppConfig.set('auth_enabled', 'false')
    return app, path


def seed_events(app, n):
    with app.app_context():
        for i in range(n):
            db.session.add(ParkingEvent(
                lat=52.0 + i / 1000, lon=13.0 + i / 1000, label='home',
                arrived_at=datetime(2026, 8, 20, 10, i),
            ))
        db.session.commit()


def test_geocode_holds_no_write_lock():
    """The real ``reverse()`` queries GeocodeCache before its HTTP call —
    that query is the autoflush point that used to open the transaction.
    The stub reproduces that ordering exactly, then probes from a second
    connection whether a write lock is being held."""
    print("test_geocode_holds_no_write_lock")
    import services.geocode_service as gs
    import services.trips_service as ts

    with tempfile.TemporaryDirectory() as tmp:
        app, path = make_app(tmp, 'geo')
        seed_events(app, 5)

        observed = []

        def fake_reverse(lat, lon, language='de'):
            # Same first step as the real reverse(): a session query,
            # which triggers autoflush of anything dirty.
            GeocodeCache.query.filter_by(
                lat_key=f'{lat:.4f}', lon_key=f'{lon:.4f}').first()
            probe = sqlite3.connect(path, timeout=0.2)
            try:
                probe.execute('BEGIN IMMEDIATE')  # needs RESERVED
                probe.rollback()
                observed.append('free')
            except sqlite3.OperationalError:
                observed.append('LOCKED')
            finally:
                probe.close()
            return f'Addr {lat},{lon}'

        orig = gs.reverse
        gs.reverse = fake_reverse
        try:
            with app.app_context():
                filled = ts.geocode_missing_events(limit=5)
                addrs = [e.address for e in
                         ParkingEvent.query.order_by(ParkingEvent.id).all()]
        finally:
            gs.reverse = orig

        check(filled == 5, "all 5 events geocoded")
        check(all(a for a in addrs), "every address persisted")
        check('LOCKED' not in observed,
              f"no write lock held during any network call ({observed})")


def test_wal_reader_survives_open_writer():
    """With WAL on, a reader gets its snapshot immediately even while a
    writer holds an open, page-cache-spilling transaction. Without WAL
    that same reader dies with 'database is locked' — which is exactly
    the traceback this release fixes."""
    print("test_wal_reader_survives_open_writer")

    def run(pragmas, busy_timeout_s=None, hold_s=2.0):
        with tempfile.TemporaryDirectory() as tmp:
            app, path = make_app(tmp, 'wal', pragmas=pragmas,
                                 busy_timeout_s=busy_timeout_s)
            result = {}
            started = threading.Event()

            def writer():
                with app.app_context():
                    conn = db.engine.raw_connection()
                    cur = conn.cursor()
                    cur.execute('BEGIN IMMEDIATE')
                    blob = 'x' * 4000
                    # Exceed the default ~2 MB page cache so the write
                    # transaction escalates to an EXCLUSIVE lock.
                    for i in range(2000):
                        cur.execute(
                            "INSERT OR REPLACE INTO app_config(key,value) "
                            "VALUES(?,?)", (f'k{i}', blob))
                    started.set()
                    time.sleep(hold_s)   # stands in for slow I/O
                    conn.commit()
                    conn.close()

            def reader():
                started.wait(10)
                time.sleep(0.3)
                with app.app_context():
                    try:
                        result['value'] = AppConfig.get('auth_enabled')
                    except Exception as e:
                        result['error'] = type(e).__name__

            threads = [threading.Thread(target=writer),
                       threading.Thread(target=reader)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return result

    with_wal = run(pragmas=True, hold_s=2.0)
    check(with_wal.get('value') == 'false',
          f"WAL: reader succeeds against an open writer ({with_wal})")

    # Control: prove the test actually exercises the failure mode, so a
    # future refactor can't make this pass vacuously. The writer must
    # out-hold the reader's busy timeout — otherwise the reader merely
    # waits its turn and the check passes for the wrong reason.
    without = run(pragmas=False, busy_timeout_s=0.3, hold_s=2.0)
    check(without.get('error') == 'OperationalError',
          f"control: without WAL the same reader is locked out ({without})")


def test_init_sqlite_pragmas_reports_wal():
    print("test_init_sqlite_pragmas_reports_wal")
    from sqlalchemy import text
    with tempfile.TemporaryDirectory() as tmp:
        app, _ = make_app(tmp, 'pragma', pragmas=True)
        with app.app_context():
            mode = db.session.execute(text('PRAGMA journal_mode')).scalar()
            timeout = db.session.execute(text('PRAGMA busy_timeout')).scalar()
        check(str(mode).lower() == 'wal', f"journal_mode is wal (got {mode!r})")
        check(timeout == 30000, f"busy_timeout is 30000 ms (got {timeout!r})")


def test_pragmas_skipped_for_non_sqlite():
    """Guard clause: the helper must be a no-op on a non-SQLite URI
    rather than blowing up at boot."""
    print("test_pragmas_skipped_for_non_sqlite")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://user@localhost/nope'
    check(init_sqlite_pragmas(app) == '', "no-op on a non-sqlite URI")


if __name__ == '__main__':
    test_geocode_holds_no_write_lock()
    test_wal_reader_survives_open_writer()
    test_init_sqlite_pragmas_reports_wal()
    test_pragmas_skipped_for_non_sqlite()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nAll db_locking tests passed")
