"""When did the car take the GPS fix stored on a sync row?

Kept apart from ``app.py`` so the trip-log self-heal in
``services.trips_service`` can read the same answer from a stored row without
importing the web app.
"""
import json
from datetime import datetime
from typing import Optional


def to_local_naive(value) -> Optional[datetime]:
    """An aware or naive datetime, or an ISO-8601 string, as naive LOCAL time.

    Aware values are CONVERTED with ``astimezone()``; stripping only the label
    would leave a UTC clock reading that is one UTC offset too early (see the
    v3.0.151 note in ``app._extract_location_last_updated``). Anything
    unparsable is None.
    """
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, datetime):
        try:
            return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _as_dict(raw):
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def is_newer_feed(data) -> bool:
    """Is this vendor payload (the ``data`` block) the NEWER cached feed?

    Since the Kia/Hyundai library 5.x every EU car's cached status comes from
    the newer feed: a ``Location`` block instead of ``vehicleLocation``, and
    the position taken from the parked-location call. That position carries
    NO usable fix time. Measured on two live installs over a week:

      * right after a drive it is very often still the PREVIOUS parking
        spot, for up to two hours, while its time looks minutes old;
      * while the car stands, its time keeps moving with every poll — it
        is the cloud's contact time, not the moment of a GPS fix;
      * and the label is the region's zone over a UTC clock reading.

    A position like that can confirm a stop, it can never prove freshness.
    """
    return (isinstance(data, dict) and 'Location' in data
            and 'vehicleLocation' not in data)


def raw_is_newer_feed(raw) -> bool:
    return is_newer_feed(_as_dict(raw).get('data'))


def location_time_from_raw(raw) -> Optional[datetime]:
    """GPS fix time from a stored raw payload, as naive local time, or None.

    1. The connector's dump of the vendor library's parsed
       ``location_last_updated_at`` (top level, ISO-8601 with offset). The
       library knows which feed it read and on which clock (the forced GPS
       feed is UTC, the old cached feed region-local), so this is the value
       to trust.
    2. Legacy: ``data.vehicleLocation.time`` (compact ``YYYYMMDDHHMMSS``,
       region-local in the old cached feed). Rows written before the
       connector dumped the parsed value only carry this one.

    None for the newer cached feed (see ``is_newer_feed``): it has no fix
    time worth the name.
    """
    d = _as_dict(raw)
    if not d or is_newer_feed(d.get('data')):
        return None
    ts = to_local_naive(d.get('location_last_updated_at'))
    if ts is not None:
        return ts
    try:
        t = (((d.get('data') or {}).get('vehicleLocation') or {}).get('time') or '')
        t = str(t)
        if len(t) >= 14:
            return datetime.strptime(t[:14], '%Y%m%d%H%M%S')
    except (ValueError, KeyError, AttributeError, TypeError):
        pass
    return None
