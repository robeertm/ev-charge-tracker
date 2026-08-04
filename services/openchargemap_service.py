"""Nearest charging-station / operator lookup via Open Charge Map (OCM).

Open Charge Map (https://openchargemap.org) is a free, community-run open
registry of ~600k charging stations worldwide, each with operator name,
coordinates, connector power and (sometimes) usage cost. It's the one
resource from the `awesome-ev-charging` list that maps directly onto this
app's existing charge-form workflow: we already capture the car's GPS at
the moment of charging and reverse-geocode it via Nominatim — OCM lets us
take that same coordinate and derive *which CPO* the charge happened at,
so the operator field (and thus the price auto-fill) can be pre-filled.

Design mirrors ``geocode_service.py`` deliberately:
  * process-wide rate limiter (OCM asks apps to be gentle),
  * permanent DB cache keyed by rounded lat/lon (``OcmCache``),
  * every failure path degrades to ``None`` — the charge form must never
    break because a third-party API is down or unkeyed.

OCM now gates its API behind a (free) API key. Without a key the service
returns ``None`` and the UI simply doesn't offer a suggestion; the key is
configured in Settings and stored in ``AppConfig['ocm_api_key']``.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from typing import Optional

from models.database import db, OcmCache, AppConfig

logger = logging.getLogger(__name__)

OCM_BASE = 'https://api.openchargemap.io/v3/poi/'
USER_AGENT = 'EV-Charge-Tracker (self-hosted; https://github.com/robeertm/ev-charge-tracker)'

_LAST_REQUEST_TS = 0.0
_MIN_INTERVAL_S = 1.0          # be a good citizen against the shared OCM API
_SEARCH_RADIUS_KM = 1.0        # widen the net; the caller decides how close is "at the station"
# Default: only *suggest* an operator when the picked station is this close
# to the query point. A public charger's registered coord and the car's
# reported GPS rarely agree to the metre, but 150 m keeps us from grabbing
# the CPO across the street.
DEFAULT_MAX_DISTANCE_M = 150


def _key(value: float) -> str:
    """Round to 4 decimals (~11 m) so nearby coords share a cache row —
    identical scheme to geocode_service so the two caches age alike."""
    return f'{value:.4f}'


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _operator_from_poi(poi: dict) -> Optional[str]:
    """Extract a clean CPO name from one OCM POI object.

    Prefers the expanded ``OperatorInfo.Title``. OCM uses a handful of
    placeholder operators for "unknown"; those are treated as no-operator
    so we never auto-fill a meaningless value.
    """
    if not isinstance(poi, dict):
        return None
    op = poi.get('OperatorInfo') or {}
    if not isinstance(op, dict):
        return None
    title = (op.get('Title') or '').strip()
    if not title:
        return None
    low = title.lower()
    if low in ('(unknown operator)', 'unknown', 'unknown operator',
               '(business owner at site)', 'private individual',
               'private owner', 'test operator'):
        return None
    return title


def _station_title(poi: dict) -> Optional[str]:
    """Human-readable station label from ``AddressInfo.Title``."""
    if not isinstance(poi, dict):
        return None
    ai = poi.get('AddressInfo') or {}
    if not isinstance(ai, dict):
        return None
    title = (ai.get('Title') or '').strip()
    return title or None


def _max_power_kw(poi: dict) -> Optional[float]:
    """Highest per-connector PowerKW advertised at the station, if any."""
    conns = poi.get('Connections')
    if not isinstance(conns, list):
        return None
    powers = []
    for c in conns:
        if isinstance(c, dict):
            try:
                p = float(c.get('PowerKW'))
                if p > 0:
                    powers.append(p)
            except (TypeError, ValueError):
                continue
    return max(powers) if powers else None


def pick_nearest(pois: list, lat: float, lon: float) -> Optional[dict]:
    """From a list of OCM POI objects pick the closest one to (lat, lon)
    and return a compact normalised dict, or ``None`` if the list is empty.

    Distance is recomputed locally with haversine rather than trusting
    OCM's ``AddressInfo.Distance`` — that field's unit depends on the
    request and isn't present in every response shape.
    """
    if not isinstance(pois, list):
        return None
    best = None
    best_d = None
    for poi in pois:
        ai = (poi or {}).get('AddressInfo') or {}
        try:
            plat = float(ai.get('Latitude'))
            plon = float(ai.get('Longitude'))
        except (TypeError, ValueError):
            continue
        d = _haversine_m(lat, lon, plat, plon)
        if best_d is None or d < best_d:
            best_d = d
            best = poi
    if best is None:
        return None
    return {
        'operator': _operator_from_poi(best),
        'title': _station_title(best),
        'distance_m': int(round(best_d)),
        'power_kw': _max_power_kw(best),
        'poi': best,
    }


def _fetch_ocm(lat: float, lon: float, api_key: str) -> Optional[list]:
    """One rate-limited call to the OCM POI API. Returns the parsed POI
    list or ``None`` on any failure (network, non-JSON, 4xx/5xx)."""
    global _LAST_REQUEST_TS
    now = time.time()
    delay = _MIN_INTERVAL_S - (now - _LAST_REQUEST_TS)
    if delay > 0:
        time.sleep(delay)
    _LAST_REQUEST_TS = time.time()

    params = {
        'output': 'json',
        'latitude': f'{lat:.5f}',
        'longitude': f'{lon:.5f}',
        'distance': _SEARCH_RADIUS_KM,
        'distanceunit': 'KM',
        'maxresults': 8,
        'compact': 'true',      # trims payload; OperatorInfo stays expanded
        'verbose': 'false',
        'key': api_key,
    }
    url = OCM_BASE + '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT,
            'X-API-Key': api_key,       # OCM accepts the key as header too
        })
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning(f"OCM lookup failed for {lat},{lon}: {e}")
        return None


def nearest_operator(lat: float, lon: float,
                     max_distance_m: int = DEFAULT_MAX_DISTANCE_M) -> Optional[dict]:
    """Return ``{'operator', 'title', 'distance_m', 'power_kw'}`` for the
    charging station nearest to (lat, lon), or ``None``.

    ``None`` is returned when: no OCM API key is configured, the API is
    unreachable, no station is found, or the nearest station is farther
    than ``max_distance_m`` (so a suggestion is only made when we're
    reasonably sure the car is actually *at* that charger).

    Results are cached permanently in ``OcmCache`` keyed by rounded coord.
    A cached miss (no station nearby) is stored too, so we don't re-hit
    OCM every time the user logs a home charge.
    """
    lat_k, lon_k = _key(lat), _key(lon)
    cached = OcmCache.query.filter_by(lat_key=lat_k, lon_key=lon_k).first()
    if cached is not None:
        return _apply_distance_gate(cached, max_distance_m)

    api_key = (AppConfig.get('ocm_api_key', '') or '').strip()
    if not api_key:
        return None  # not configured — no cache row, so it works once keyed

    pois = _fetch_ocm(lat, lon, api_key)
    if pois is None:
        return None  # transient failure — don't poison the cache

    picked = pick_nearest(pois, lat, lon)
    entry = OcmCache(
        lat_key=lat_k, lon_key=lon_k,
        operator=(picked or {}).get('operator'),
        title=(picked or {}).get('title'),
        distance_m=(picked or {}).get('distance_m'),
        raw_json=json.dumps((picked or {}).get('poi')) if picked else None,
    )
    db.session.add(entry)
    db.session.commit()
    return _apply_distance_gate(entry, max_distance_m)


def _apply_distance_gate(row: 'OcmCache', max_distance_m: int) -> Optional[dict]:
    """Turn a cache row into the public result dict, honouring the
    caller's distance gate. A station outside the gate still counts as a
    resolved lookup (we don't refetch) but yields ``None``."""
    if row.distance_m is None:
        return None
    if row.distance_m > max_distance_m:
        return None
    power_kw = None
    if row.raw_json:
        try:
            power_kw = _max_power_kw(json.loads(row.raw_json))
        except Exception:
            power_kw = None
    return {
        'operator': row.operator,
        'title': row.title,
        'distance_m': row.distance_m,
        'power_kw': power_kw,
    }
