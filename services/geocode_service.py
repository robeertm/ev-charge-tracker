"""Reverse geocoding via OpenStreetMap Nominatim, with DB cache."""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from models.database import db, GeocodeCache

logger = logging.getLogger(__name__)

NOMINATIM_BASE = 'https://nominatim.openstreetmap.org/reverse'
USER_AGENT = 'EV-Charge-Tracker/2.3 (self-hosted)'
_LAST_REQUEST_TS = 0.0
_MIN_INTERVAL_S = 1.1  # Nominatim usage policy: max 1 req/s


def _key(value: float) -> str:
    """Round to 4 decimals (~11m precision) so nearby coords share a cache."""
    return f'{value:.4f}'


def reverse(lat: float, lon: float, language: str = 'de') -> Optional[str]:
    """Return a human-readable address for `lat, lon`. Cached forever."""
    global _LAST_REQUEST_TS

    lat_k, lon_k = _key(lat), _key(lon)
    cached = GeocodeCache.query.filter_by(lat_key=lat_k, lon_key=lon_k).first()
    if cached:
        return cached.address

    # Rate-limit to respect Nominatim ToS
    now = time.time()
    delay = _MIN_INTERVAL_S - (now - _LAST_REQUEST_TS)
    if delay > 0:
        time.sleep(delay)
    _LAST_REQUEST_TS = time.time()

    params = {
        'lat': f'{lat:.5f}',
        'lon': f'{lon:.5f}',
        'format': 'jsonv2',
        'accept-language': language,
        'zoom': 17,
    }
    url = NOMINATIM_BASE + '?' + urllib.parse.urlencode(params)
    address = None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode())
        address = data.get('display_name')
    except Exception as e:
        logger.warning(f"Nominatim reverse failed for {lat},{lon}: {e}")

    entry = GeocodeCache(lat_key=lat_k, lon_key=lon_k, address=address)
    db.session.add(entry)
    db.session.commit()
    return address
