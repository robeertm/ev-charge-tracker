# -*- coding: utf-8 -*-
"""„An dieser Wallbox lädt nur dieses Auto" — der dritte Beweisweg.

Die Wallbox misst Energie, sie sieht keine Autos. Bis v3.0.147 musste das
FAHRZEUG bestätigen, dass es die Energie genommen hat: entweder meldet es
„ich lade" innerhalb des Fensters, oder es stand nachweislich zu Hause und
wurde dabei voller.

Beides setzt voraus, dass das Auto im richtigen Moment gefragt wurde. Wird die
Wolke alle vier Stunden abgefragt und dauert eine Ladung eine halbe Stunde,
fällt dieser Moment fast nie ins Fenster. Auf einer echten Installation traf
bei ZWEI Ladungen kein einziger Sync ins Fenster — beide wurden abgelehnt,
obwohl der Zähler sie auf die Wattstunde hatte.

Weg C beantwortet darum eine andere Frage, und zwar die einzige Stelle, die
sie beantworten kann: den Besitzer. Steht sein Haken, reicht die Messung —
solange nichts im Fenster dagegenspricht.

Jede Regel wird von BEIDEN Seiten geprüft: sie greift, wenn sie soll, und sie
schweigt, wenn sie nicht soll.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evexcl-'))

from flask import Flask                                             # noqa: E402
from services import shelly_link as L                               # noqa: E402


def _m():
    """Die Modelle ZUR LAUFZEIT holen, nicht oben importieren.

    🔴 ``tests/test_sync_audit.py`` leert in seiner Fixture ``sys.modules`` von
    allem mit Praefix ``app``/``config``/``models`` und importiert neu. Danach
    gibt es ein ZWEITES ``db``-Objekt. Ein Testmodul, das seine Modelle oben
    importiert, haelt dann das alte, waehrend ``services.shelly_link`` seine
    Modelle erst IN der Funktion holt und damit das neue benutzt — Flask meldet
    dann "The current Flask app is not registered with this SQLAlchemy
    instance". Einzeln gefahren waren alle acht Proben gruen, im Gesamtlauf
    fielen alle acht. Dieselbe Falle wie in
    ``tests/test_fahrtenbuch_laeuft_nicht_fest.py``.
    """
    import models.database as M
    return M


def _trips_service_neu_binden():
    """``services.trips_service`` an das AKTUELLE Modell-Modul binden.

    ``_steht_zuhause`` holt sich ``_classify_location`` von dort, und jenes
    Modul importiert seine Modelle auf MODULEBENE — es bleibt also an der
    alten Instanz haengen, waehrend alles andere schon umgezogen ist.
    """
    import sys as _s
    _s.modules.pop('services.trips_service', None)

# Erfundene Koordinaten. Echte Standorte gehoeren nicht in ein oeffentliches
# Repo — auch nicht als Pruefdaten.
HEIM = (52.000000, 13.000000)
ARBEIT = (52.100000, 13.200000)          # rund 17 km entfernt
FENSTER_AN = datetime(2026, 10, 6, 16, 15)
FENSTER_AUS = datetime(2026, 10, 6, 17, 9)


def pruefe(name, ist, soll):
    print(("  OK   " if ist == soll else "  FEHL ") + name + "   ist=%r soll=%r" % (ist, soll))
    assert ist == soll, "%s: ist=%r soll=%r" % (name, ist, soll)


def _app():
    _trips_service_neu_binden()
    M = _m()
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    M.db.init_app(app)
    ctx = app.app_context()
    ctx.push()
    M.db.create_all()
    M.AppConfig.set('home_lat', str(HEIM[0]))
    M.AppConfig.set('home_lon', str(HEIM[1]))
    return app, ctx


def _auto(exklusiv=False, batterie=58.0):
    M = _m()
    v = M.Vehicle(name='Probewagen', battery_kwh=batterie,
                  wallbox_link_enabled=True, wallbox_device_key='',
                  wallbox_exclusive=exklusiv)
    M.db.session.add(v)
    M.db.session.commit()
    return v


def _messung(vid=None):
    M = _m()
    wc = M.WallboxCharge(device_key='wallbox', source_id='s1',
                         start_ts=int(FENSTER_AN.timestamp()),
                         end_ts=int(FENSTER_AUS.timestamp()),
                         energy_kwh=3.73, vehicle_id=vid)
    M.db.session.add(wc)
    M.db.session.commit()
    return wc


def _sync(v, wann, soc, odo, ort=HEIM, laedt=False):
    M = _m()
    s = M.VehicleSync(vehicle_id=v.id, timestamp=wann, soc_percent=soc,
                      odometer_km=odo, is_charging=laedt,
                      location_lat=(ort[0] if ort else None),
                      location_lon=(ort[1] if ort else None))
    M.db.session.add(s)
    M.db.session.commit()
    return s


# ── Der Haken ist aus: alles bleibt wie vorher ────────────────────────────

def test_01_ohne_haken_bleibt_eine_unbelegte_messung_unbelegt():
    """Vorgabe ist AUS — für jede bestehende Installation ändert sich nichts."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=False)
        wc = _messung()
        # Der echte Fall: gefahren und danach geladen, Ladestand unverändert.
        _sync(v, FENSTER_AN - timedelta(minutes=53), 70, 34506, ort=None)
        _sync(v, FENSTER_AUS + timedelta(minutes=6), 70, 34525, ort=HEIM)
        pruefe('ohne Haken kein Beleg',
               L.beleg_fuer_dieses_auto(wc, v, 90), None)
    finally:
        ctx.pop()


# ── Der Haken ist an: die zwei echten Fälle ───────────────────────────────

def test_02_mit_haken_wird_die_gefahren_und_geladen_messung_belegt():
    """06.10.: 19 km heim, dann 3,73 kWh — Ladestand 70 % vorher wie nachher.

    Die Fahrt und die Ladung heben sich fast genau auf. Weg B scheitert
    dreifach: kein GPS im Sync davor, Kilometerstand verändert, Ladestand
    nicht gestiegen.
    """
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True)
        wc = _messung()
        _sync(v, FENSTER_AN - timedelta(minutes=53), 70, 34506, ort=None)
        _sync(v, FENSTER_AUS + timedelta(minutes=6), 70, 34525, ort=HEIM)
        beleg = L.beleg_fuer_dieses_auto(wc, v, 90)
        pruefe('mit Haken belegt', bool(beleg), True)
        pruefe('Begründung nennt den Besitzer', 'owner' in (beleg or ''), True)
    finally:
        ctx.pop()


def test_03_mit_haken_wird_auch_das_nachladen_eines_vollen_autos_belegt():
    """05.10.: 36 h voll am Kabel, dann 2,07 kWh Standby-Verluste nachgeladen.

    Ladestand 100 % davor und danach. Jede Regel, die einen STEIGENDEN
    Ladestand verlangt, wirft diese Ladung weg.
    """
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True)
        wc = _messung()
        wc.energy_kwh = 2.07
        _m().db.session.commit()
        _sync(v, FENSTER_AN - timedelta(hours=36), 100, 34407, ort=HEIM)
        _sync(v, FENSTER_AUS + timedelta(minutes=20), 100, 34407, ort=HEIM)
        pruefe('volles Auto, Nachladung belegt',
               bool(L.beleg_fuer_dieses_auto(wc, v, 90)), True)
    finally:
        ctx.pop()


# ── Der Haken hebt die Gegenbeweise nicht auf ─────────────────────────────

def test_04_ein_sync_IM_fenster_der_das_auto_woanders_zeigt_widerlegt():
    """Wer nicht an der Box steht, kann ihr nichts entnehmen — Haken hin oder her."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True)
        wc = _messung()
        _sync(v, FENSTER_AN - timedelta(minutes=30), 70, 34506, ort=HEIM)
        _sync(v, FENSTER_AN + timedelta(minutes=20), 70, 34520, ort=ARBEIT)
        _sync(v, FENSTER_AUS + timedelta(minutes=6), 70, 34525, ort=HEIM)
        pruefe('woanders im Fenster -> kein Beleg',
               L.beleg_fuer_dieses_auto(wc, v, 90), None)
    finally:
        ctx.pop()


def test_05_ein_sync_im_fenster_ZUHAUSE_widerlegt_nicht():
    """Gegenprobe zu 04: zu Hause im Fenster ist kein Gegenbeweis, sondern einer mehr."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True)
        wc = _messung()
        _sync(v, FENSTER_AN + timedelta(minutes=20), 70, 34525, ort=HEIM)
        beleg = L.beleg_fuer_dieses_auto(wc, v, 90)
        pruefe('zuhause im Fenster -> Beleg', bool(beleg), True)
        pruefe('und es steht in der Begründung',
               'at home inside the window' in (beleg or ''), True)
    finally:
        ctx.pop()


# ── Die alten Wege bleiben unverändert ────────────────────────────────────

def test_06_weg_A_wird_vom_haken_nicht_angefasst():
    """Meldet das Auto selbst „ich lade", gilt weiterhin das — ohne Haken."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=False)
        wc = _messung()
        _sync(v, FENSTER_AN + timedelta(minutes=10), 70, 34525, ort=HEIM, laedt=True)
        pruefe('Weg A greift ohne Haken',
               'reported charging' in (L.beleg_fuer_dieses_auto(wc, v, 90) or ''), True)
    finally:
        ctx.pop()


def test_07_weg_B_wird_vom_haken_nicht_angefasst():
    """Stand zu Hause, Kilometerstand gleich, Ladestand gestiegen — wie bisher."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=False)
        wc = _messung()
        _sync(v, FENSTER_AN - timedelta(minutes=10), 60, 34500, ort=HEIM)
        _sync(v, FENSTER_AUS + timedelta(minutes=10), 67, 34500, ort=HEIM)
        pruefe('Weg B greift ohne Haken',
               'stood at home' in (L.beleg_fuer_dieses_auto(wc, v, 90) or ''), True)
    finally:
        ctx.pop()


def test_08_weg_B_schlaegt_weg_C_wenn_beides_zutraefe():
    """Der stärkere Beleg wird genannt, nicht der bequemere."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True)
        wc = _messung()
        _sync(v, FENSTER_AN - timedelta(minutes=10), 60, 34500, ort=HEIM)
        _sync(v, FENSTER_AUS + timedelta(minutes=10), 67, 34500, ort=HEIM)
        pruefe('Weg B hat Vorrang',
               'stood at home' in (L.beleg_fuer_dieses_auto(wc, v, 90) or ''), True)
    finally:
        ctx.pop()
