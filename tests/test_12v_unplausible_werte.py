# -*- coding: utf-8 -*-
"""A 12 V reading above 100 % is not a measurement and must not be kept.

After work on the 12 V battery the ECU answers 255 (0xFF, "no value") until it
has calibrated again. On a real Kia that happened for nine syncs across 18
hours, and the sentinel did damage in two places at once:

  * the vehicle-history plot drew a 255 % spike, and
  * ``sync_service.is_12v_low`` reads the newest stored reading — so a 255
    would have answered "battery fine" and released the force-refresh guard
    whose whole job is to not wake a car with a weak 12 V battery.

So the fix is a plausibility gate, not a clamp: 255 means *unknown*, and
clamping it to 100 would invent a reading the car never reported. The field is
dropped, never the row — every other value of that sync is good, and the
untouched payload stays in ``raw_json``.

Figures here are invented; only the shape of the sentinel is real.

Run with:  python3 -m pytest tests/test_12v_unplausible_werte.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='ev12v-'))

from flask import Flask                                            # noqa: E402
from models.database import db, Vehicle, VehicleSync               # noqa: E402
from services.vehicle.base import VehicleStatus, plausible_percent  # noqa: E402

SENTINEL = 255


def _app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    return app, ctx


# ── the gate itself ───────────────────────────────────────────────────

def test_was_kein_prozentwert_sein_kann_wird_verworfen():
    for roh in (SENTINEL, 101, 1000, -1, 'kaputt'):
        assert plausible_percent(roh) is None, roh


def test_echte_werte_gehen_unveraendert_durch():
    for roh in (0, 44, 64, 93, 100, None):
        assert plausible_percent(roh) == roh, roh


def test_der_platzhalter_wird_nicht_auf_hundert_gestaucht():
    """A clamp would invent "battery full" out of "no value"."""
    assert plausible_percent(SENTINEL) != 100


# ── the write path ────────────────────────────────────────────────────

def test_der_platzhalter_kommt_nicht_in_die_zeile_und_der_rest_bleibt():
    import app as A
    zeile = A._build_vehicle_sync(
        VehicleStatus(soc_percent=71, odometer_km=91835,
                      battery_12v_percent=SENTINEL),
        battery_kwh=64.0, raw_json='{"roh": "bleibt"}', vehicle_id=1)
    assert zeile.battery_12v_percent is None
    # Only the one field is dropped — the sync itself is perfectly good.
    assert (zeile.soc_percent, zeile.odometer_km) == (71, 91835)
    assert zeile.raw_json == '{"roh": "bleibt"}'


def test_ein_echter_wert_wird_weiter_geschrieben():
    import app as A
    zeile = A._build_vehicle_sync(
        VehicleStatus(soc_percent=71, battery_12v_percent=64),
        battery_kwh=64.0, vehicle_id=1)
    assert zeile.battery_12v_percent == 64


# ── the guard that protects the 12 V battery ──────────────────────────

def test_ein_neuerer_platzhalter_verdeckt_keine_schwache_batterie():
    """The behavioural heart of it: before the gate, the newest row won —
    and a 255 on top of a 60 % reading said "fine, go wake the car"."""
    from services.vehicle import sync_service as S
    app, ctx = _app()
    try:
        db.session.add(Vehicle(id=1, name='Prüfwagen'))
        jetzt = datetime.now()
        db.session.add(VehicleSync(vehicle_id=1, battery_12v_percent=60,
                                   timestamp=jetzt - timedelta(hours=2)))
        db.session.add(VehicleSync(vehicle_id=1,
                                   battery_12v_percent=SENTINEL,
                                   timestamp=jetzt))
        db.session.commit()
        assert S._latest_12v_percent(1) == 60, 'der Platzhalter darf nicht gelten'
        assert S.is_12v_low(1) is True, 'die Sperre muss greifen'
    finally:
        ctx.pop()


def test_ohne_brauchbaren_wert_wird_die_erste_abfrage_nicht_blockiert():
    from services.vehicle import sync_service as S
    app, ctx = _app()
    try:
        db.session.add(Vehicle(id=1, name='Prüfwagen'))
        db.session.add(VehicleSync(vehicle_id=1, battery_12v_percent=SENTINEL,
                                   timestamp=datetime.now()))
        db.session.commit()
        assert S._latest_12v_percent(1) is None
        assert S.is_12v_low(1) is False
    finally:
        ctx.pop()


# ── the plot ──────────────────────────────────────────────────────────

def test_der_plot_zeigt_eine_luecke_statt_eines_ausschlags():
    from services import stats_service as ST
    app, ctx = _app()
    try:
        db.session.add(Vehicle(id=1, name='Prüfwagen'))
        jetzt = datetime.now()
        for versatz, wert in ((3, 67), (2, SENTINEL), (1, 64)):
            db.session.add(VehicleSync(vehicle_id=1, soc_percent=70,
                                       battery_12v_percent=wert,
                                       timestamp=jetzt - timedelta(hours=versatz)))
        db.session.commit()
        d = ST.get_vehicle_history(vehicle_id=1)
        assert d is not None
        reihe = d['series']['battery_12v']
        assert SENTINEL not in reihe, reihe
        assert reihe == [67, None, 64], reihe
        assert d['summary']['last']['battery_12v'] == 64
    finally:
        ctx.pop()
