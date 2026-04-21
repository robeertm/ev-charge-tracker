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

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ev-tracker-dev-key-change-me')
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(DATA_DIR, 'ev_tracker.db').replace(os.sep, '/')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    ENTSOE_API_KEY = os.environ.get('ENTSOE_API_KEY', '')
    ENTSOE_COUNTRY = 'DE'

    APP_VERSION = '2.28.29'
    GITHUB_REPO = 'robeertm/ev-charge-tracker'
    APP_HOST = os.environ.get('APP_HOST', '0.0.0.0')
    APP_PORT = int(os.environ.get('APP_PORT', '7654'))

    BATTERY_CAPACITY_KWH = 64.0
    CAR_MODEL = 'Kia Niro EV 64kWh MY21'
