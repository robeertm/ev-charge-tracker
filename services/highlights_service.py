"""Highlights / interesting facts about the user's charging history."""
from typing import Optional

from models.database import Charge, ParkingEvent, VehicleSync
from services.trips_service import get_trips


def get_highlights() -> dict:
    """Compute interesting one-shot stats for the dashboard 'Highlights' card."""
    out = {}

    # Cheapest / most expensive charge per kWh (must have meaningful kWh)
    charges = (Charge.query
               .filter(Charge.eur_per_kwh.isnot(None), Charge.kwh_loaded.isnot(None),
                       Charge.kwh_loaded > 1)
               .all())
    if charges:
        cheap = min(charges, key=lambda c: c.eur_per_kwh)
        expensive = max(charges, key=lambda c: c.eur_per_kwh)
        out['cheapest_charge'] = {
            'date': cheap.date.isoformat() if cheap.date else None,
            'eur_per_kwh': round(cheap.eur_per_kwh, 3),
            'kwh': round(cheap.kwh_loaded, 1),
            'type': cheap.charge_type,
        }
        out['most_expensive_charge'] = {
            'date': expensive.date.isoformat() if expensive.date else None,
            'eur_per_kwh': round(expensive.eur_per_kwh, 3),
            'kwh': round(expensive.kwh_loaded, 1),
            'type': expensive.charge_type,
        }

        # Largest single charge (kWh)
        biggest = max(charges, key=lambda c: c.kwh_loaded)
        out['biggest_charge'] = {
            'date': biggest.date.isoformat() if biggest.date else None,
            'kwh': round(biggest.kwh_loaded, 1),
            'eur': round(biggest.total_cost, 2) if biggest.total_cost else None,
            'type': biggest.charge_type,
        }

    # Trip-based highlights
    trips = get_trips()
    if trips:
        # Longest trip by km
        with_km = [t for t in trips if t['km']]
        if with_km:
            longest = max(with_km, key=lambda t: t['km'])
            out['longest_trip'] = {
                'km': longest['km'],
                'duration_min': longest['duration_min'],
                'date': longest['from']['departed_at'],
            }
        # Fastest trip (avg speed)
        with_spd = [t for t in trips if t['avg_speed_kmh']]
        if with_spd:
            fastest = max(with_spd, key=lambda t: t['avg_speed_kmh'])
            out['fastest_trip'] = {
                'km': fastest['km'],
                'avg_speed_kmh': fastest['avg_speed_kmh'],
                'date': fastest['from']['departed_at'],
            }

    # Longest park (closed events with longest spread)
    closed_events = (ParkingEvent.query
                     .filter(ParkingEvent.departed_at.isnot(None))
                     .all())
    if closed_events:
        def _duration_h(e):
            return (e.departed_at - e.arrived_at).total_seconds() / 3600.0
        longest_park = max(closed_events, key=_duration_h)
        hours = _duration_h(longest_park)
        out['longest_park'] = {
            'hours': round(hours, 1),
            'days': round(hours / 24, 1),
            'arrived_at': longest_park.arrived_at.isoformat(),
            'label': longest_park.label,
            'name': longest_park.favorite_name,
        }

    return out


def get_charging_stations(limit: int = 10):
    """Group charges with location into 'stations' (rounded coords).

    Returns the cheapest / most-used stations.
    """
    charges = (Charge.query
               .filter(Charge.location_lat.isnot(None),
                       Charge.location_lon.isnot(None))
               .all())
    if not charges:
        return []

    stations = {}
    for c in charges:
        # 4 decimals ~= 11m bucket
        key = (round(c.location_lat, 3), round(c.location_lon, 3))
        s = stations.setdefault(key, {
            'lat': c.location_lat,
            'lon': c.location_lon,
            'name': c.location_name,
            'count': 0,
            'total_kwh': 0.0,
            'total_cost': 0.0,
            'cheapest_eur_per_kwh': None,
            'fastest_kw': None,
            'last_used': None,
            'type': c.charge_type,
        })
        s['count'] += 1
        if c.kwh_loaded:
            s['total_kwh'] += c.kwh_loaded
        if c.total_cost:
            s['total_cost'] += c.total_cost
        if c.eur_per_kwh is not None:
            if s['cheapest_eur_per_kwh'] is None or c.eur_per_kwh < s['cheapest_eur_per_kwh']:
                s['cheapest_eur_per_kwh'] = c.eur_per_kwh
        if c.date and (s['last_used'] is None or c.date > s['last_used']):
            s['last_used'] = c.date
        if c.location_name and not s['name']:
            s['name'] = c.location_name

    out = []
    for s in stations.values():
        avg_eur = (s['total_cost'] / s['total_kwh']) if s['total_kwh'] else None
        out.append({
            'lat': s['lat'],
            'lon': s['lon'],
            'name': s['name'] or f"{s['lat']:.4f}, {s['lon']:.4f}",
            'count': s['count'],
            'total_kwh': round(s['total_kwh'], 1),
            'total_cost': round(s['total_cost'], 2),
            'cheapest_eur_per_kwh': round(s['cheapest_eur_per_kwh'], 3) if s['cheapest_eur_per_kwh'] is not None else None,
            'avg_eur_per_kwh': round(avg_eur, 3) if avg_eur is not None else None,
            'last_used': s['last_used'].isoformat() if s['last_used'] else None,
            'type': s['type'],
        })
    out.sort(key=lambda s: -s['count'])
    return out[:limit]


def calculate_range(soc_percent: float, battery_kwh: float,
                    consumption_kwh_per_100km: float,
                    temp_c: Optional[float] = None) -> Optional[dict]:
    """Realistic range estimate from current SoC, battery and recent consumption.

    Applies a temperature factor based on `temp_c` (0–25°C optimal, colder
    or hotter = higher consumption).
    """
    if not soc_percent or not battery_kwh or not consumption_kwh_per_100km:
        return None
    available_kwh = (soc_percent / 100.0) * battery_kwh

    temp_factor = 1.0
    if temp_c is not None:
        if temp_c < 0:
            temp_factor = 1.30
        elif temp_c < 10:
            temp_factor = 1.18
        elif temp_c < 20:
            temp_factor = 1.06
        elif temp_c < 30:
            temp_factor = 1.00
        else:
            temp_factor = 1.10

    effective_cons = consumption_kwh_per_100km * temp_factor
    if effective_cons <= 0:
        return None
    range_km = (available_kwh / effective_cons) * 100
    return {
        'range_km': round(range_km, 0),
        'effective_consumption': round(effective_cons, 1),
        'temp_factor': round(temp_factor, 2),
        'available_kwh': round(available_kwh, 1),
    }
