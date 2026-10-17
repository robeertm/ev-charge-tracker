"""Renault / Dacia connector via renault-api."""
import asyncio
import logging

try:
    from renault_api.renault_client import RenaultClient
    from aiohttp import ClientSession
    HAS_RENAULT = True
except ImportError:
    HAS_RENAULT = False

from .base import VehicleConnector, VehicleStatus, plausible_percent
from .registry import describe, register

logger = logging.getLogger(__name__)

CREDENTIAL_FIELDS = [
    {"key": "username", "label": "E-Mail (MyRenault / MyDacia)", "type": "text",
     "label_key": "cred.email", "context": "MyRenault / MyDacia"},
    {"key": "password", "label": "Passwort", "type": "password",
     "label_key": "cred.password", "context": "MyRenault / MyDacia"},
    {"key": "locale", "label": "Region", "type": "select", "label_key": "cred.region",
     "optional": True,
     "options": [
         {"value": "de_DE", "label": "Deutschland", "label_key": "set.api_region_de"},
         {"value": "at_AT", "label": "Oesterreich", "label_key": "set.api_region_at"},
         {"value": "fr_FR", "label": "Frankreich", "label_key": "set.api_region_fr"},
         {"value": "it_IT", "label": "Italien", "label_key": "set.api_region_it"},
         {"value": "es_ES", "label": "Spanien", "label_key": "set.api_region_es"},
         {"value": "nl_NL", "label": "Niederlande", "label_key": "set.api_region_nl"},
         {"value": "en_GB", "label": "UK", "label_key": "set.api_region_gb"},
     ]},
]


def _run_async(coro):
    """Run async code from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(coro)


def _t(key, **kw):
    try:
        from services.i18n import t
        return t(key, **kw)
    except Exception:                      # pragma: no cover
        return key


def _is_bad_login(exc) -> bool:
    """Gigya's answer for a wrong e-mail or password.

    Matched by class name as well, so an older renault-api without
    ``InvalidCredentialsException`` still gets the clear message.
    """
    names = {c.__name__ for c in type(exc).__mro__}
    return 'InvalidCredentialsException' in names


def _wants_code(exc) -> bool:
    """Gigya 403101: the account asks for an e-mail code first.

    Renault switches this on and off per account; after one sign-in in a
    browser it usually stops asking, so the message says exactly that.
    """
    names = {c.__name__ for c in type(exc).__mro__}
    return 'PendingTwoFactorAuthenticationException' in names or '403101' in str(exc)


class _RenaultBase(VehicleConnector):
    BRAND = None  # 'renault' or 'dacia'

    async def _fetch(self, force=False):
        # ``or``: the region field is optional, and an empty value is ''
        # rather than missing — renault-api cannot look up a '' locale.
        locale = self.credentials.get('locale') or 'de_DE'
        async with ClientSession() as websession:
            # ``websession=``, not ``session=``: ``session`` is renault-api's
            # own RenaultSession. Handing it the aiohttp session made
            # ``client.session`` the aiohttp object, and every sign-in ended
            # in "'ClientSession' object has no attribute 'login'".
            client = RenaultClient(websession=websession, locale=locale)
            try:
                await client.session.login(self.credentials['username'],
                                           self.credentials['password'])
            except Exception as e:
                if _wants_code(e):
                    raise RuntimeError(_t('renault.err_2fa')) from e
                if _is_bad_login(e):
                    raise RuntimeError(_t('renault.err_login')) from e
                raise
            person = await client.get_person()
            # The models are camelCase (accountId, accountType) — the
            # snake_case names this used to read do not exist.
            accounts = [a for a in (person.accounts or []) if a.accountId]
            wanted = {'renault': 'MYRENAULT', 'dacia': 'MYDACIA'}.get(self.BRAND)
            accounts.sort(key=lambda a: a.accountType != wanted)
            if not accounts:
                raise RuntimeError(_t('renault.err_no_account'))

            # An owner can have the car on another account than the first
            # one (MyRenault next to SFDC, say), so look in each.
            want_vin = (self.credentials.get('vin') or '').strip().upper()
            account = link = None
            for a in accounts:
                acc = await client.get_api_account(a.accountId)
                try:
                    links = (await acc.get_vehicles()).vehicleLinks or []
                except Exception as e:
                    logger.info(f"Renault account {a.accountType}: no vehicles ({e})")
                    continue
                for l in links:
                    if l.vin and (not want_vin or l.vin.upper() == want_vin):
                        account, link = acc, l
                        break
                if link:
                    break
            if not link:
                raise RuntimeError(_t('renault.err_no_vehicle'))
            vin = link.vin
            vehicle = await account.get_api_vehicle(vin)

            battery = await vehicle.get_battery_status()
            cockpit = None
            location = None
            try:
                cockpit = await vehicle.get_cockpit()
            except Exception:
                pass
            try:
                location = await vehicle.get_location()
            except Exception:
                pass

            soc = plausible_percent(battery.batteryLevel) if battery else None
            # chargingStatus is a state code, not a flag: 0.1 waiting for a
            # planned charge, 0.2 charge ended, 0.3 waiting for current,
            # 0.4 flap open — only 1.0 means energy is flowing in.
            is_charging = bool(battery and battery.chargingStatus is not None
                               and battery.chargingStatus >= 1.0)
            is_plugged = bool(battery and battery.plugStatus is not None
                              and battery.plugStatus > 0)
            range_km = battery.batteryAutonomy if battery else None
            odometer = int(cockpit.totalMileage) if cockpit and cockpit.totalMileage else None
            details = link.vehicleDetails
            model = details.model.label if details and details.model else None

            return VehicleStatus(
                soc_percent=soc,
                odometer_km=odometer,
                is_charging=is_charging,
                is_plugged_in=is_plugged,
                estimated_range_km=range_km,
                location_lat=location.gpsLatitude if location else None,
                location_lon=location.gpsLongitude if location else None,
                last_updated=str(battery.timestamp) if battery and battery.timestamp else None,
                vehicle_name=model,
                vehicle_model=model,
                raw_data={'vin': vin},
            )

    def authenticate(self) -> bool:
        try:
            _run_async(self._fetch())
            return True
        except Exception as e:
            logger.error(f"Renault auth failed: {e}")
            return False

    def test_connection(self) -> bool:
        return self.authenticate()

    def verify_credentials(self) -> None:
        # Let the real reason through: "wrong password", "no car on this
        # account" and a library error each need something different from
        # the owner, and the generic "rejected" hid the bug above.
        _run_async(self._fetch())

    def get_status(self, force=False) -> VehicleStatus:
        return _run_async(self._fetch(force))

    @staticmethod
    def credential_fields() -> list:
        return CREDENTIAL_FIELDS


class RenaultConnector(_RenaultBase):
    BRAND = 'renault'

    @staticmethod
    def brand_name() -> str:
        return "Renault (MyRenault)"


class DaciaConnector(_RenaultBase):
    BRAND = 'dacia'

    @staticmethod
    def brand_name() -> str:
        return "Dacia (MyDacia)"


describe('renault', RenaultConnector)
describe('dacia', DaciaConnector)
if HAS_RENAULT:
    register('renault', RenaultConnector)
    register('dacia', DaciaConnector)
