# -*- coding: utf-8 -*-
"""The report's EUR/100 km must divide cost and kilometres from the SAME
time span.

A user picked "all time" in the report and read 75.56 EUR/100 km — about
seven times the dashboard figure. The report summed the cost of every
charge since the first entry, but took its kilometres from GPS trips,
which only exist since the vehicle connector began logging. Years of
cost over weeks of driving.

Run with:  python3 tests/test_report_km_basis.py
"""
import os
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import db, Charge, VehicleTrip  # noqa: E402
from services.report_range import build_report  # noqa: E402

_failures = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        _failures.append(msg)


def make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    return app


def seed(app):
    """Twelve months of charging (10 EUR each, 500 km between charges),
    but GPS trips only for the last month (500 km)."""
    with app.app_context():
        for m in range(1, 13):
            db.session.add(Charge(date=date(2025, m, 15), kwh_loaded=40.0,
                                  total_cost=10.0, odometer=10000 + m * 500,
                                  charge_type='AC'))
        db.session.add(VehicleTrip(trip_date=date(2025, 12, 20),
                                   start_time=datetime(2025, 12, 20, 8, 0),
                                   distance_km=500.0, drive_minutes=400))
        db.session.commit()


def test_all_time_uses_odometer_span():
    print("all time: cost and km from the same span")
    app = make_app(); seed(app)
    with app.app_context():
        r = build_report(date(2025, 1, 1), date(2025, 12, 31))
    s = r['summary']
    # 12 charges = 120 EUR; odometer 10500 -> 16000 = 5500 km -> 2.18 EUR/100km
    check(s['km_source'] == 'odometer', f"km come from the odometer (got {s['km_source']})")
    check(abs(s['total_km'] - 5500) < 0.01, f"total_km = 5500 (got {s['total_km']})")
    check(abs(s['avg_eur_per_100km'] - 2.18) < 0.01, f"2.18 EUR/100km (got {s['avg_eur_per_100km']})")
    check(abs(s['trip_km'] - 500) < 0.01, "trip km still reported for the trip plots")
    check(abs(s['avg_efficiency_kwh_per_100km'] - 480 / 55) < 0.05, "kWh/100km on the same basis")
    old_wrong = round(120 / 500 * 100, 2)
    check(s['avg_eur_per_100km'] != old_wrong, f"no longer {old_wrong} (years of cost over weeks of trips)")


def test_window_starts_at_previous_reading():
    print("a window inside the history starts at the last reading before it")
    app = make_app(); seed(app)
    with app.app_context():
        r = build_report(date(2025, 6, 1), date(2025, 6, 30))
    s = r['summary']
    # June: one charge (10 EUR) at 13000 km; previous reading (May) 12500 -> 500 km -> 2.00
    check(abs(s['total_km'] - 500) < 0.01, f"June = 500 km, not 0 (got {s['total_km']})")
    check(abs(s['avg_eur_per_100km'] - 2.0) < 0.01, f"June = 2.00 EUR/100km (got {s['avg_eur_per_100km']})")


def test_trips_are_the_fallback_without_odometer():
    print("no odometer anywhere: trips remain the basis")
    app = make_app()
    with app.app_context():
        db.session.add(Charge(date=date(2025, 12, 10), kwh_loaded=30.0, total_cost=9.0, charge_type='AC'))
        db.session.add(VehicleTrip(trip_date=date(2025, 12, 12), start_time=datetime(2025, 12, 12, 9, 0),
                                   distance_km=300.0, drive_minutes=200))
        db.session.commit()
        r = build_report(date(2025, 12, 1), date(2025, 12, 31))
    s = r['summary']
    check(s['km_source'] == 'trips', f"source = trips (got {s['km_source']})")
    check(abs(s['total_km'] - 300) < 0.01 and abs(s['avg_eur_per_100km'] - 3.0) < 0.01, "3.00 EUR/100km from trips")


if __name__ == '__main__':
    test_all_time_uses_odometer_span()
    test_window_starts_at_previous_reading()
    test_trips_are_the_fallback_without_odometer()
    print("\nFAILED" if _failures else "\nALL OK")
    sys.exit(1 if _failures else 0)
