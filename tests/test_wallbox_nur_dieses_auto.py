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


def _auto(exklusiv=False, batterie=58.0, name='Probewagen', box='',
          gebunden=True):
    M = _m()
    v = M.Vehicle(name=name, battery_kwh=batterie,
                  wallbox_link_enabled=gebunden, wallbox_device_key=box,
                  wallbox_exclusive=exklusiv)
    M.db.session.add(v)
    M.db.session.commit()
    return v


def _messung(vid=None, sid='s1'):
    M = _m()
    wc = M.WallboxCharge(device_key='wallbox', source_id=sid,
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


# ── Weg C tritt zurueck, sobald ein zweites Auto im Haushalt steht ────────
#
# Der Haken sagt einen Satz ueber die BOX: „hier haengt nie ein anderes Auto".
# Bekommt der Haushalt ein zweites Elektroauto, ist der Satz falsch — und der
# Besitzer darf nicht darauf angewiesen sein, daran zu denken, ihn beim ERSTEN
# Auto zurueckzunehmen. Darum wird er bei jedem Durchgang gegen die Flotte
# gehalten, nicht gegen die Erinnerung von damals.

def test_09_ein_zweites_auto_in_der_flotte_setzt_weg_C_aus():
    """Der Fall, der kommt: zweites Elektroauto, gleiche Wallbox."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen')
        _auto(name='Zweitwagen')
        wc = _messung()
        _sync(v, FENSTER_AN - timedelta(minutes=53), 70, 34506, ort=None)
        _sync(v, FENSTER_AUS + timedelta(minutes=6), 70, 34525, ort=HEIM)
        pruefe('zweites Auto -> Weg C schweigt',
               L.beleg_fuer_dieses_auto(wc, v, 90), None)
    finally:
        ctx.pop()


def test_10_ein_zweites_auto_mit_GESETZTEM_haken_aendert_daran_nichts():
    """🔴 Der Haken des anderen Autos ist kein Gegenbeweis, sondern derselbe
    falsche Satz zweimal. Beide muessen schweigen, nicht sich aufheben."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen')
        z = _auto(exklusiv=True, name='Zweitwagen')
        wc = _messung()
        pruefe('Erstwagen schweigt', L.beleg_fuer_dieses_auto(wc, v, 90), None)
        pruefe('Zweitwagen schweigt', L.beleg_fuer_dieses_auto(wc, z, 90), None)
    finally:
        ctx.pop()


def test_11_ein_zweites_auto_OHNE_bindung_zaehlt_trotzdem():
    """🔴 Der gefaehrlichste Fall, und der wahrscheinlichste.

    ``wallbox_link_enabled`` sagt, ob wir fuer dieses Auto Messungen holen —
    nicht, wo es laedt. Wer das zweite Auto eintraegt und den Schalter noch
    nicht umgelegt hat, haette sonst dessen Kilowattstunden beim ersten Auto
    stehen, und niemandem waere es aufgefallen. Ein nie befragtes Auto gilt
    deshalb als Mitbewerber.
    """
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen')
        _auto(name='Zweitwagen', gebunden=False)
        wc = _messung()
        pruefe('ungebundenes zweites Auto -> Weg C schweigt',
               L.beleg_fuer_dieses_auto(wc, v, 90), None)
    finally:
        ctx.pop()


def test_12_ein_zweites_auto_an_EINER_ANDEREN_box_zaehlt_nicht():
    """Gegenprobe: hier hat der Besitzer gesagt, wo das zweite Auto laedt.
    Das ist eine Aussage und kein Schweigen — Weg C bleibt gueltig."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen', box='wallbox')
        _auto(name='Zweitwagen', box='garage-hinten')
        wc = _messung()
        pruefe('andere Box -> Weg C greift weiter',
               bool(L.beleg_fuer_dieses_auto(wc, v, 90)), True)
    finally:
        ctx.pop()


def test_13_ein_zweites_auto_das_im_fenster_WOANDERS_war_zaehlt_nicht():
    """Die zweite Gegenprobe: wer nachweislich nicht da war, ist kein
    Mitbewerber. Gemessen, nicht erklaert."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen')
        z = _auto(name='Zweitwagen')
        wc = _messung()
        _sync(z, FENSTER_AN + timedelta(minutes=10), 55, 8000, ort=ARBEIT)
        pruefe('nachweislich weg -> Weg C greift weiter',
               bool(L.beleg_fuer_dieses_auto(wc, v, 90)), True)
    finally:
        ctx.pop()


def test_14_ein_zweites_auto_das_im_fenster_ZUHAUSE_war_zaehlt_sehr_wohl():
    """Und die Umkehrung davon, damit 13 nicht aus dem falschen Grund gruen
    ist: ein Abruf, der das zweite Auto zu Hause zeigt, ist der Mitbewerber
    in seiner deutlichsten Form."""
    app, ctx = _app()
    try:
        v = _auto(exklusiv=True, name='Erstwagen')
        z = _auto(name='Zweitwagen')
        wc = _messung()
        _sync(z, FENSTER_AN + timedelta(minutes=10), 55, 8000, ort=HEIM)
        pruefe('zweites Auto war hier -> Weg C schweigt',
               L.beleg_fuer_dieses_auto(wc, v, 90), None)
    finally:
        ctx.pop()


# ── Und was an die Stelle von Weg C tritt: der Besitzer sagt es ───────────

def test_15_der_besitzer_kann_die_messung_selbst_einem_auto_zuschreiben():
    """Weg C zieht sich zurueck — dafuer darf gefragt werden.

    Vorher liess sich eine Messung nur an eine BESTEHENDE Ladung haengen, was
    genau in dem Fall nicht hilft, in dem es keine gibt.
    """
    app, ctx = _app()
    try:
        v = _auto(name='Erstwagen')
        _auto(name='Zweitwagen')
        wc = _messung()
        c, grund = L.auf_wunsch_anlegen(wc, v, {'tolerance_min': 90})
        pruefe('Ladung angelegt', c is not None, True)
        pruefe('fuer das genannte Auto', c.vehicle_id, v.id)
        pruefe('mit der gemessenen Energie', c.kwh_loaded, 3.73)
        pruefe('Ladestand bleibt leer', c.soc_from, None)
        pruefe('und der Grund nennt den Besitzer', 'owner' in grund, True)
    finally:
        ctx.pop()


def test_16_auch_der_besitzer_legt_keine_zweite_ladung_fuers_fenster_an():
    """🔑 Sein Wort ersetzt den BEWEIS, nicht die Plausibilitaet. Ein Doppel
    wird von keiner Entscheidung richtig — der Fall, der auf einem laufenden
    System zwei Ladungen von Hand loeschen liess."""
    app, ctx = _app()
    try:
        v = _auto(name='Erstwagen')
        wc = _messung()
        c1, _ = L.auf_wunsch_anlegen(wc, v, {'tolerance_min': 90})
        _m().db.session.commit()
        pruefe('die erste entsteht', c1 is not None, True)
        wc2 = _messung(sid='s2')
        c2, grund = L.auf_wunsch_anlegen(wc2, v, {'tolerance_min': 90})
        pruefe('die zweite nicht', c2, None)
        pruefe('und der Grund sagt warum', 'already filed' in grund, True)
    finally:
        ctx.pop()

