import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
# On some mounted filesystems, SQLite may not work. Use a local fallback.
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Test if SQLite can write here
    import sqlite3
    test_db = os.path.join(DATA_DIR, '_test.db')
    conn = sqlite3.connect(test_db)
    conn.execute('CREATE TABLE IF NOT EXISTS _test (id INTEGER)')
    conn.close()
    os.unlink(test_db)
except Exception:
    DATA_DIR = os.path.join(os.path.expanduser('~'), '.ev-charge-tracker')
    os.makedirs(DATA_DIR, exist_ok=True)

# How long a connection waits for a competing writer before giving up
# with "database is locked". Shared by the engine connect_args and the
# per-connection PRAGMA so both layers agree.
SQLITE_BUSY_TIMEOUT_MS = 30000


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ev-tracker-dev-key-change-me')
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(DATA_DIR, 'ev_tracker.db').replace(os.sep, '/')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # v3.0.111: the app is multi-threaded (Flask request threads + the
    # vehicle sync loop + the nightly maintenance loop + the geocode
    # maintenance loop), so writer/reader contention on the single
    # SQLite file is normal operation, not an edge case. pysqlite's
    # default busy timeout is 5 s — short enough that one slow write on
    # a Pi's SD card turns every concurrent request into a
    # "database is locked" 500. 30 s gives a contended writer room to
    # finish instead. The matching PRAGMAs (incl. WAL) are applied per
    # connection in models.database.init_sqlite_pragmas().
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'timeout': SQLITE_BUSY_TIMEOUT_MS / 1000.0},
    }

    ENTSOE_API_KEY = os.environ.get('ENTSOE_API_KEY', '')
    ENTSOE_COUNTRY = 'DE'

    APP_VERSION = '3.0.111'
    GITHUB_REPO = 'robeertm/ev-charge-tracker'
    APP_HOST = os.environ.get('APP_HOST', '0.0.0.0')
    APP_PORT = int(os.environ.get('APP_PORT', '7654'))

    BATTERY_CAPACITY_KWH = 64.0
    CAR_MODEL = 'Kia Niro EV 64kWh MY21'
