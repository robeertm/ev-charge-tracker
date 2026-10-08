# -*- coding: utf-8 -*-
"""A car whose cloud never sends a GPS timestamp must still get a trip log.

v3.0.147 settled the principle: a MISSING ``location_last_updated_at`` is the
cache-echo fingerprint only for a cloud that normally sends one. For a
connector that never sends one at all, the coordinate is all there will ever
be, and reading it as "stale" costs every destination while buying no safety.

That fix reached ONE of the two freshness tests in
``update_parking_from_sync``. The same-place branch kept its own, inline copy
that still demanded a timestamp outright. Measured on a live install whose
cloud sends none: 0 of 746 syncs carried a timestamp, and 187 of 194 parking
events therefore never had ``last_seen_at`` advanced once — against 373 of 624,
182 of 283 and 158 of 326 on three installs whose clouds do send one.

Two consequences, both visible in that install's raw data:

  * The odometer rescue closes a stay at ``last_seen_at or arrived_at``. With
    ``last_seen_at`` frozen at the arrival, a stay collapses to ZERO duration —
    one such event existed, the only one in 194. The trip log then showed a
    drive arriving and departing in the same second, and the day's real drive
    was gone.
  * A placeholder was stamped with a coordinate that arrived 3 h 16 min and
    36 km later, so the arrival was backdated by that much and the drive in
    between vanished. Both existing echo guards are structurally blind here:
    one compares coordinates, the other reads the timestamps this car never
    sends.

Figures are invented; only the shape of the failure is real.

Run with:  python3 -m pytest tests/test_auto_ohne_gps_zeitstempel.py
"""
import ast
import inspect
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evoz-'))

from flask import Flask                                              # noqa: E402

# Der Tag, an dem die Kette im Buch zerfiel — auf die Stunde nachgebaut.
AN_WORK = datetime(2026, 10, 8, 8, 11, 22)
NOCH_WORK = datetime(2026, 10, 8, 12, 11, 26)
OHNE_GPS = datetime(2026, 10, 8, 16, 39, 13)
WIEDER_GPS = datetime(2026, 10, 8, 19, 55, 43)

ORT_A = (51.1242, 13.7204)     # der Halt, an dem das Auto wirklich stand
ORT_B = (51.0714, 13.5254)     # der Ort, der viel spaeter gemeldet wurde


def _m():
    """Die Modelle holen, die DER GEPRUEFTE CODE benutzt — zur Laufzeit.

    🔴 ``tests/test_sync_audit.py`` leert in seiner Fixture ``sys.modules`` von
    allem mit Praefix ``app``/``config``/``models``. Ein Testmodul, das seine
    Modelle oben importiert, haelt danach das alte ``db``, waehrend der
    Produktionscode das neue benutzt. Darum hier derselbe Weg wie in
    ``test_fahrtenbuch_laeuft_nicht_fest.py``.
    """
    import services.trips_service as T
    return sys.modules[T.ParkingEvent.__module__]


def _trips_service_neu_binden():
    sys.modules.pop('services.trips_service', None)


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
    return app, ctx


def _auto(marke='skoda_api'):
    M = _m()
    v = M.Vehicle(name='Probewagen', api_brand=marke)
    M.db.session.add(v)
    M.db.session.commit()
    return v


def _halt(vid, ort, odo, wann, etikett='other'):
    """Ein offener, beschrifteter Halt — wie ``_open_event`` ihn anlegt."""
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=wann, last_seen_at=wann,
                        departed_at=None, lat=ort[0], lon=ort[1],
                        label=etikett, odometer_arrived=odo,
                        odometer_departed=odo, soc_arrived=50,
                        soc_departed=50)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _platzhalter(vid, odo, wann):
    """Ein offener Platzhalter — wie ``_open_unknown`` ihn anlegt (Sentinel 0,0)."""
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=wann, last_seen_at=wann,
                        departed_at=None, lat=0.0, lon=0.0, label='unknown',
                        odometer_arrived=odo, odometer_departed=odo,
                        soc_arrived=50, soc_departed=50)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _sync(vid, odo, wann, ort=None, gps_alter_min=None, soc=50):
    """Ein Sync. ``ort=None`` heisst: die Wolke lieferte GAR KEINE Koordinate.
    ``gps_alter_min=None`` heisst: sie lieferte keinen GPS-Zeitstempel."""
    M = _m()
    gps_ts = None if gps_alter_min is None else wann - timedelta(minutes=gps_alter_min)
    s = M.VehicleSync(vehicle_id=vid, timestamp=wann, odometer_km=odo,
                      soc_percent=soc,
                      location_lat=None if ort is None else ort[0],
                      location_lon=None if ort is None else ort[1],
                      location_last_updated_at=gps_ts)
    M.db.session.add(s)
    M.db.session.commit()
    return s


# ── 1. Der Halt muss mitwachsen, solange das Auto dort steht ──────────

def test_01_gleicher_ort_schreibt_last_seen_fort_ohne_gps_zeitstempel():
    """🔑 Der Kern. Ein Auto, dessen Wolke NIE einen GPS-Zeitstempel schickt,
    steht um 12:11 nachweislich noch am selben Ort wie um 08:11 — gleiche
    Koordinate, gleicher Kilometerstand. ``last_seen_at`` muss mitgehen.

    Vor der Behebung kehrte der Zweig vorher zurueck, weil er einen
    Zeitstempel verlangte, den dieses Auto nie liefert.
    """
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto()
        pe = _halt(v.id, ORT_A, odo=34606, wann=AN_WORK)
        s = _sync(v.id, odo=34606, wann=NOCH_WORK, ort=ORT_A)
        update_parking_from_sync(s)
        frisch = _m().ParkingEvent.query.get(pe.id)
        assert frisch.last_seen_at == NOCH_WORK, (
            'last_seen_at blieb bei %s stehen' % frisch.last_seen_at)
    finally:
        ctx.pop()


def test_02_wer_sonst_zeitstempel_schickt_bleibt_streng():
    """🔑 Die Gegenprobe, und der Grund, warum die Sperre nicht einfach weg
    darf. Dieses Auto liefert normalerweise einen Zeitstempel. Ein Sync OHNE
    ist dann genau die Echo-Form, fuer die die Sperre gebaut wurde: die Wolke
    serviert die letzte bekannte Koordinate erneut, waehrend das Auto schon
    faehrt. ``last_seen_at`` darf NICHT mitgehen."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto(marke='hyundai')
        # Die Historie, die dieses Auto von Mikes unterscheidet:
        _sync(v.id, odo=34600, wann=AN_WORK - timedelta(hours=4),
              ort=ORT_A, gps_alter_min=2)
        pe = _halt(v.id, ORT_A, odo=34606, wann=AN_WORK)
        s = _sync(v.id, odo=34606, wann=NOCH_WORK, ort=ORT_A)   # kein Zeitstempel
        update_parking_from_sync(s)
        frisch = _m().ParkingEvent.query.get(pe.id)
        assert frisch.last_seen_at == AN_WORK, (
            'das Echo hat last_seen_at auf %s gezogen' % frisch.last_seen_at)
    finally:
        ctx.pop()


def test_03_ein_veralteter_zeitstempel_bleibt_veraltet():
    """Wer einen Zeitstempel schickt und der ist zu alt, bleibt abgewiesen —
    unabhaengig von der Frage aus Probe 01."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto(marke='hyundai')
        pe = _halt(v.id, ORT_A, odo=34606, wann=AN_WORK)
        s = _sync(v.id, odo=34606, wann=NOCH_WORK, ort=ORT_A,
                  gps_alter_min=90)
        update_parking_from_sync(s)
        assert _m().ParkingEvent.query.get(pe.id).last_seen_at == AN_WORK
    finally:
        ctx.pop()


# ── 2. Kein Halt mit Dauer null ───────────────────────────────────────

def test_04_halt_zerfaellt_nicht_zu_dauer_null():
    """🔴 Der gemeldete Fehler, Form 1: „keine Fahrten erkannt".

    Die ganze Kette eines Tages. Um 16:39 kommt ein Sync OHNE Koordinate,
    aber mit gestiegenem Kilometerstand — die Odometer-Rettung schliesst den
    Halt bei ``last_seen_at or arrived_at``. Stand ``last_seen_at`` nie
    fort, ist die Abfahrt gleich der Ankunft: ein Halt von null Sekunden,
    und im Buch eine Fahrt, die in derselben Sekunde ankommt und abfaehrt.
    """
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto()
        pe = _halt(v.id, ORT_A, odo=34606, wann=AN_WORK)
        update_parking_from_sync(_sync(v.id, 34606, NOCH_WORK, ort=ORT_A))
        update_parking_from_sync(_sync(v.id, 34629, OHNE_GPS, ort=None))
        frisch = _m().ParkingEvent.query.get(pe.id)
        assert frisch.departed_at is not None, 'haette geschlossen werden muessen'
        assert frisch.departed_at > frisch.arrived_at, (
            'Halt mit Dauer null: an=%s ab=%s' % (frisch.arrived_at,
                                                  frisch.departed_at))
        assert frisch.departed_at == NOCH_WORK, (
            'Abfahrt gehoert auf die letzte Bestaetigung am Ort, nicht auf %s'
            % frisch.departed_at)
    finally:
        ctx.pop()


# ── 3. Ein Platzhalter wird nur beschriftet, wenn das Auto STAND ──────

def test_05_platzhalter_wird_nicht_beschriftet_wenn_weitergefahren():
    """🔴 Der gemeldete Fehler, Form 2: „Fahrten sind vertauscht".

    Der Platzhalter wurde um 16:39 bei 34629 km angelegt. Die naechste
    Koordinate kommt 3 h 16 min spaeter — und 36 km weiter. Das Auto stand
    in dieser Zeit nicht; es fuhr. Wird die Koordinate trotzdem auf den
    Platzhalter gestempelt, ist die Ankunft um 3 h zurueckdatiert und die
    Fahrt dazwischen verschwindet.

    🔑 Beide vorhandenen Waechter sind hier blind: der eine vergleicht
    Koordinaten (die unterscheiden sich ja), der andere liest GPS-Zeitstempel
    (die dieses Auto nie schickt). Der Kilometerstand sagt es trotzdem.
    """
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        M = _m()
        v = _auto()
        pe = _platzhalter(v.id, odo=34629, wann=OHNE_GPS)
        update_parking_from_sync(_sync(v.id, 34665, WIEDER_GPS, ort=ORT_B))

        alt = M.ParkingEvent.query.get(pe.id)
        assert alt.departed_at is not None, (
            'der Platzhalter blieb offen, obwohl 36 km Beweis vorliegen')
        neu = (M.ParkingEvent.query
               .filter(M.ParkingEvent.id != pe.id)
               .order_by(M.ParkingEvent.arrived_at.desc()).first())
        assert neu is not None, 'es wurde kein neuer Halt angelegt'
        assert neu.arrived_at == WIEDER_GPS, (
            'die Ankunft gehoert auf 19:55, nicht auf %s' % neu.arrived_at)
        assert neu.odometer_arrived == 34665
        assert abs(neu.lat - ORT_B[0]) < 1e-6 and abs(neu.lon - ORT_B[1]) < 1e-6
        assert neu.label != 'unknown', (
            'die Koordinate war frisch und unverdaechtig — sie darf benannt werden')
    finally:
        ctx.pop()


def test_06_platzhalter_wird_beschriftet_wenn_das_auto_stand():
    """🔑 Die Gegenprobe. Gleicher Kilometerstand heisst: das Auto stand
    wirklich dort. Dann ist das Nachbeschriften genau richtig, und die
    Ankunftszeit bleibt beim Odometer-Anker — kein neuer Halt."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        M = _m()
        v = _auto()
        pe = _platzhalter(v.id, odo=34629, wann=OHNE_GPS)
        update_parking_from_sync(_sync(v.id, 34629, WIEDER_GPS, ort=ORT_B))

        frisch = M.ParkingEvent.query.get(pe.id)
        assert frisch.departed_at is None, 'haette offen bleiben muessen'
        assert frisch.arrived_at == OHNE_GPS
        assert frisch.label != 'unknown', 'haette beschriftet werden muessen'
        assert M.ParkingEvent.query.count() == 1, 'es kam ein Halt zuviel dazu'
    finally:
        ctx.pop()


def test_07_ein_kilometer_genuegt_noch_nicht_als_beweis():
    """Die Grenze ist dieselbe wie ueberall sonst im Modul: ab 1 km. Darunter
    ist es Messrauschen des Tachos, kein Umzug."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        M = _m()
        v = _auto()
        pe = _platzhalter(v.id, odo=34629, wann=OHNE_GPS)
        update_parking_from_sync(_sync(v.id, 34629, WIEDER_GPS, ort=ORT_B))
        assert M.ParkingEvent.query.get(pe.id).departed_at is None
        assert M.ParkingEvent.query.count() == 1
    finally:
        ctx.pop()


# ── 4. Die Eigenschaft, nicht die Zahl ────────────────────────────────

def test_08_es_gibt_nur_EINE_frischepruefung():
    """🔴 Die Eigenschaft, an der der Fehler hing: in
    ``update_parking_from_sync`` darf es keine ZWEITE, eigene Frischepruefung
    neben ``_is_fresh_gps`` geben.

    Genau daran scheiterte v3.0.147: die Behebung ging in die eine Pruefung,
    die zweite — als lokale Variable im Gleich-Ort-Zweig — blieb streng und
    machte die Behebung fuer dieses Auto wirkungslos. Gemessen am Syntaxbaum,
    nicht am Text: eine Zahl waere ein Schnappschuss, die Eigenschaft nicht.
    """
    import services.trips_service as T
    quelle = inspect.getsource(T.update_parking_from_sync)
    baum = ast.parse(quelle.lstrip())
    fn = baum.body[0]

    # a) Keine lokale Variable, die eine Frische festhaelt.
    namen = set()
    for knoten in ast.walk(fn):
        if isinstance(knoten, ast.Assign):
            for ziel in knoten.targets:
                if isinstance(ziel, ast.Name):
                    namen.add(ziel.id)
    assert 'gps_fresh' not in namen, (
        'eine zweite Frischepruefung ist wieder da: gps_fresh')

    # b) Jede Stelle, die ueber die Frische ENTSCHEIDET, fragt die eine
    #    Funktion. Zugelassen bleibt genau die Veralterungssperre, die einen
    #    VORHANDENEN Zeitstempel prueft und das Alter an ``age_min`` bindet.
    #
    #    🔴 Ausgenommen sind die Rumpfe der Helfer, die die Frische DEFINIEREN
    #    — ``_is_fresh_gps`` muss den Zeitstempel ja lesen duerfen. Ohne diese
    #    Ausnahme meldete der Waechter die eine erlaubte Stelle als Verstoss.
    #    Geprueft wird also: entscheidet AUSSERHALB der Helfer noch jemand
    #    selbst ueber Frische.
    drin = set()
    for knoten in ast.walk(fn):
        if (isinstance(knoten, (ast.FunctionDef, ast.AsyncFunctionDef))
                and knoten is not fn
                and knoten.name in ('_is_fresh_gps',
                                    '_car_ever_reports_gps_time')):
            for k in ast.walk(knoten):
                drin.add(id(k))
    verdaechtig = []
    for knoten in ast.walk(fn):
        if not isinstance(knoten, ast.If):
            continue
        if id(knoten) in drin:
            continue
        text = ast.dump(knoten.test)
        if 'location_last_updated_at' not in text:
            continue
        rumpf = ast.dump(ast.Module(body=knoten.body, type_ignores=[]))
        if 'age_min' in rumpf or 'age_min' in text:
            continue        # die bekannte, gewollte Veralterungssperre
        verdaechtig.append(ast.unparse(knoten.test))
    assert not verdaechtig, (
        'Frische wird an %d Stelle(n) ausserhalb von _is_fresh_gps '
        'entschieden: %s' % (len(verdaechtig), verdaechtig))


# ── 5. Die Nachtraege: was der Fehler im Buch hinterlassen hat ────────

def test_09_halt_mit_dauer_null_wird_am_beweis_aufgezogen():
    """Der Sync um 12:11 zeigt dasselbe Auto an derselben Koordinate mit
    demselben Kilometerstand. Also stand es dort — die Abfahrt gehoert dahin."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_halte_ohne_dauer
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=AN_WORK,
                            last_seen_at=AN_WORK, departed_at=AN_WORK,
                            lat=ORT_A[0], lon=ORT_A[1], label='other',
                            odometer_arrived=34606, odometer_departed=34606)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34606, wann=NOCH_WORK, ort=ORT_A)
        assert repariere_halte_ohne_dauer() == 1
        assert M.ParkingEvent.query.get(pe.id).departed_at == NOCH_WORK
    finally:
        ctx.pop()


def test_10_ohne_beweis_bleibt_der_halt_wie_er_ist():
    """🔑 Die Gegenprobe. Ein Halt von null Sekunden kann auch die Wahrheit
    sein. Zeigt kein Sync das Auto noch dort, wird nichts angefasst — hier
    liegt der spaetere Sync 23 km weiter."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_halte_ohne_dauer
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=AN_WORK,
                            last_seen_at=AN_WORK, departed_at=AN_WORK,
                            lat=ORT_A[0], lon=ORT_A[1], label='other',
                            odometer_arrived=34606, odometer_departed=34606)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34629, wann=NOCH_WORK, ort=ORT_B)
        assert repariere_halte_ohne_dauer() == 0
        assert M.ParkingEvent.query.get(pe.id).departed_at == AN_WORK
    finally:
        ctx.pop()


def test_11_ein_beweis_nach_dem_naechsten_halt_zaehlt_nicht():
    """Der Beweis muss VOR dem naechsten Halt liegen. Sonst zieht ein spaeterer
    Besuch am selben Ort eine alte Abfahrt ueber einen dazwischenliegenden
    Halt hinweg."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_halte_ohne_dauer
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=AN_WORK,
                            last_seen_at=AN_WORK, departed_at=AN_WORK,
                            lat=ORT_A[0], lon=ORT_A[1], label='other',
                            odometer_arrived=34606, odometer_departed=34606)
        spaeter = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                                 last_seen_at=OHNE_GPS, departed_at=None,
                                 lat=ORT_B[0], lon=ORT_B[1], label='other',
                                 odometer_arrived=34629,
                                 odometer_departed=34629)
        M.db.session.add_all([pe, spaeter])
        M.db.session.commit()
        # Beweis am alten Ort, aber ERST NACH der Ankunft des naechsten Halts:
        _sync(v.id, odo=34606, wann=WIEDER_GPS, ort=ORT_A)
        assert repariere_halte_ohne_dauer() == 0
        assert M.ParkingEvent.query.get(pe.id).departed_at == AN_WORK
    finally:
        ctx.pop()


def test_12_zurueckdatierte_ankunft_des_laufenden_halts_wird_gerichtet():
    """Der Ort kam 3 h und 36 km spaeter. Danach gehoert der benannte Halt auf
    19:55 — und der Abschnitt davor war nie ein Ort, sondern nur ein
    Kilometerstand."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                            last_seen_at=WIEDER_GPS, departed_at=None,
                            lat=ORT_B[0], lon=ORT_B[1], label='home',
                            odometer_arrived=34629, odometer_departed=34629)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34665, wann=WIEDER_GPS, ort=ORT_B, soc=23)
        assert repariere_zurueckdatierte_ankunft() == 1

        alt = M.ParkingEvent.query.get(pe.id)
        assert alt.departed_at is not None, 'der falsche Abschnitt blieb offen'
        assert alt.label == 'unknown' and alt.lat == 0.0, (
            'der Abschnitt behielt einen Ort, den er nie hatte')
        neu = (M.ParkingEvent.query.filter(M.ParkingEvent.id != pe.id)
               .order_by(M.ParkingEvent.arrived_at.desc()).first())
        assert neu is not None and neu.departed_at is None
        assert neu.arrived_at == WIEDER_GPS
        assert neu.odometer_arrived == 34665
        assert neu.label == 'home'
    finally:
        ctx.pop()


def test_13_stand_das_auto_wirklich_dort_wird_nichts_gerichtet():
    """🔑 Gegenprobe: gleicher Kilometerstand heisst, das Auto stand dort.
    Dann ist die frueher gesetzte Ankunft richtig und bleibt."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                            last_seen_at=WIEDER_GPS, departed_at=None,
                            lat=ORT_B[0], lon=ORT_B[1], label='home',
                            odometer_arrived=34629, odometer_departed=34629)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34629, wann=WIEDER_GPS, ort=ORT_B)
        assert repariere_zurueckdatierte_ankunft() == 0
        assert M.ParkingEvent.query.get(pe.id).arrived_at == OHNE_GPS
        assert M.ParkingEvent.query.count() == 1
    finally:
        ctx.pop()


def test_14_ein_geschlossener_halt_wird_nicht_umgeschrieben():
    """🔴 Die Grenze, und zwar als EIGENSCHAFT, nicht als gewaehlte Zahl.

    Der Trockenlauf fand dieselbe Form auf zwei GESCHLOSSENEN Halten einer
    anderen Installation — mit einem einzigen Kilometer Unterschied, also
    genau im Rauschband, das dieses Modul anderswo selbst als "keine
    Bewegung" liest. Die Endpunkte eines geschlossenen Halts sind ausserdem
    schon am SDK-Fahrtenabgleich ausgerichtet. Sie anzufassen waere geraten.
    """
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                            last_seen_at=WIEDER_GPS,
                            departed_at=WIEDER_GPS + timedelta(hours=2),
                            lat=ORT_B[0], lon=ORT_B[1], label='home',
                            odometer_arrived=34629, odometer_departed=34629)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34665, wann=WIEDER_GPS, ort=ORT_B)
        assert repariere_zurueckdatierte_ankunft() == 0
        assert M.ParkingEvent.query.get(pe.id).arrived_at == OHNE_GPS
        assert M.ParkingEvent.query.count() == 1
    finally:
        ctx.pop()


def test_15_ein_platzhalter_ohne_ort_wird_nicht_gerichtet():
    """Ein Sentinel-Platzhalter hat keinen Ort, gegen den man pruefen koennte.
    Fuer den ist ``release_stuck_parking_events`` zustaendig, nicht dieser
    Nachtrag."""
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = _platzhalter(v.id, odo=34629, wann=OHNE_GPS)
        _sync(v.id, odo=34665, wann=WIEDER_GPS, ort=ORT_B)
        assert repariere_zurueckdatierte_ankunft() == 0
        assert M.ParkingEvent.query.get(pe.id).departed_at is None
    finally:
        ctx.pop()


# ── 6. Die Nachtraege haengen an EIGENEN Marken ───────────────────────

def test_16_jeder_nachtrag_hat_seine_eigene_neue_marke():
    """🔑 Derselbe Stolperstein wie bei v3.0.152: der Aufruf haengt an einer
    einmaligen Marke. Eine Regel unter einer Marke, die schon auf 'done' steht,
    kommt nie wieder vorbei — die ganze Aenderung waere ein Nulldurchgang,
    ohne dass irgendetwas rot wird. Am CODE verankert, nicht an einem
    Zeichenfenster: von der Wachzeile bis zum naechsten ``if AppConfig.get(``.
    """
    import app as appmod
    q = inspect.getsource(appmod)
    for marke, regel in (
        ('v3_0_153_halt_ohne_dauer', 'repariere_halte_ohne_dauer'),
        ('v3_0_153_zurueckdatierte_ankunft',
         'repariere_zurueckdatierte_ankunft'),
    ):
        wache = "if AppConfig.get('%s') != 'done':" % marke
        assert wache in q, 'der Nachtrag %s haengt an keiner eigenen Marke' % regel
        rest = q[q.index(wache) + len(wache):]
        ende = rest.find('if AppConfig.get(')
        block = rest if ende < 0 else rest[:ende]
        assert regel in block, \
            'unter %s wird %s gar nicht gerufen' % (marke, regel)
        assert "AppConfig.set('%s', 'done')" % marke in block, \
            'die Marke %s wird nicht gesetzt — der Nachtrag liefe bei jedem Start' % marke
    # Und die beiden Marken sind wirklich verschieden.
    assert q.count("AppConfig.set('v3_0_153_halt_ohne_dauer', 'done')") == 1
    assert q.count(
        "AppConfig.set('v3_0_153_zurueckdatierte_ankunft', 'done')") == 1


# ── 7. Die Luecken, die der Mutationstest gefunden hat ────────────────

def test_17_gleicher_kilometerstand_an_ANDEREM_ort_ist_kein_beweis():
    """🔴 Gefunden, weil ein Mutationstest die Ortspruefung entfernte und
    KEINE Probe rot wurde. Probe 10 variiert Ort UND Kilometerstand und misst
    damit nur den Kilometerstand — die Ortspruefung war ungedeckt.

    Der Fall ist echt: der Kilometerstand kommt in ganzen Kilometern. Ein Auto
    kann 800 m weiter stehen und dieselbe Zahl melden. Dann ist der Ort das
    einzige, was den Beweis traegt.
    """
    app, ctx = _app()
    try:
        from services.trips_service import repariere_halte_ohne_dauer
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=AN_WORK,
                            last_seen_at=AN_WORK, departed_at=AN_WORK,
                            lat=ORT_A[0], lon=ORT_A[1], label='other',
                            odometer_arrived=34606, odometer_departed=34606)
        M.db.session.add(pe)
        M.db.session.commit()
        # Gleicher Kilometerstand, aber ein ganz anderer Ort:
        _sync(v.id, odo=34606, wann=NOCH_WORK, ort=ORT_B)
        assert repariere_halte_ohne_dauer() == 0
        assert M.ParkingEvent.query.get(pe.id).departed_at == AN_WORK
    finally:
        ctx.pop()


def test_18_ein_halt_auf_dem_sentinel_wird_nicht_gerichtet():
    """🔴 Zweite Luecke desselben Mutationstests: die Sentinel-Ausnahme in
    Nachtrag 2 war ungedeckt, weil Probe 15 schon an der Etiketten-Pruefung
    haengenblieb.

    Die Form, fuer die die Zeile da ist: ein Halt mit einem ANDEREN Etikett,
    dessen Koordinate aber noch der Sentinel 0,0 ist. Ohne die Ausnahme wuerde
    gegen 0,0 verglichen — und ein Sync in der Naehe dieses Nullpunkts gaelte
    als "derselbe Ort".
    """
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                            last_seen_at=WIEDER_GPS, departed_at=None,
                            lat=0.0, lon=0.0, label='other',
                            odometer_arrived=34629, odometer_departed=34629)
        M.db.session.add(pe)
        M.db.session.commit()
        # ~78 m vom Sentinel entfernt, also innerhalb SAME_PLACE_M von 0,0:
        _sync(v.id, odo=34665, wann=WIEDER_GPS, ort=(0.0005, 0.0005))
        assert repariere_zurueckdatierte_ankunft() == 0
        frisch = M.ParkingEvent.query.get(pe.id)
        assert frisch.departed_at is None
        assert M.ParkingEvent.query.count() == 1
    finally:
        ctx.pop()


def test_19_am_selben_ort_mit_HOEHEREM_kilometerstand_ist_kein_beweis():
    """🔴 Dritte Luecke desselben Mutationstests: der Kilometer-Filter in
    Nachtrag 1 war ungedeckt.

    Zeigt ein Sync das Auto am selben Ort, aber mit hoeherem Kilometerstand,
    dann ist es zwischendurch weggefahren und zurueckgekommen. Die Abfahrt des
    alten Halts liegt dann FRUEHER als dieser Sync — er darf sie nicht
    tragen. Ohne Beweis bleibt der Halt, wie er ist.
    """
    app, ctx = _app()
    try:
        from services.trips_service import repariere_halte_ohne_dauer
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=AN_WORK,
                            last_seen_at=AN_WORK, departed_at=AN_WORK,
                            lat=ORT_A[0], lon=ORT_A[1], label='other',
                            odometer_arrived=34606, odometer_departed=34606)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34629, wann=NOCH_WORK, ort=ORT_A)   # 23 km dazwischen
        assert repariere_halte_ohne_dauer() == 0
        assert M.ParkingEvent.query.get(pe.id).departed_at == AN_WORK
    finally:
        ctx.pop()


def test_20_ein_halt_ohne_etikett_wird_nicht_gerichtet():
    """Deckt die erreichbare Haelfte der Etiketten-Ausnahme in Nachtrag 2:
    ``label is None``, wie es aus einem Import stammen kann.

    🔑 Offen gesagt: die andere Haelfte — ``label == 'unknown'`` MIT echter
    Koordinate — ist durch keine Probe gedeckt, weil der Code sie nicht
    erzeugen kann: ``_open_unknown`` legt Platzhalter immer auf dem Sentinel
    0,0 an, und den faengt schon Probe 18. Die Bedingung bleibt trotzdem
    stehen, als zweiter Boden, nicht als gemessene Zusicherung.
    """
    app, ctx = _app()
    try:
        from services.trips_service import repariere_zurueckdatierte_ankunft
        M = _m()
        v = _auto()
        pe = M.ParkingEvent(vehicle_id=v.id, arrived_at=OHNE_GPS,
                            last_seen_at=WIEDER_GPS, departed_at=None,
                            lat=ORT_B[0], lon=ORT_B[1], label=None,
                            odometer_arrived=34629, odometer_departed=34629)
        M.db.session.add(pe)
        M.db.session.commit()
        _sync(v.id, odo=34665, wann=WIEDER_GPS, ort=ORT_B)
        assert repariere_zurueckdatierte_ankunft() == 0
        assert M.ParkingEvent.query.count() == 1
    finally:
        ctx.pop()


# ── 8. Auch OHNE Bestaetigung am Ort kein Halt von null Sekunden ──────

def test_21_ohne_zwischensync_endet_der_halt_am_beweisenden_sync():
    """🔴 Die Luecke in der ersten Fassung dieser Behebung, gefunden am
    Nachbau der Sequenz vom Vortag.

    Dort gab es zwischen Ankunft und dem Sync ohne Koordinate KEINEN Sync am
    Ort. ``last_seen_at`` stand also weiter auf der Ankunft, und der Halt fiel
    trotz der ersten Behebung auf Dauer null zusammen.

    Das Auto wurde um 07:15 dort gesehen und um 15:15 woanders — es stand also
    eine positive Zeit da. Fehlt die Bestaetigung am Ort, ist der beweisende
    Sync der einzige belegte Anker.
    """
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto()
        pe = _halt(v.id, ORT_A, odo=34549, wann=AN_WORK)
        update_parking_from_sync(_sync(v.id, 34557, OHNE_GPS, ort=None))
        frisch = _m().ParkingEvent.query.get(pe.id)
        assert frisch.departed_at == OHNE_GPS, (
            'Abfahrt steht auf %s' % frisch.departed_at)
        assert frisch.departed_at > frisch.arrived_at, 'Halt mit Dauer null'
    finally:
        ctx.pop()


def test_22_mit_bestaetigung_am_ort_gewinnt_die_bestaetigung():
    """🔑 Die Gegenprobe zu Probe 21: gibt es einen Sync, der das Auto noch am
    Ort zeigt, zaehlt DER — nicht der spaetere, der nur die Bewegung beweist.
    Sonst verschluckt der Halt die ganze Fahrt."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto()
        pe = _halt(v.id, ORT_A, odo=34606, wann=AN_WORK)
        update_parking_from_sync(_sync(v.id, 34606, NOCH_WORK, ort=ORT_A))
        update_parking_from_sync(_sync(v.id, 34629, OHNE_GPS, ort=None))
        frisch = _m().ParkingEvent.query.get(pe.id)
        assert frisch.departed_at == NOCH_WORK, (
            'die Bestaetigung am Ort wurde uebergangen: %s' % frisch.departed_at)
    finally:
        ctx.pop()
