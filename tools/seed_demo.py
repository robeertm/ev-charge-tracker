#!/usr/bin/env python3
"""Fill an EMPTY installation with a plausible demo dataset.

Why this exists: every screen of this app is a view onto history. A fresh
install shows empty charts and "no data yet", so there is no honest way to
show what the app looks like — and no way for someone evaluating it to see
anything before they have driven and charged for a month.

    docker exec ev-charge-tracker python tools/seed_demo.py

Two years of one fictional car: home AC, public DC, PV surplus, trips,
parking, maintenance and the sync history behind the vehicle page. The random
generator is SEEDED, so the same command always produces the same numbers and
a screenshot taken today matches one taken next year.

🔴 Refuses to touch a database that already holds charges. Nobody's real
history gets mixed with invented numbers.

🔴 Every place in here is invented. Coordinates point at city centres and
motorway services, never at a home — this file lives in a public repository.
"""
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app                                     # noqa: E402
from models.database import (Charge, MaintenanceEntry, ParkingEvent,  # noqa: E402
                             ThgQuota, Vehicle, VehicleSync, VehicleTrip, db)

RNG = random.Random(20260909)          # fixed: same picture every time
TAGE = 730                             # two years back from today

# Invented places. Coordinates are city centres / motorway services.
ORTE = [
    ("Home", "Home wallbox", 51.0504, 13.7373, "AC"),
    ("City Power", "Market Square car park", 51.0493, 13.7381, "AC"),
    ("Autohof Nord", "A4 services, north side", 51.1200, 13.6200, "DC"),
    ("Ladepark Süd", "Ring road retail park", 50.9800, 13.7900, "DC"),
    ("Stadtwerke", "Central station, level 2", 51.0400, 13.7320, "AC"),
    ("Highway Charge", "A17 services", 50.9100, 13.8300, "DC"),
]
WARTUNG = [
    ("inspection", "Annual service", 380.0),
    ("tyres", "Winter tyres fitted", 120.0),
    ("tyres", "Summer tyres fitted", 120.0),
    ("hu", "Roadworthiness test", 145.0),
    ("wiper", "Wiper blades replaced", 39.9),
    ("brakes", "Brake fluid change", 89.0),
    ("cabin_filter", "Cabin filter replaced", 55.0),
]


def _stromkosten(art):
    """Prices that look like a real tariff sheet, not like round numbers."""
    if art == "PV":
        return round(RNG.uniform(0.0, 0.10), 4)      # own solar, feed-in value
    if art == "AC":
        return round(RNG.uniform(0.28, 0.36), 4)     # home tariff
    return round(RNG.uniform(0.49, 0.79), 4)         # public fast charging


def baue(app):
    with app.app_context():
        # Guard on real HISTORY, not on the scaffold: a fresh install already
        # carries one placeholder vehicle ("Mein Auto") with no charges. Refusing
        # on that would make the script useless exactly where it is needed, and
        # adding a second vehicle would leave the app showing two.
        if Charge.query.count():
            print("This database already holds charges — refusing to seed. "
                  "Demo numbers must never mix with a real history.")
            return 1

        heute = date.today()
        start = heute - timedelta(days=TAGE)

        werte = dict(
            name="Demo EV", brand="demo", model="Compact 64",
            color="#3b82f6", icon="mdi:car-electric",
            battery_kwh=64.0, battery_kwh_gross=67.5,
            battery_soh_baseline=100.0, battery_co2_per_kwh=60.0,
            max_ac_kw=11.0, fossil_co2_per_km=140.0,
            recuperation_kwh_per_km=0.03,
            first_registered_at=start, acquired_at=start,
            notes="Fictional vehicle — demo data only.",
        )
        auto = Vehicle.query.first()          # reuse the placeholder if present
        if auto is None:
            auto = Vehicle(**werte)
            db.session.add(auto)
        else:
            for k, v in werte.items():
                setattr(auto, k, v)
        db.session.flush()

        km = 12500                       # odometer at the start of the period
        tag = start
        ladungen = fahrten = 0
        letzter_soc = 78

        while tag <= heute:
            # Roughly every second day a charge, weighted the way a commuter
            # charges: mostly at home, DC only on longer trips.
            if RNG.random() < 0.52:
                art = RNG.choices(["AC", "DC", "PV"], weights=[62, 18, 20])[0]
                ort = RNG.choice([o for o in ORTE if o[4] == art or art == "PV"])
                soc_von = RNG.randint(18, 55)
                soc_bis = RNG.randint(max(soc_von + 12, 60), 100 if art != "DC" else 82)
                kwh = round((soc_bis - soc_von) / 100.0 * 64.0 * RNG.uniform(1.03, 1.12), 2)
                preis = _stromkosten(art)
                verlust = round(kwh * RNG.uniform(0.03, 0.11), 2)
                start_h = RNG.randint(6, 22)
                co2 = RNG.randint(38, 62) if art == "PV" else RNG.randint(160, 480)
                c = Charge(
                    vehicle_id=auto.id, date=tag,
                    charge_hour=start_h,
                    charge_end_hour=(start_h + RNG.randint(1, 6)) % 24,
                    odometer=km, eur_per_kwh=preis, kwh_loaded=kwh,
                    total_cost=round(kwh * preis, 2), charge_type=art,
                    soc_from=soc_von, soc_to=soc_bis, soc_charged=soc_bis - soc_von,
                    loss_kwh=verlust, loss_pct=round(verlust / kwh * 100, 1),
                    co2_g_per_kwh=co2, co2_kg=round(kwh * co2 / 1000.0, 2),
                    co2_estimated=(RNG.random() < 0.08),
                    location_lat=ort[2], location_lon=ort[3],
                    location_name=ort[1], operator=ort[0],
                    start_fee_eur=(round(RNG.uniform(0.3, 0.8), 2) if art == "DC" else None),
                )
                db.session.add(c)
                ladungen += 1
                letzter_soc = soc_bis

            # Driving days
            if RNG.random() < 0.78:
                strecke = round(RNG.uniform(8, 140), 1)
                fahrzeit = int(strecke / RNG.uniform(0.55, 1.15))
                db.session.add(VehicleTrip(
                    vehicle_id=auto.id, trip_date=tag,
                    start_time=datetime.combine(tag, datetime.min.time()) +
                    timedelta(hours=RNG.randint(6, 19), minutes=RNG.randint(0, 59)),
                    drive_minutes=fahrzeit, idle_minutes=RNG.randint(2, 40),
                    distance_km=strecke,
                    avg_speed_kmh=round(strecke / (fahrzeit / 60.0), 1),
                    max_speed_kmh=RNG.randint(85, 165),
                    regen_kwh_per_100km=round(RNG.uniform(1.4, 4.8), 2),
                    consumption_kwh_per_100km=round(RNG.uniform(13.2, 22.6), 1),
                ))
                km += int(strecke)
                fahrten += 1

            # A sync roughly twice a day keeps the vehicle page's history dense.
            for stunde in (8, 20):
                db.session.add(VehicleSync(
                    vehicle_id=auto.id,
                    timestamp=datetime.combine(tag, datetime.min.time()) + timedelta(hours=stunde),
                    soc_percent=max(8, min(100, letzter_soc - RNG.randint(0, 14))),
                    odometer_km=km, is_charging=False,
                    estimated_range_km=int(64 * 0.9 * letzter_soc / 100 * 5.6),
                    battery_12v_percent=RNG.randint(82, 100),
                    battery_soh_percent=round(100.0 - (heute - tag).days / TAGE * 3.4, 1),
                    consumption_30d_kwh_per_100km=round(RNG.uniform(14.5, 20.5), 1),
                    # Ohne diese beiden zeigt das Dashboard "Total 0 kWh"
                    # direkt neben "Year 1642 kWh" — sichtbar widerspruechlich.
                    total_regenerated_kwh=round(km * 0.031, 1),
                    regen_cumulative_kwh=round(km * 0.031, 1),
                    location_lat=ORTE[0][2], location_lon=ORTE[0][3],
                ))
            tag += timedelta(days=1)

        # Parking, maintenance, quota
        # 🔴 Die App leitet die Fahrten aus PAAREN aufeinanderfolgender
        # Parkereignisse ab (Kilometerstand bei Abfahrt -> naechste Ankunft).
        # Zufaellige Kilometerstaende ergaben dort eine "laengste Fahrt" von
        # 6678 km — eine Zahl, die im Bildschirmfoto sofort auffaellt. Also
        # eine echte Kette: chronologisch, Kilometerstand monoton steigend.
        p_zeit = datetime.combine(heute - timedelta(days=45), datetime.min.time()) \
            + timedelta(hours=8)
        p_km = km - 3200
        for i in range(30):
            ort = RNG.choice(ORTE)
            steh = timedelta(hours=RNG.randint(2, 14))
            db.session.add(ParkingEvent(
                vehicle_id=auto.id, arrived_at=p_zeit,
                last_seen_at=p_zeit + steh - timedelta(minutes=5),
                departed_at=p_zeit + steh,
                lat=ort[2], lon=ort[3],
                label="home" if ort[0] == "Home" else "charger",
                favorite_name=ort[0], address=ort[1],
                odometer_arrived=p_km, odometer_departed=p_km,
                soc_arrived=RNG.randint(22, 55), soc_departed=RNG.randint(60, 98),
            ))
            gefahren = RNG.randint(6, 145)          # die Fahrt bis zum naechsten Halt
            p_km += gefahren
            p_zeit = p_zeit + steh + timedelta(hours=RNG.randint(1, 20))
        for j, (typ, titel, preis) in enumerate(WARTUNG):
            d = heute - timedelta(days=RNG.randint(30, 700))
            db.session.add(MaintenanceEntry(
                vehicle_id=auto.id, date=d, item_type=typ, title=titel,
                odometer_km=km - RNG.randint(500, 20000),
                cost_eur=preis, notes="Demo entry",
                next_due_km=km + RNG.randint(5000, 20000),
                next_due_date=d + timedelta(days=365),
            ))
        for jahr in (heute.year - 2, heute.year - 1):
            db.session.add(ThgQuota(vehicle_id=auto.id, year_from=jahr,
                                    year_to=jahr, amount_eur=float(RNG.randint(75, 320))))

        # Ohne Wetter bleibt die Kachel "Consumption vs temperature" leer —
        # ein leeres Feld im wichtigsten Bildschirmfoto. Es braucht BEIDES:
        # die Temperaturen UND eine hinterlegte Heimatkoordinate, denn danach
        # sucht get_consumption_temperature_correlation die Reihe. Ohne sie
        # bleibt die Kachel leer, obwohl Wetterdaten in der Datenbank stehen.
        from models.database import AppConfig, WeatherCache
        AppConfig.set("home_lat", str(ORTE[0][2]))
        AppConfig.set("home_lon", str(ORTE[0][3]))
        lat_k, lon_k = "%.2f" % ORTE[0][2], "%.2f" % ORTE[0][3]
        d = start
        while d <= heute:
            jahrestag = d.timetuple().tm_yday
            import math
            mittel = 9.5 - 9.0 * math.cos(2 * math.pi * (jahrestag - 15) / 365.0)
            db.session.add(WeatherCache(
                date=d, lat_key=lat_k, lon_key=lon_k,
                temp_mean_c=round(mittel + RNG.uniform(-4.0, 4.0), 1)))
            d += timedelta(days=1)

        db.session.commit()
        print("Seeded: %d charges, %d trips, %d km driven, %d maintenance entries."
              % (ladungen, fahrten, km - 12500, len(WARTUNG)))
        return 0


if __name__ == "__main__":
    sys.exit(baue(create_app()))
