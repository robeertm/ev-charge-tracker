"""Renault / Dacia connector via renault-api."""
import asyncio
import hashlib
import json
import logging
import os
import time

try:
    from renault_api.renault_client import RenaultClient
    from aiohttp import ClientSession
    HAS_RENAULT = True
except ImportError:
    HAS_RENAULT = False

from .base import VehicleConnector, VehicleStatus, plausible_percent
from .registry import describe, register

logger = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:                          # pragma: no cover
    DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data')

# Renault asks some accounts for an e-mail code (Gigya 403101). Between
# "send me the code" and "here it is" lie two web requests, so the open
# challenge is kept here for a few minutes; afterwards only the device IDs
# stay — with them Gigya remembers this tracker for about 30 days.
STATE_DIR = os.path.join(DATA_DIR, 'renault')
PENDING_TTL = 15 * 60        # a challenge older than this is not completed
RESEND_GAP = 60              # pressing Test twice must not mail twice

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


# Kamereon refuses the car's data while the car does not share it. Renault
# answers with the bare "Access is denied for this resource" (lkcd) and puts
# the real reason, "Privacy mode currently ON", in a second message that
# renault-api drops — so the code alone has to tell the owner.
PRIVACY_CODES = {'err.func.privacy.on', 'err.func.wired.lkcd-authorization.failure'}


def _privacy_on(exc) -> bool:
    names = {c.__name__ for c in type(exc).__mro__}
    return ('PrivacyModeOnException' in names
            or getattr(exc, 'error_code', None) in PRIVACY_CODES)


class RenaultCodeNeeded(RuntimeError):
    """Renault mailed a code; the owner has to type it in.

    ``code_needed`` lets the web layer recognise it without importing this
    module (renault-api is optional).
    """
    code_needed = True

    def __init__(self, message, email_hint=''):
        super().__init__(message)
        self.email_hint = email_hint


def _state_path(username: str) -> str:
    # Hashed: the e-mail address does not belong in a file name.
    key = hashlib.sha256((username or '').strip().lower().encode()).hexdigest()[:32]
    return os.path.join(STATE_DIR, key + '.json')


def _load_state(username: str) -> dict:
    try:
        with open(_state_path(username), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(username: str, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    p = _state_path(username)
    tmp = p + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, p)


def _fresh_pending(state: dict):
    pend = state.get('pending') or {}
    if pend and time.time() - pend.get('ts', 0) < PENDING_TTL:
        return pend
    return None


class _RenaultBase(VehicleConnector):
    BRAND = None  # 'renault' or 'dacia'

    def _client(self, websession):
        # ``or``: the region field is optional, and an empty value is ''
        # rather than missing — renault-api cannot look up a '' locale.
        locale = self.credentials.get('locale') or 'de_DE'
        # ``websession=``, not ``session=``: ``session`` is renault-api's
        # own RenaultSession. Handing it the aiohttp session made
        # ``client.session`` the aiohttp object, and every sign-in ended
        # in "'ClientSession' object has no attribute 'login'".
        return RenaultClient(websession=websession, locale=locale)

    async def _login(self, client, request_code=False):
        user = self.credentials['username']
        state = _load_state(user)
        if state.get('device_ids'):
            client.session.set_device_ids(state['device_ids'])
        try:
            await client.session.login(user, self.credentials['password'])
        except Exception as e:
            if _wants_code(e):
                if not request_code:
                    # Background sync: never mail a code nobody asked for.
                    raise RuntimeError(_t('renault.err_2fa')) from e
                await self._send_code(client.session, state)
            if _is_bad_login(e):
                raise RuntimeError(_t('renault.err_login')) from e
            raise

    async def _send_code(self, session, state):
        """Ask Renault to mail the code, keep the challenge, raise CodeNeeded."""
        user = self.credentials['username']
        pend = _fresh_pending(state)
        if pend and time.time() - pend['ts'] < RESEND_GAP:
            raise RenaultCodeNeeded(_t('renault.code_sent', email=pend.get('email') or '?'),
                                    pend.get('email', ''))
        emails = await session.get_two_factor_auth_emails()
        if not emails:
            raise RuntimeError(_t('renault.err_2fa'))
        await session.request_two_factor_auth_code(emails[0].id)
        tfa = getattr(session, '_pending_tfa', None)
        hint = emails[0].obfuscated or ''
        state['pending'] = {
            'ts': time.time(), 'email': hint,
            'reg_token': tfa.reg_token, 'gigya_assertion': tfa.gigya_assertion,
            'phv_token': tfa.phv_token,
        }
        # The code belongs to THIS device: the same IDs must come back
        # with it, so they are stored now, not after the code.
        state['device_ids'] = session.device_ids
        _save_state(user, state)
        raise RenaultCodeNeeded(_t('renault.code_sent', email=hint or '?'), hint)

    async def _submit_code(self, code):
        from renault_api.renault_session import _PendingTwoFactorAuth
        user = self.credentials['username']
        state = _load_state(user)
        pend = _fresh_pending(state)
        if not pend:
            raise RuntimeError(_t('renault.code_expired'))
        async with ClientSession() as websession:
            client = self._client(websession)
            session = client.session
            session.set_device_ids(state.get('device_ids') or {})
            # renault-api keeps the challenge in memory only; between two
            # web requests it has to be put back from what was stored.
            session._pending_tfa = _PendingTwoFactorAuth(
                login_id=user, password=self.credentials['password'],
                reg_token=pend['reg_token'], gigya_assertion=pend['gigya_assertion'],
                phv_token=pend['phv_token'])
            try:
                await session.complete_two_factor_auth(code.strip(), remember_device=True)
            except Exception as e:
                names = {c.__name__ for c in type(e).__mro__}
                if 'InvalidVerificationCodeException' in names:
                    raise RuntimeError(_t('renault.code_wrong')) from e
                raise
            state.pop('pending', None)
            state['device_ids'] = session.device_ids
            state['confirmed_at'] = time.time()
            _save_state(user, state)

    def submit_code(self, code: str) -> None:
        """Finish the e-mail confirmation with the code the owner typed."""
        _run_async(self._submit_code(code))

    async def _fetch(self, force=False, request_code=False):
        async with ClientSession() as websession:
            client = self._client(websession)
            await self._login(client, request_code=request_code)
            person = await client.get_person()
            # The models are camelCase (accountId, accountType) — the
            # snake_case names this used to read do not exist.
            accounts = [a for a in (person.accounts or []) if a.accountId]
            wanted = {'renault': 'MYRENAULT', 'dacia': 'MYDACIA'}.get(self.BRAND)
            accounts.sort(key=lambda a: a.accountType != wanted)
            if not accounts:
                raise RuntimeError(_t('renault.err_no_account'))

            # An owner can have the car on another account than the first
            # one (MyRenault next to SFDC, say), so look in each. And an
            # account that lists the car may still be refused its data (the
            # car's privacy mode, or an old account after a change of
            # owner): then the SAME car is tried on the next account —
            # never another car, whose values would land in this history.
            want_vin = (self.credentials.get('vin') or '').strip().upper()
            vin = link = vehicle = battery = refused = None
            for a in accounts:
                acc = await client.get_api_account(a.accountId)
                try:
                    links = (await acc.get_vehicles()).vehicleLinks or []
                except Exception as e:
                    logger.info(f"Renault account {a.accountType}: no vehicles ({e})")
                    continue
                want = vin or want_vin
                l = next((l for l in links if l.vin
                          and (not want or l.vin.upper() == want)), None)
                if not l:
                    continue
                vin = l.vin.upper()
                v = await acc.get_api_vehicle(l.vin)
                try:
                    battery = await v.get_battery_status()
                except Exception as e:
                    if not _privacy_on(e):
                        raise
                    logger.info(f"Renault account {a.accountType}: car data refused "
                                f"({getattr(e, 'error_code', e)})")
                    refused = e
                    continue
                link, vehicle = l, v
                break
            if not vin:
                raise RuntimeError(_t('renault.err_no_vehicle'))
            if not link:
                raise RuntimeError(_t('renault.err_privacy')) from refused
            vin = link.vin

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
        # A person pressed Test: this is the one place allowed to have
        # Renault mail a confirmation code.
        _run_async(self._fetch(request_code=True))

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
