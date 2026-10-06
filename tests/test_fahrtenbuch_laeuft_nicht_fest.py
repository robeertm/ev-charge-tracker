# -*- coding: utf-8 -*-
"""An ``unknown`` placeholder must never be able to freeze the trip log.

Measured on a live Kia install on 2026-10-05/06. A 12 V lockout suppressed
every automatic force-refresh, so the only GPS still arriving was cache echo
with a stale ECU timestamp — correctly dropped by the staleness gate. The
consequence was not obvious: the open ``unknown`` placeholder could only be
upgraded by a sync with *fresh* GPS, the move path needs fresh GPS too, and
the odometer rescue that exists for exactly this shape was gated on
``brand == 'hyundai'`` with the comment "Kia pushes fresh GPS with every
update, so it never hits this data shape". It did. The placeholder stayed open
for 25 h while the odometer advanced 57 km, and the trip log stopped there.

Two things are asserted here:

  * the odometer rescue now also runs for other brands — but only when the
    sync carries no fresh GPS, so the proven Kia path stays untouched, and
  * a stuck placeholder can be released afterwards, with the odometer as the
    only admissible proof that the car has moved on.

Figures are invented; only the shape of the failure is real.

Run with:  python3 -m pytest tests/test_fahrtenbuch_laeuft_nicht_fest.py
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evfb-'))

from flask import Flask                                              # noqa: E402

JETZT = datetime(2026, 10, 6, 18, 0, 0)


def _m():
    """Die Modelle holen, die DER GEPRUEFTE CODE benutzt — zur Laufzeit.

    🔴 ``tests/test_sync_audit.py`` leert in seiner Fixture ``sys.modules``
    von allem mit Praefix ``app``/``config``/``models`` und importiert neu.
    Danach existiert ein ZWEITES ``db``-Objekt. Ein Testmodul, das seine
    Modelle oben importiert, haelt dann das alte, waehrend der
    Produktionscode (der ``from models.database import ...`` erst in der
    Funktion macht) das neue benutzt — und ``db.session`` meldet "current
    Flask app is not registered with this 'SQLAlchemy' instance".
    Einzeln gefahren waren alle Proben gruen, gemeinsam fielen sieben.

    🔴 Und ``import models.database`` genuegt NICHT: ``services.trips_service``
    wird von jener Fixture nicht geleert und haelt sein ``db`` aus Zeile 29
    weiter fest. Der Verlass liegt also beim geprueften Modul selbst — hier
    wird genau dessen ``db``/``ParkingEvent`` benutzt, und die uebrigen
    Modelle aus demselben Modul geholt.
    """
    import sys
    import services.trips_service as T
    return sys.modules[T.ParkingEvent.__module__]


def _trips_service_neu_binden():
    """``services.trips_service`` an das AKTUELLE Modell-Modul binden.

    🔴 ``tests/test_sync_audit.py`` leert in seiner Fixture ``sys.modules`` von
    allem mit Praefix ``app``/``config``/``models``. Danach ist
    ``services.trips_service`` in sich gespalten: seine Modul-Importe (Zeile 29)
    zeigen auf das ALTE ``models.database``, ein ``from models.database import
    VehicleSync`` innerhalb einer Funktion auf das NEUE. Eine Flask-App nimmt
    nur EINE SQLAlchemy-Instanz an, bei beiden anmelden ist also unmoeglich.

    🔑 Geheilt wird genau das gespaltene Modul — und ``models`` wird NICHT
    angefasst. Ein Schnitt auch durch ``models`` hat meine Proben gruen gemacht
    und dafuer ``test_12v_unplausible_werte`` in der umgekehrten Reihenfolge
    gebrochen: ein verschobenes Problem ist kein geloestes.
    """
    import sys
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


def _auto(marke='kia'):
    M = _m()
    v = M.Vehicle(name='Probewagen', api_brand=marke)
    M.db.session.add(v)
    M.db.session.commit()
    return v


def _platzhalter(vid, odo, wann=None, etikett='unknown'):
    """Ein offener Platzhalter, genau wie ``_open_unknown`` ihn anlegt."""
    wann = wann or (JETZT - timedelta(hours=25))
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=wann, last_seen_at=wann,
                      departed_at=None, lat=0.0, lon=0.0, label=etikett,
                      odometer_arrived=odo, odometer_departed=odo)
    M.db.session.add(pe)
    M.db.session.commit()
    return pe


def _sync(vid, odo, wann=None, lat=None, lon=None, gps_alter_min=None):
    wann = wann or JETZT
    gps_ts = None
    if gps_alter_min is not None:
        gps_ts = wann - timedelta(minutes=gps_alter_min)
    M = _m()
    s = M.VehicleSync(vehicle_id=vid, timestamp=wann, odometer_km=odo,
                    soc_percent=50, location_lat=lat, location_lon=lon,
                    location_last_updated_at=gps_ts)
    M.db.session.add(s)
    M.db.session.commit()
    return s


# ── Die Schwelle ──────────────────────────────────────────────────────

def test_schwelle_liegt_bei_60():
    from services.vehicle import sync_service
    assert sync_service.LOW_12V_THRESHOLD_PERCENT == 60


def test_sperre_greift_genau_unter_der_schwelle():
    app, ctx = _app()
    try:
        from services.vehicle.sync_service import is_12v_low
        v = _auto()
        # 61 und 60 sind frei, 59 ist gesperrt — die Grenze ist "kleiner als".
        for wert, erwartet in ((61, False), (60, False), (59, True)):
            M = _m()
            M.db.session.add(M.VehicleSync(vehicle_id=v.id, timestamp=JETZT,
                                           battery_12v_percent=wert))
            M.db.session.commit()
            assert is_12v_low(v.id) is erwartet, (wert, erwartet)
            M.VehicleSync.query.delete()
            M.db.session.commit()
    finally:
        ctx.pop()


# ── Der Platzhalter lässt sich befreien ───────────────────────────────

def test_platzhalter_wird_durch_kilometerstand_befreit():
    app, ctx = _app()
    try:
        from services.trips_service import release_stuck_unknown_events
        v = _auto()
        pe = _platzhalter(v.id, odo=91871)
        _sync(v.id, odo=91928)                      # 57 km weitergefahren
        assert release_stuck_unknown_events() == 1
        assert _m().ParkingEvent.query.get(pe.id).departed_at is not None
    finally:
        ctx.pop()


def test_platzhalter_ohne_kilometerbeweis_bleibt_offen():
    """Ein Auto, das wirklich noch dort steht, darf nicht abgeraeumt werden."""
    app, ctx = _app()
    try:
        from services.trips_service import release_stuck_unknown_events
        v = _auto()
        pe = _platzhalter(v.id, odo=91871)
        _sync(v.id, odo=91871)                      # keinen Meter bewegt
        assert release_stuck_unknown_events() == 0
        assert _m().ParkingEvent.query.get(pe.id).departed_at is None
    finally:
        ctx.pop()


def test_beschrifteter_parkvorgang_wird_nie_angefasst():
    app, ctx = _app()
    try:
        from services.trips_service import release_stuck_unknown_events
        v = _auto()
        pe = _platzhalter(v.id, odo=91871, etikett='home')
        _sync(v.id, odo=91928)
        assert release_stuck_unknown_events() == 0
        assert _m().ParkingEvent.query.get(pe.id).departed_at is None
    finally:
        ctx.pop()


def test_fremdes_fahrzeug_bleibt_unberuehrt():
    app, ctx = _app()
    try:
        from services.trips_service import release_stuck_unknown_events
        a = _auto(); b = _auto()
        pa = _platzhalter(a.id, odo=100)
        pb = _platzhalter(b.id, odo=200)
        _sync(a.id, odo=180)                        # nur a ist gefahren
        _sync(b.id, odo=200)
        assert release_stuck_unknown_events() == 1
        assert _m().ParkingEvent.query.get(pa.id).departed_at is not None
        assert _m().ParkingEvent.query.get(pb.id).departed_at is None
    finally:
        ctx.pop()


# ── Die Rettung greift jetzt auch ohne Hyundai ────────────────────────

def test_odo_rettung_greift_bei_kia_ohne_frisches_gps():
    """Der Fall, der 25 h lang keinen Ausgang hatte."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto('kia')
        pe = _platzhalter(v.id, odo=91871)
        # GPS da, aber mit 90 min altem ECU-Zeitstempel -> nicht frisch.
        s = _sync(v.id, odo=91928, lat=51.11, lon=13.92, gps_alter_min=90)
        update_parking_from_sync(s)
        assert _m().ParkingEvent.query.get(pe.id).departed_at is not None, \
            'der Platzhalter haette geschlossen werden muessen'
    finally:
        ctx.pop()


def test_mit_frischem_gps_bleibt_der_bewaehrte_weg():
    """Gegenprobe: mit frischem GPS darf sich fuer Kia nichts aendern —
    dann entscheidet die Bewegungserkennung, nicht der Odo-Zweig."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto('kia')
        pe = _platzhalter(v.id, odo=91871, etikett='home')
        pe.lat, pe.lon = 51.1247, 13.7206
        _m().db.session.commit()
        # Frisches GPS (2 min alt) an einem anderen Ort.
        s = _sync(v.id, odo=91928, lat=51.1120, lon=13.9194, gps_alter_min=2)
        update_parking_from_sync(s)
        geschlossen = _m().ParkingEvent.query.get(pe.id)
        assert geschlossen.departed_at is not None
        # Und der neue Parkvorgang hat echte Koordinaten, keinen Sentinel:
        PE = _m().ParkingEvent
        neu = (PE.query
               .filter(PE.id != pe.id)
               .order_by(PE.id.desc()).first())
        assert neu is not None and (neu.lat, neu.lon) != (0.0, 0.0), \
            'mit frischem GPS darf kein Platzhalter entstehen'
    finally:
        ctx.pop()


# ── Der Platzhalter muss auch schließen, wenn der Ort unklar bleibt ───

def _flip_lage(vid, pe_odo, pe_wann):
    """Die Lage, in der ``flip`` dauerhaft wahr ist.

    Innerhalb der Lebenszeit des Platzhalters liegt ein frueherer Sync mit
    FRISCHEM GPS an einem ANDEREN Ort. Danach misstraut der Waechter jeder
    Koordinate — zu Recht. Nur darf das nicht heissen, dass die Abfahrt
    verschwiegen wird.
    """
    _sync(vid, odo=pe_odo, wann=pe_wann + timedelta(minutes=5),
          lat=52.5200, lon=13.4050, gps_alter_min=2)      # weit weg, frisch


def test_platzhalter_schliesst_auch_wenn_die_koordinate_misstraut_wird():
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto('kia')
        wann = JETZT - timedelta(hours=25)
        pe = _platzhalter(v.id, odo=91871, wann=wann)
        _flip_lage(v.id, 91871, wann)
        # Jetzt: frisches GPS UND der Kilometerstand ist weitergelaufen.
        s = _sync(v.id, odo=91928, lat=51.1120, lon=13.9194, gps_alter_min=2)
        update_parking_from_sync(s)
        PE = _m().ParkingEvent
        assert PE.query.get(pe.id).departed_at is not None, \
            'der Platzhalter haette schliessen muessen'
        neu_pe = (PE.query.filter(PE.id != pe.id)
                  .order_by(PE.id.desc()).first())
        assert neu_pe is not None and neu_pe.departed_at is None
        # Geraten wird nichts: der neue Parkvorgang bleibt ohne Ort.
        assert neu_pe.label == 'unknown', neu_pe.label
        assert (neu_pe.lat, neu_pe.lon) == (0.0, 0.0)
    finally:
        ctx.pop()


def test_ohne_kilometerbeweis_bleibt_der_waechter_streng():
    """Gegenprobe: misstraute Koordinate UND kein gefahrener Kilometer —
    dann darf sich nichts aendern, das ist der Sinn des Waechters."""
    app, ctx = _app()
    try:
        from services.trips_service import update_parking_from_sync
        v = _auto('kia')
        wann = JETZT - timedelta(hours=25)
        pe = _platzhalter(v.id, odo=91871, wann=wann)
        _flip_lage(v.id, 91871, wann)
        vorher = _m().ParkingEvent.query.count()
        s = _sync(v.id, odo=91871, lat=51.1120, lon=13.9194, gps_alter_min=2)
        update_parking_from_sync(s)
        PE = _m().ParkingEvent
        assert PE.query.get(pe.id).departed_at is None, 'haette offen bleiben muessen'
        assert PE.query.get(pe.id).label == 'unknown'
        assert PE.query.count() == vorher, 'kein neuer Parkvorgang ohne Beweis'
    finally:
        ctx.pop()


# ── Die Lücke aus den Rohdaten füllen ─────────────────────────────────

def _beschrifteter_pe(vid, lat, lon, an, ab, odo, etikett='home'):
    M = _m()
    pe = M.ParkingEvent(vehicle_id=vid, arrived_at=an, last_seen_at=ab,
                        departed_at=ab, lat=lat, lon=lon, label=etikett,
                        odometer_arrived=odo, odometer_departed=odo)
    M.db.session.add(pe); M.db.session.commit()
    return pe


def test_luecke_wird_aus_den_rohdaten_gefuellt():
    """Das Buch steht, die Rohdaten sind vollständig — dann muss es wachsen."""
    app, ctx = _app()
    try:
        from services.trips_service import replay_gap_for_every_vehicle
        v = _auto('kia')
        t0 = JETZT - timedelta(hours=20)
        _beschrifteter_pe(v.id, 51.1120, 13.9194, t0, t0 + timedelta(minutes=30), 91867)
        # Drei Orte nach dem Ende, alle mit frischem GPS.
        for n, (la, lo, odo) in enumerate(((51.1120, 13.9194, 91867),
                                           (51.1278, 13.9543, 91871),
                                           (51.1248, 13.7195, 91899)), start=1):
            _sync(v.id, odo=odo, wann=t0 + timedelta(hours=n), lat=la, lon=lo,
                  gps_alter_min=3)
        vorher = _m().ParkingEvent.query.count()
        erg = replay_gap_for_every_vehicle()
        nachher = _m().ParkingEvent.query.count()
        assert nachher > vorher, (vorher, nachher, erg)
        assert erg and list(erg.values())[0]['syncs_processed'] == 3, erg
        # Und die Orte stehen wirklich drin, nicht als Platzhalter.
        orte = {(round(p.lat, 4), round(p.lon, 4))
                for p in _m().ParkingEvent.query.all()}
        assert (51.1278, 13.9543) in orte, orte
    finally:
        ctx.pop()


def test_bei_laufendem_buch_wird_nichts_nachgetragen():
    """Gegenprobe: der neueste Parkvorgang ist OFFEN — dann läuft das Buch."""
    app, ctx = _app()
    try:
        from services.trips_service import replay_gap_for_every_vehicle
        v = _auto('kia')
        t0 = JETZT - timedelta(hours=5)
        pe = _platzhalter(v.id, odo=91867, wann=t0, etikett='home')
        pe.departed_at = None
        _m().db.session.commit()
        _sync(v.id, odo=91871, wann=t0 + timedelta(hours=1),
              lat=51.1278, lon=13.9543, gps_alter_min=3)
        vorher = _m().ParkingEvent.query.count()
        erg = replay_gap_for_every_vehicle()
        assert erg == {}, erg
        assert _m().ParkingEvent.query.count() == vorher
    finally:
        ctx.pop()


def test_ohne_rohdaten_nach_dem_ende_bleibt_es_dabei():
    app, ctx = _app()
    try:
        from services.trips_service import replay_gap_for_every_vehicle
        v = _auto('kia')
        t0 = JETZT - timedelta(hours=5)
        _beschrifteter_pe(v.id, 51.1120, 13.9194, t0, t0 + timedelta(minutes=10), 91867)
        vorher = _m().ParkingEvent.query.count()
        assert replay_gap_for_every_vehicle() == {}
        assert _m().ParkingEvent.query.count() == vorher
    finally:
        ctx.pop()


def test_der_erzeugende_sync_wird_nicht_doppelt_verarbeitet():
    """``since`` ist ausschliessend — sonst entstuende ein Doppel des
    Parkvorgangs, der gerade geschlossen wurde."""
    app, ctx = _app()
    try:
        from services.trips_service import rebuild_parking_events_since
        v = _auto('kia')
        t0 = JETZT - timedelta(hours=5)
        ende = t0 + timedelta(minutes=10)
        _beschrifteter_pe(v.id, 51.1120, 13.9194, t0, ende, 91867)
        # Genau auf dem Anker, mit demselben Ort:
        _sync(v.id, odo=91867, wann=ende, lat=51.1120, lon=13.9194, gps_alter_min=2)
        vorher = _m().ParkingEvent.query.count()
        erg = rebuild_parking_events_since(ende, vehicle_id=v.id)
        assert erg['syncs_processed'] == 0, erg
        assert _m().ParkingEvent.query.count() == vorher
    finally:
        ctx.pop()


# ── Die Oberfläche darf die Zahl nicht behaupten ──────────────────────

def test_keine_feste_schwelle_in_vorlage_und_texten():
    vorlage = open(os.path.join(ROOT, 'templates', 'dashboard.html'),
                   encoding='utf-8').read()
    assert 'threshold: { value: 70' not in vorlage
    for lang in ('de', 'en', 'es', 'fr', 'it', 'nl'):
        d = json.load(open(os.path.join(ROOT, 'translations', '%s.json' % lang),
                           encoding='utf-8'))
        text = d['dash.low_12v_threshold_label']
        assert '{pct}' in text, lang
        assert not re.search(r'\b70\b', text), (lang, text)


def test_alle_sechs_sprachen_tragen_die_neuen_schluessel():
    mengen = {}
    for lang in ('de', 'en', 'es', 'fr', 'it', 'nl'):
        d = json.load(open(os.path.join(ROOT, 'translations', '%s.json' % lang),
                           encoding='utf-8'))
        for k in ('dash.stale_gps_title', 'dash.stale_gps_hint'):
            assert k in d and d[k].strip(), (lang, k)
        mengen[lang] = set(d)
    basis = mengen['de']
    for lang, m in mengen.items():
        assert m == basis, (lang, sorted(m ^ basis)[:5])
