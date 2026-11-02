# -*- coding: utf-8 -*-
"""Trip times for a car that is polled only rarely.

Measured on a live Renault install (polling mode ``cached``, every 4 h) — no
time-zone error, the container clock was right; the logic was wrong:

  1. An UNCHANGED poll writes no row, so the open stop's ``last_seen_at`` was
     never carried past the last CHANGED row. The departure of the next trip
     was taken from it and lay days too early (and the trip carried the wrong
     date).
  2. A new stop was dated with the poll time, not with the moment the car
     itself captured the data, which the connector knew but threw away.
  3. The nightly maintenance thread, woken at 02:59:59.x, computed a sleep of
     ``int(0.4) == 0`` and fired hundreds of times until 03:00:00.

Figures below are the measured ones; positions are invented.

Run with:  python3 -m pytest tests/test_fahrtzeit_seltene_abfrage.py
"""
import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ORT = (51.1242, 13.7204)

# Runs in a subprocess: ``config.DATA_DIR`` is fixed at import, and every case
# wants a database of its own and the Berlin clock.
_VORSPANN = """
import json, sys
sys.path.insert(0, %r)
from datetime import datetime
import app as A
from models.database import (db, AppConfig, Vehicle, VehicleSync, ParkingEvent,
                             VehicleTrip)
from sqlalchemy import event
from services.vehicle.base import VehicleStatus
from services import trips_service as T

app = A.create_app()
CLOCK = {'now': None}
A._poll_clock = lambda: CLOCK['now']

@event.listens_for(VehicleSync, 'before_insert')
def _stamp(mapper, conn, target):
    target.timestamp = CLOCK['now']

def poll(when, km, soc=50, captured=None, vid=None, ort=%r):
    CLOCK['now'] = when
    A._save_vehicle_sync(VehicleStatus(
        soc_percent=soc, odometer_km=km, location_lat=ort[0], location_lon=ort[1],
        captured_at=captured), 64.0, raw_json='{}', vehicle_id=vid)

def car(marke, name='Probe'):
    AppConfig.set('vehicle_sync_enabled', 'false')
    v = Vehicle(name=name, brand=marke, api_brand=marke)
    db.session.add(v); db.session.commit()
    return v.id

def stops(vid):
    return [(e.arrived_at.isoformat(), e.last_seen_at.isoformat() if e.last_seen_at else None,
             e.departed_at.isoformat() if e.departed_at else None, e.odometer_arrived)
            for e in ParkingEvent.query.filter_by(vehicle_id=vid).order_by(ParkingEvent.arrived_at)]
""" % (ROOT, ORT)


def _lauf(skript, tz='Europe/Berlin'):
    daten = tempfile.mkdtemp(prefix='evfz-')
    env = dict(os.environ, EV_DATA_DIR=daten, SECRET_KEY='fz-test', TZ=tz)
    p = subprocess.run([sys.executable, '-c', _VORSPANN + skript], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-3000:]
    zeilen = [z for z in p.stdout.strip().split('\n') if z.startswith('{')]
    assert zeilen, p.stdout[-2000:] + p.stderr[-2000:]
    return json.loads(zeilen[-1])


def pruefe(name, ist, soll):
    print(("  OK   " if ist == soll else "  FEHL ") + name + "   ist=%r soll=%r" % (ist, soll))
    assert ist == soll, "%s: ist=%r soll=%r" % (name, ist, soll)


_ECHTER_FALL = """
with app.app_context():
    vid = car(%(marke)r)
    D = datetime
    poll(D(2026, 10, 31, 15, 47, 9), 39, soc=50, vid=vid)
    poll(D(2026, 10, 31, 16, 52, 41), 39, soc=49, vid=vid)
    %(trip)s
    for t in (D(2026, 11, 1, 22, 7, 28), D(2026, 11, 2, 1, 42, 51), D(2026, 11, 2, 5, 42, 39)):
        poll(t, 39, soc=49, vid=vid)
    n_vorher = VehicleSync.query.count()
    poll(D(2026, 11, 2, 9, 42, 42), 54, soc=48, captured='2026-11-02T08:01:51Z', vid=vid)
    fahrten = T.get_trips(vehicle_id=vid)
    print(json.dumps({'stops': stops(vid), 'rows': n_vorher,
                      'fahrten': [(f['from'].get('departed_at'), f['to'].get('arrived_at'))
                                  for f in fahrten],
                      'captured': [s.captured_at.isoformat() if s.captured_at else None
                                   for s in VehicleSync.query.order_by(VehicleSync.timestamp)]}))
"""


# ── 1. der gemessene Fall ────────────────────────────────────────────

def test_01_echter_fall_abfahrt_aus_unveraenderter_abfrage_ankunft_aus_erfassungszeit():
    r = _lauf(_ECHTER_FALL % {'marke': 'renault', 'trip': ''})
    pruefe('unveraenderte Abfragen schreiben keine Zeile', r['rows'], 2)
    alt, neu = r['stops']
    pruefe('Abfahrt = letzte unveraenderte Abfrage', alt[2], '2026-11-02T05:42:39')
    pruefe('Ankunft = Erfassungszeit des Autos', neu[0], '2026-11-02T09:01:51')
    pruefe('last_seen des neuen Halts = Abfragezeit', neu[1], '2026-11-02T09:42:42')
    pruefe('die Fahrt traegt das Datum 02.11.',
           r['fahrten'][0], ['2026-11-02T05:42:39', '2026-11-02T09:01:51'])
    pruefe('captured_at als naive Ortszeit gespeichert',
           r['captured'][-1], '2026-11-02T09:01:51')


# ── 2. Sommer-/Winterzeit ────────────────────────────────────────────

class _Zone:
    def __init__(self, name):
        self.name = name
        self.vorher = os.environ.get('TZ')

    def __enter__(self):
        os.environ['TZ'] = self.name
        time.tzset()
        return self

    def __exit__(self, *_):
        if self.vorher is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.vorher
        time.tzset()


class _St:
    def __init__(self, c):
        self.captured_at = c


def _extrahiere(c):
    from app import _extract_captured_at
    return _extract_captured_at(_St(c))


def test_02_zeitumstellung():
    with _Zone('Europe/Berlin'):
        pruefe('Sommerzeit', _extrahiere('2026-10-24T08:00:00Z'), datetime(2026, 10, 24, 10, 0))
        pruefe('Winterzeit', _extrahiere('2026-10-26T08:00:00Z'), datetime(2026, 10, 26, 9, 0))
        a = _extrahiere('2026-10-25T00:30:00Z')
        b = _extrahiere('2026-10-25T01:30:00Z')
        pruefe('doppelte Stunde, CEST', a, datetime(2026, 10, 25, 2, 30))
        pruefe('doppelte Stunde, CET', b, datetime(2026, 10, 25, 2, 30))
        # Beide zeigen auf 02:30 Ortszeit — die Reihenfolge steckt in der
        # UTC-Zeit; als Uhrzeit sind sie gleich und duerfen nichts werfen.
        assert a <= b
        # aware datetime, naive datetime, Unsinn
        pruefe('aware datetime', _extrahiere(datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)),
               datetime(2026, 10, 24, 10, 0))
        pruefe('naive bleibt', _extrahiere(datetime(2026, 10, 24, 8, 0)), datetime(2026, 10, 24, 8, 0))
        pruefe('Unsinn', _extrahiere('gestern'), None)
        pruefe('None', _extrahiere(None), None)


# ── 3. unglaubwuerdige Erfassungszeit ────────────────────────────────

_RUECKFALL = """
with app.app_context():
    vid = car('renault')
    D = datetime
    poll(D(2026, 11, 1, 8, 0, 0), 100, vid=vid)
    poll(D(2026, 11, 1, 9, 0, 0), 100, soc=49, vid=vid)
    erg = {}
    for name, cap, wann, km in (('alt', '2026-11-01T07:00:00Z', D(2026, 11, 1, 12, 0, 0), 110),
                                ('zukunft', '2026-11-01T20:00:00Z', D(2026, 11, 1, 15, 0, 0), 120),
                                ('keine', None, D(2026, 11, 1, 18, 0, 0), 130)):
        poll(wann, km, soc=40, captured=cap, vid=vid)
        letzt = ParkingEvent.query.filter_by(vehicle_id=vid).order_by(ParkingEvent.arrived_at.desc()).first()
        erg[name] = letzt.arrived_at.isoformat()
    print(json.dumps(erg))
"""


def test_03_unglaubwuerdige_erfassungszeit_faellt_auf_die_abfragezeit_zurueck():
    r = _lauf(_RUECKFALL)
    # alt: 07:00Z = 08:00 Ortszeit, vor der Abfahrt des Halts (>= 09:00) -> Abfragezeit
    pruefe('Altwert', r['alt'], '2026-11-01T12:00:00')
    pruefe('Zukunft', r['zukunft'], '2026-11-01T15:00:00')
    pruefe('None', r['keine'], '2026-11-01T18:00:00')


# ── 4. andere Autos bleiben, wie sie waren ───────────────────────────

def test_04a_auto_mit_herstellerfahrten_unveraenderte_abfrage_aendert_nichts():
    r = _lauf(_ECHTER_FALL % {'marke': 'kia', 'trip': (
        "db.session.add(VehicleTrip(vehicle_id=vid, trip_date=D(2026, 10, 31).date(),"
        " start_time=D(2026, 10, 31, 10, 0, 0), distance_km=5.0)); db.session.commit()")})
    alt, _neu = r['stops']
    pruefe('last_seen unveraendert', alt[1], '2026-10-31T16:52:41')
    pruefe('Abfahrt wie bisher', alt[2], '2026-10-31T16:52:41')


def test_04b_andere_connectoren_setzen_kein_captured_at():
    import re
    from services.vehicle import registry  # noqa: F401
    vz = os.path.join(ROOT, 'services', 'vehicle')
    gefunden = {}
    for f in sorted(os.listdir(vz)):
        if f.startswith('connector_') and f.endswith('.py'):
            gefunden[f] = bool(re.search(r'captured_at\s*=', open(os.path.join(vz, f), encoding='utf-8').read()))
    pruefe('nur der Renault-Connector setzt captured_at',
           sorted(k for k, v in gefunden.items() if v), ['connector_renault.py'])
    # und das Feld hat den Standard None
    from services.vehicle.base import VehicleStatus
    pruefe('Standard None', VehicleStatus().captured_at, None)


def test_04c_captured_at_ist_kein_aenderungsfeld():
    from models.database import VehicleSync
    assert 'captured_at' not in VehicleSync.TRACKED_FIELDS


# ── 5. Renault-Connector gegen die echten Schemas ────────────────────

def test_05_renault_connector_liefert_captured_at():
    import pytest
    pytest.importorskip('renault_api')
    from renault_api.renault_session import RenaultSession  # noqa: F401
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    import _renault_fake as fake
    from services.vehicle import connector_renault as cr

    def lauf(batterie_ts, ort_ts):
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(cr, 'STATE_DIR', tempfile.mkdtemp(prefix='evrn-'))
            st = fake.install(lambda obj, name, val: mp.setattr(obj, name, val))
            st['battery'] = dict(fake.BATTERY, timestamp=batterie_ts)
            loc = dict(fake.LOCATION)
            if ort_ts is None:
                loc.pop('lastUpdateTime')
            else:
                loc['lastUpdateTime'] = ort_ts
            mp.setattr(fake, 'LOCATION', loc)
            c = cr.RenaultConnector({'username': 'a@example.invalid', 'password': 'x',
                                     'vin': fake.VIN})
            return c.get_status()
        finally:
            mp.undo()

    s = lauf('2026-11-02T08:01:51Z', '2026-11-02T07:55:00Z')
    pruefe('Standort-Zeit hat Vorrang', s.captured_at, '2026-11-02T07:55:00Z')
    s = lauf('2026-11-02T08:01:51Z', None)
    pruefe('sonst Batterie-Zeit', s.captured_at, '2026-11-02T08:01:51Z')


# ── 6. Nachtwartung feuert genau einmal ──────────────────────────────

def test_06_nachtwartung_feuert_einmal():
    import services.vehicle.sync_service as S

    start = datetime(2026, 11, 2, 2, 59, 59, 600000)
    uhr = {'t': start}

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return uhr['t']

    aufrufe = []
    schlaf = []
    orig = (S.datetime, S.time.sleep, S._maybe_daily_trip_reconcile, S._sync_running)

    def _sleep(sek):
        from datetime import timedelta
        schlaf.append(sek)
        uhr['t'] += timedelta(seconds=sek)
        # genug Zeit nach dem Feuern: 10 Minuten spaeter beenden
        if uhr['t'] > start.replace(hour=3, minute=10, second=0, microsecond=0) or len(schlaf) > 50:
            S._sync_running = False

    def _rec(app):
        aufrufe.append(uhr['t'])
        if len(aufrufe) >= 5:      # the old loop would go on for hundreds
            S._sync_running = False

    try:
        S.datetime = _Dt
        S.time.sleep = _sleep
        S._maybe_daily_trip_reconcile = _rec
        S._sync_running = True
        # Startup catch-up must not count: make it a no-op.
        import services.trip_reconcile as TR
        orig_run = TR.should_run_daily
        TR.should_run_daily = lambda: False

        class _App:
            def app_context(self):
                import contextlib
                return contextlib.nullcontext()
        # Stop after the first night: end the loop once the call happened and
        # the clock has passed 03:00:01.
        import threading
        t = threading.Thread(target=S._nightly_maintenance_loop, args=(_App(),), daemon=True)
        t.start()
        t.join(20)
        assert not t.is_alive(), 'the loop did not end'
    finally:
        S.datetime, S.time.sleep, S._maybe_daily_trip_reconcile, S._sync_running = orig
        TR.should_run_daily = orig_run
    pruefe('genau ein Aufruf', len(aufrufe), 1)
    assert min(schlaf) >= 1, schlaf


# ── 7. kein Oeffnen-Aufruf umgeht die Hilfsfunktion ──────────────────

def test_07_jeder_oeffnen_aufruf_nutzt_den_ankunftsanker():
    import services.trips_service as T
    baum = ast.parse(inspect.getsource(T))
    # (a) jeder Aufruf von _open_event/_open_unknown gibt den geschlossenen Halt mit
    aufrufe = [n for n in ast.walk(baum) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id in ('_open_event', '_open_unknown')]
    assert len(aufrufe) >= 7, len(aufrufe)
    for n in aufrufe:
        assert any(k.arg == 'vorher' for k in n.keywords), ast.unparse(n)
    # (b) beide Funktionen datieren die Ankunft ausschliesslich ueber _ankunft_anker
    for fn in (n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
               and n.name in ('_open_event', '_open_unknown')):
        arr = [k.value for c in ast.walk(fn) if isinstance(c, ast.Call)
               and getattr(c.func, 'id', '') == 'ParkingEvent' for k in c.keywords if k.arg == 'arrived_at']
        assert len(arr) == 1 and isinstance(arr[0], ast.Call) and arr[0].func.id == '_ankunft_anker', fn.name
    # (c) ParkingEvent(...) wird nirgends sonst im Modul mit sync.timestamp als Ankunft angelegt
    for c in ast.walk(baum):
        if isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'ParkingEvent':
            for k in c.keywords:
                if k.arg == 'arrived_at':
                    assert 'sync.timestamp' not in ast.unparse(k.value)
