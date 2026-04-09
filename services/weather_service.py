"""Open-Meteo weather correlation service.

Fetches daily mean temperatures for a coordinate and caches them in
WeatherCache. Used to correlate consumption (kWh/100km) with outside
temperature so the user can see why winter is more expensive.

Open-Meteo is free, no API key, rate-limited to ~10000 requests/day.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

from models.database import db, WeatherCache, AppConfig, Charge

logger = logging.getLogger(__name__)

API_BASE = 'https://archive-api.open-meteo.com/v1/archive'


def _key(value: float) -> str:
    return f'{value:.2f}'


def _get_home_coords():
    try:
        lat = AppConfig.get('home_lat')
        lon = AppConfig.get('home_lon')
        if lat and lon:
            return float(lat), float(lon)
    except (ValueError, TypeError):
        pass
    return None


def fetch_temp_for_date(d: date, lat: float, lon: float) -> Optional[float]:
    """Return daily mean temperature for `d` at coords. Caches results."""
    lat_k, lon_k = _key(lat), _key(lon)
    cached = WeatherCache.query.filter_by(date=d, lat_key=lat_k, lon_key=lon_k).first()
    if cached:
        return cached.temp_mean_c

    # Fetch from Open-Meteo
    params = {
        'latitude': f'{lat:.4f}',
        'longitude': f'{lon:.4f}',
        'start_date': d.isoformat(),
        'end_date': d.isoformat(),
        'daily': 'temperature_2m_mean',
        'timezone': 'auto',
    }
    url = API_BASE + '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EV-Charge-Tracker'})
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode())
        temps = (data.get('daily') or {}).get('temperature_2m_mean') or []
        temp = temps[0] if temps else None
    except Exception as e:
        logger.warning(f"Open-Meteo fetch failed for {d}: {e}")
        temp = None

    entry = WeatherCache(date=d, lat_key=lat_k, lon_key=lon_k, temp_mean_c=temp)
    db.session.add(entry)
    db.session.commit()
    return temp


def fetch_range(start: date, end: date, lat: float, lon: float) -> dict:
    """Bulk-fetch a date range, returning {date_iso: temp_c}. Uses cache."""
    out = {}
    # Determine which dates are missing from cache
    cached_dates = set()
    lat_k, lon_k = _key(lat), _key(lon)
    rows = (WeatherCache.query
            .filter(WeatherCache.lat_key == lat_k, WeatherCache.lon_key == lon_k)
            .filter(WeatherCache.date >= start, WeatherCache.date <= end).all())
    for r in rows:
        out[r.date.isoformat()] = r.temp_mean_c
        cached_dates.add(r.date)

    # Find earliest/latest missing date
    missing = []
    cur = start
    while cur <= end:
        if cur not in cached_dates:
            missing.append(cur)
        cur += timedelta(days=1)
    if not missing:
        return out

    # Fetch the whole missing span in one request (Open-Meteo allows ranges)
    m_start = min(missing)
    m_end = max(missing)
    params = {
        'latitude': f'{lat:.4f}',
        'longitude': f'{lon:.4f}',
        'start_date': m_start.isoformat(),
        'end_date': m_end.isoformat(),
        'daily': 'temperature_2m_mean',
        'timezone': 'auto',
    }
    url = API_BASE + '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EV-Charge-Tracker'})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode())
        days = (data.get('daily') or {}).get('time') or []
        temps = (data.get('daily') or {}).get('temperature_2m_mean') or []
        for day_str, temp in zip(days, temps):
            try:
                day_d = date.fromisoformat(day_str)
            except ValueError:
                continue
            if day_d in cached_dates:
                continue
            entry = WeatherCache(date=day_d, lat_key=lat_k, lon_key=lon_k, temp_mean_c=temp)
            db.session.add(entry)
            out[day_str] = temp
        db.session.commit()
    except Exception as e:
        logger.warning(f"Open-Meteo bulk fetch failed: {e}")
    return out


def get_consumption_temperature_correlation(months: int = 12):
    """Group charges by month, return points {month, kwh, temp_avg}."""
    home = _get_home_coords()
    if not home:
        return None
    lat, lon = home

    cutoff = date.today().replace(day=1) - timedelta(days=months * 31)
    charges = (Charge.query
               .filter(Charge.date >= cutoff)
               .order_by(Charge.date.asc()).all())
    if not charges:
        return None

    # Bulk fetch temps for the whole charge date range
    start_d = min(c.date for c in charges)
    end_d = max(c.date for c in charges)
    temps_map = fetch_range(start_d, end_d, lat, lon)

    # Group by month
    monthly = {}
    for c in charges:
        key = (c.date.year, c.date.month)
        bucket = monthly.setdefault(key, {'kwh': 0.0, 'temps': []})
        bucket['kwh'] += c.kwh_loaded or 0.0
        t = temps_map.get(c.date.isoformat())
        if t is not None:
            bucket['temps'].append(t)

    out = []
    for (year, month), bucket in sorted(monthly.items()):
        if not bucket['temps']:
            continue
        avg_t = sum(bucket['temps']) / len(bucket['temps'])
        out.append({
            'label': f'{month:02d}/{year}',
            'kwh': round(bucket['kwh'], 1),
            'temp_avg_c': round(avg_t, 1),
        })
    return out
