"""Reverse geocoding via OpenStreetMap Nominatim, with DB cache.

v2.28.17 switched from storing Nominatim's verbose ``display_name`` to a
compact form derived from the structured ``address`` object: either a
POI name ("Lidl", "Rewe", "IKEA", …) or a street + house number, in
both cases followed by postcode + city. This keeps the driving-log
table readable without truncating country/state padding on every row.

The full API response is stored alongside in ``raw_json`` so the short
format can be re-derived later without another API call.

v2.28.18 added a background-maintenance thread that slowly rebuilds
legacy cache entries (and retries transient Nominatim failures) one
row at a time, with a conservative 2 s spacing. It sleeps 10 min
between probes when nothing's pending, and backs off 60 s after a
Nominatim error (typically 429). This spreads Nominatim load out over
time so bulk migration after a v2.28.17-style upgrade can't re-trigger
the block we hit during the initial rollout.
"""
from __future__ import annotations

import json
import logging
import threading
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


def _format_short(nominatim_data: dict) -> Optional[str]:
    """Turn a Nominatim reverse-geocode response into a compact label.

    Priority:
    1. POI (shop, amenity, leisure, tourism) → "POI-Name, PLZ Stadt"
    2. Street + house number → "Straße Nr, PLZ Stadt"
    3. Fallback → just "PLZ Stadt" or ``display_name``.

    City picks from city → town → village → municipality → suburb, so
    rural locations still get something useful. The ``name`` field on
    the response is preferred over ``addr['shop']`` when both exist,
    because ``name`` carries the brand ("Lidl", "REWE") rather than the
    generic type ("supermarket").
    """
    if not isinstance(nominatim_data, dict):
        return None
    addr = nominatim_data.get('address') or {}
    if not isinstance(addr, dict):
        addr = {}

    # POI detection. ``name`` is often the cleanest (brand) label; fall
    # back to whichever structured field actually holds a value.
    name = (nominatim_data.get('name') or '').strip()
    road = (addr.get('road') or '').strip()
    poi_name: Optional[str] = None
    if name and name != road:
        poi_name = name
    else:
        for fld in ('shop', 'amenity', 'leisure', 'tourism', 'office'):
            val = addr.get(fld)
            if val and str(val).strip() and str(val) != road:
                poi_name = str(val).strip()
                break

    city = (
        addr.get('city')
        or addr.get('town')
        or addr.get('village')
        or addr.get('municipality')
        or addr.get('suburb')
        or ''
    )
    city = (city or '').strip()
    postcode = (addr.get('postcode') or '').strip()
    house = (addr.get('house_number') or '').strip()

    city_line = (f'{postcode} {city}').strip()
    street_line = (f'{road} {house}').strip()

    if poi_name:
        return ', '.join(p for p in (poi_name, city_line) if p) or None
    if street_line:
        return ', '.join(p for p in (street_line, city_line) if p) or None
    if city_line:
        return city_line
    return nominatim_data.get('display_name')


def _fetch_nominatim(lat: float, lon: float, language: str) -> Optional[dict]:
    """One rate-limited call to Nominatim /reverse. Returns parsed JSON
    or None on any failure."""
    global _LAST_REQUEST_TS
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
        'addressdetails': 1,
    }
    url = NOMINATIM_BASE + '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Nominatim reverse failed for {lat},{lon}: {e}")
        return None


def reverse(lat: float, lon: float, language: str = 'de') -> Optional[str]:
    """Return a compact human-readable address for ``lat, lon``. Cached.

    Pre-v2.28.17 cache entries stored ``display_name`` in ``address``
    with no ``raw_json``; those are returned as-is (a one-off
    ``rebuild_legacy_entries()`` run converts them to short form).
    """
    lat_k, lon_k = _key(lat), _key(lon)
    cached = GeocodeCache.query.filter_by(lat_key=lat_k, lon_key=lon_k).first()
    if cached:
        # If we have the raw response, always re-derive the short form
        # from it — that way the rendering follows the current formatter
        # rather than whatever was stored at original fetch time.
        if cached.raw_json:
            try:
                short = _format_short(json.loads(cached.raw_json))
                if short:
                    return short
            except Exception:
                pass
        return cached.address

    data = _fetch_nominatim(lat, lon, language)
    if data is None:
        entry = GeocodeCache(lat_key=lat_k, lon_key=lon_k, address=None, raw_json=None)
        db.session.add(entry)
        db.session.commit()
        return None

    short = _format_short(data)
    entry = GeocodeCache(
        lat_key=lat_k, lon_key=lon_k,
        address=short,
        raw_json=json.dumps(data, ensure_ascii=False),
    )
    db.session.add(entry)
    db.session.commit()
    return short


def rebuild_legacy_entries(limit: int = 100, language: str = 'de') -> int:
    """Re-fetch cache rows that predate v2.28.17 (raw_json IS NULL) so
    they get the new short-form address. Rate-limited; returns the
    number of rows updated in this pass. Intended for CLI / one-shot
    use — the background maintenance loop does the same job trickle-
    fed in production."""
    rows = (GeocodeCache.query
            .filter(GeocodeCache.raw_json.is_(None))
            .limit(limit)
            .all())
    updated = 0
    for row in rows:
        ok = _rebuild_one_legacy_row(row, language=language)
        if ok:
            updated += 1
    return updated


# ── Background maintenance loop ───────────────────────────────────────

_maintenance_thread: Optional[threading.Thread] = None
_maintenance_running: bool = False

_IDLE_SLEEP_S = 600       # 10 min when no legacy entries are pending
_RATE_LIMIT_S = 2.0       # between successful rebuilds (2 s > Nominatim's 1 req/s policy)
_BACKOFF_S = 60           # after Nominatim error (typically 429 — block usually clears in minutes)


def _cascade_pe_addresses(lat_key: str, lon_key: str, short: Optional[str]) -> int:
    """Copy the new short address onto every ParkingEvent whose rounded
    coords match this cache key. Returns the number of rows updated.

    Without this, PE.address only refreshes when
    ``services.trips_service.geocode_missing_events`` happens to run and
    the PE row has ``address IS NULL`` — i.e. never, for existing rows.
    The maintenance loop calls this after each successful cache rebuild
    so the UI actually sees the new short form without a manual reset.
    """
    from models.database import ParkingEvent
    updated = 0
    # Approach: filter PE by a coarse bounding box, then confirm the
    # rounded key matches. Avoids a full-table scan when the repo grows.
    try:
        lat_f = float(lat_key)
        lon_f = float(lon_key)
    except (TypeError, ValueError):
        return 0
    eps = 0.0002  # 4-decimal key rounds to ~11m; this box is ~22m each side
    candidates = (ParkingEvent.query
                  .filter(ParkingEvent.lat >= lat_f - eps,
                          ParkingEvent.lat <= lat_f + eps,
                          ParkingEvent.lon >= lon_f - eps,
                          ParkingEvent.lon <= lon_f + eps)
                  .all())
    for pe in candidates:
        if _key(pe.lat) != lat_key or _key(pe.lon) != lon_key:
            continue
        if pe.address != short:
            pe.address = short
            updated += 1
    if updated:
        db.session.commit()
    return updated


def _rebuild_one_legacy_row(row: GeocodeCache, language: str = 'de') -> bool:
    """Fetch Nominatim for one cache row and persist short form +
    raw_json. Also cascades the new short address onto matching PE
    rows. Returns True on success, False on Nominatim error (which
    leaves the row untouched for the next pass)."""
    try:
        lat = float(row.lat_key)
        lon = float(row.lon_key)
    except (TypeError, ValueError):
        # Malformed key — drop the row so the loop doesn't spin on it.
        db.session.delete(row)
        db.session.commit()
        return False
    data = _fetch_nominatim(lat, lon, language)
    if data is None:
        return False
    short = _format_short(data)
    row.address = short
    row.raw_json = json.dumps(data, ensure_ascii=False)
    db.session.commit()
    _cascade_pe_addresses(row.lat_key, row.lon_key, short)
    return True


def _maintenance_loop(app) -> None:
    """Trickle-fed background rebuild of legacy cache entries.

    Runs forever while the service is up. Picks up new legacy rows on
    each probe — this handles both (a) the one-time v2.28.17 migration
    and (b) future transient Nominatim failures whose raw_json-NULL
    rows sit in the cache until the service comes back and we can
    retry.
    """
    global _maintenance_running
    _maintenance_running = True
    logger.info('Geocode address-maintenance loop started')
    while _maintenance_running:
        try:
            with app.app_context():
                row = (GeocodeCache.query
                       .filter(GeocodeCache.raw_json.is_(None))
                       .order_by(GeocodeCache.id.asc())
                       .first())
                if row is None:
                    sleep_s = _IDLE_SLEEP_S
                else:
                    ok = _rebuild_one_legacy_row(row)
                    sleep_s = _RATE_LIMIT_S if ok else _BACKOFF_S
                    if ok:
                        logger.info(
                            f'Geocode maintenance: rebuilt cache#{row.id} '
                            f'({row.lat_key},{row.lon_key}) → {row.address!r}'
                        )
        except Exception as e:
            logger.warning(f'Geocode maintenance error: {e}')
            sleep_s = _BACKOFF_S

        # Interruptible sleep so stop_address_maintenance() is responsive.
        slept = 0
        while slept < sleep_s and _maintenance_running:
            time.sleep(min(5, sleep_s - slept))
            slept += 5
    logger.info('Geocode address-maintenance loop stopped')


def start_address_maintenance(app) -> bool:
    """Start the background maintenance thread. No-op if already running."""
    global _maintenance_thread, _maintenance_running
    if _maintenance_running:
        return False
    _maintenance_thread = threading.Thread(
        target=_maintenance_loop, args=(app,), daemon=True,
        name='geocode-maintenance',
    )
    _maintenance_thread.start()
    return True


def stop_address_maintenance() -> None:
    """Signal the maintenance thread to stop at its next sleep boundary."""
    global _maintenance_running
    _maintenance_running = False
