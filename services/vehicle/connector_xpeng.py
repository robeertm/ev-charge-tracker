"""XPENG connector via the Enode aggregator API.

XPENG publishes no public developer API of its own — its open platform
(open.xiaopeng.com) is geo-restricted and account-gated, and there is no
pip-installable brand SDK the way there is for Kia/Hyundai/Skoda. The
established route the community uses (the ``mnordseth/xpeng-homeassistant``
integration, EVLinkHA, Homey's XPENG Car Manager) is **Enode**
(https://enode.com) — a hardware-aggregator that speaks to XPENG's cloud
on your behalf and exposes a single, well-documented REST API for battery,
charge state, odometer and location across 1000+ vehicle brands.

Because Enode is a plain OAuth2 + REST service, this connector needs no
extra dependency: it rides on ``requests`` (already a hard requirement of
the app). That makes XPENG the one brand that works out of the box with no
manual ``pip install`` on the host.

Setup (one-time, done by the vehicle owner):
  1. Create an Enode account at https://enode.io and request production
     access, then create an application → note its **Client ID** and
     **Client Secret**.
  2. Run Enode's hosted *Link* flow once to connect the XPENG account
     (this is where the user logs in with their XPENG credentials — we
     never see or store the XPENG password, only the Enode app keys).
  3. In the tracker's Fahrzeuge form, pick brand *XPENG* and enter:
       • Benutzer  = Enode Client ID
       • Passwort  = Enode Client Secret
       • Region    = ``EU``/``PROD`` (production, default) or ``TEST``/
                     ``SAND`` (sandbox)
       • VIN       = optional, only needed when several vehicles are
                     linked to the same Enode app.

Auth is OAuth2 ``client_credentials`` (HTTP Basic with the app keys) →
Bearer token, cached module-side until shortly before expiry. Data comes
from ``GET /vehicles`` with the ``chargeState``/``odometer``/``location``/
``information`` fields enriched.
"""
import base64
import logging
import time

try:
    import requests
    HAS_REQUESTS = True
except ImportError:  # requests is a hard dependency, but stay defensive
    HAS_REQUESTS = False

from .base import VehicleConnector, VehicleStatus
from .registry import register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "Enode Client ID", "type": "text"},
    {"key": "password", "label": "Enode Client Secret", "type": "password"},
    {"key": "region", "label": "Umgebung (EU/PROD oder TEST/SAND)", "type": "text"},
    {"key": "vin", "label": "VIN (optional, bei mehreren Fahrzeugen)", "type": "text"},
]

# Enode has two isolated environments. Sandbox is for development against
# simulated vehicles; production is the real one and the default here.
_ENV_URLS = {
    'production': {
        'oauth': 'https://oauth.production.enode.io/oauth2/token',
        'api':   'https://enode-api.production.enode.io',
    },
    'sandbox': {
        'oauth': 'https://oauth.sandbox.enode.io/oauth2/token',
        'api':   'https://enode-api.sandbox.enode.io',
    },
}

# Fields to enrich on the /vehicles listing (Enode returns metadata only
# unless you ask for the telemetry sub-objects explicitly).
_VEHICLE_FIELDS = ['information', 'chargeState', 'odometer', 'location']

_HTTP_TIMEOUT = 30  # seconds; mirrors the other connectors' async timeouts

# Module-level token cache keyed by (env, client_id). Connector instances
# are re-created on every sync tick, so caching the Bearer token here (not
# on ``self``) means we authenticate ~once an hour, not once a poll.
_TOKEN_CACHE: dict = {}


def _resolve_env(region: str) -> str:
    """Map the free-text Region field to an Enode environment key.

    The Fahrzeuge form uppercases and 6-char-caps the region, so we match
    on a prefix: anything starting SAND/TEST/DEV → sandbox, else the
    production default (covers the ``EU`` default and ``PROD``)."""
    r = (region or '').strip().upper()
    if r.startswith(('SAND', 'TEST', 'DEV')):
        return 'sandbox'
    return 'production'


class XpengConnector(VehicleConnector):

    def _env(self) -> str:
        return _resolve_env(self.credentials.get('region', ''))

    def _ensure_auth(self) -> str:
        """Return a valid Bearer token, fetching/refreshing as needed.

        Named to match the Kia/Hyundai connector so the Fahrzeuge
        "Testen" button (which calls ``connector._ensure_auth()``) works
        for XPENG too."""
        client_id = (self.credentials.get('username') or '').strip()
        client_secret = (self.credentials.get('password') or '').strip()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Enode Client ID und Client Secret erforderlich "
                "(Benutzer = Client ID, Passwort = Client Secret)")

        env = self._env()
        cache_key = (env, client_id)
        cached = _TOKEN_CACHE.get(cache_key)
        # Reuse while > 60 s of life remains.
        if cached and cached['expires_at'] - 60 > time.time():
            return cached['token']

        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode()).decode()
        try:
            resp = requests.post(
                _ENV_URLS[env]['oauth'],
                headers={
                    'Authorization': f'Basic {basic}',
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                data={'grant_type': 'client_credentials'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Enode-Token-Request fehlgeschlagen: {e}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Enode-Auth abgelehnt (HTTP {resp.status_code}): "
                f"{resp.text[:200]}")
        payload = resp.json()
        token = payload.get('access_token')
        if not token:
            raise RuntimeError("Enode lieferte kein access_token zurück")
        expires_in = int(payload.get('expires_in', 3600))
        _TOKEN_CACHE[cache_key] = {
            'token': token,
            'expires_at': time.time() + expires_in,
        }
        return token

    def _list_vehicles(self) -> list:
        token = self._ensure_auth()
        env = self._env()
        params = [('field', f) for f in _VEHICLE_FIELDS]
        try:
            resp = requests.get(
                f"{_ENV_URLS[env]['api']}/vehicles",
                headers={'Authorization': f'Bearer {token}'},
                params=params,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Enode /vehicles fehlgeschlagen: {e}")
        if resp.status_code == 401:
            # Token might have been revoked early — drop cache and bail;
            # next tick re-auths cleanly.
            _TOKEN_CACHE.pop((env, (self.credentials.get('username') or '').strip()), None)
            raise RuntimeError("Enode-Token abgelehnt (401) — bitte erneut testen")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Enode /vehicles HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        # Enode wraps the list in {"data": [...]}; tolerate a bare list too.
        return body.get('data', body) if isinstance(body, dict) else body

    def _pick_vehicle(self, vehicles: list) -> dict:
        if not vehicles:
            raise RuntimeError(
                "Kein Fahrzeug bei Enode verknüpft — bitte zuerst den "
                "XPENG-Account über den Enode-Link-Flow koppeln")
        vin = (self.credentials.get('vin') or '').strip().upper()
        if vin:
            for v in vehicles:
                info = v.get('information') or {}
                if (info.get('vin') or '').upper() == vin:
                    return v
        # No VIN filter (or no match): prefer an XPENG if the app has
        # several brands linked, else the first vehicle.
        for v in vehicles:
            if (v.get('vendor') or '').upper() == 'XPENG':
                return v
        return vehicles[0]

    @staticmethod
    def _to_status(v: dict) -> VehicleStatus:
        info = v.get('information') or {}
        charge = v.get('chargeState') or {}
        odo = v.get('odometer') or {}
        loc = v.get('location') or {}

        def _num(x):
            try:
                return None if x is None else float(x)
            except (ValueError, TypeError):
                return None

        soc = _num(charge.get('batteryLevel'))
        rng = _num(charge.get('range'))
        rate = _num(charge.get('chargeRate'))
        dist = _num(odo.get('distance'))
        eta = charge.get('chargeTimeRemaining')

        # Enode normalises plug/charge status into powerDeliveryState
        # ("UNPLUGGED", "PLUGGED_IN:CHARGING", "PLUGGED_IN:STOPPED", ...).
        # Fall back to the explicit booleans when present.
        pds = (charge.get('powerDeliveryState') or '').upper()
        is_charging = charge.get('isCharging')
        if is_charging is None:
            is_charging = pds.startswith('PLUGGED_IN:CHARGING')
        is_plugged = charge.get('isPluggedIn')
        if is_plugged is None:
            is_plugged = pds.startswith('PLUGGED_IN')

        lat = _num(loc.get('latitude'))
        lon = _num(loc.get('longitude'))

        model = info.get('model') or ''
        display = info.get('displayName') or (f"XPENG {model}".strip())

        status = VehicleStatus(
            soc_percent=int(round(soc)) if soc is not None else None,
            odometer_km=int(round(dist)) if dist is not None else None,
            is_charging=bool(is_charging),
            is_plugged_in=bool(is_plugged),
            charge_power_kw=round(rate, 2) if rate is not None else None,
            estimated_range_km=int(round(rng)) if rng is not None else None,
            est_charge_duration_min=int(eta) if isinstance(eta, (int, float)) else None,
            location_lat=lat,
            location_lon=lon,
            last_updated=charge.get('lastUpdated') or v.get('lastSeen'),
            vehicle_name=display,
            vehicle_model=display,
            registration_date=str(info.get('year')) if info.get('year') else None,
            raw_data=v,
        )
        # Feed the parking/trip GPS-staleness filter with Enode's own
        # location timestamp (ISO-8601); _extract_location_last_updated
        # parses this straight off the status attribute.
        loc_ts = loc.get('lastUpdated')
        if loc_ts:
            status.location_last_updated_at = loc_ts
        return status

    def authenticate(self) -> bool:
        try:
            self._ensure_auth()
            return True
        except Exception as e:
            logger.error(f"XPENG (Enode) auth failed: {e}")
            return False

    def test_connection(self) -> bool:
        try:
            self._list_vehicles()
            return True
        except Exception as e:
            logger.error(f"XPENG (Enode) test failed: {e}")
            return False

    def get_status(self, force=False) -> VehicleStatus:
        # ``force`` is a no-op for Enode: it polls the vehicle cloud on its
        # own cadence and always serves the freshest cached snapshot; there
        # is no per-request "wake the car" call in the read API.
        vehicles = self._list_vehicles()
        return self._to_status(self._pick_vehicle(vehicles))

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS

    @staticmethod
    def brand_name() -> str:
        return "XPENG (via Enode)"


if HAS_REQUESTS:
    register('xpeng', XpengConnector)
