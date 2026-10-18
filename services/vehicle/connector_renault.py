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
    # v3.0.171: without a VIN the tracker looks for the car that answers;
    # with one, it asks only for that car.
    {"key": "vin", "label": "FIN / VIN (optional, bei mehreren Fahrzeugen)", "type": "text",
     "label_key": "cred.vin", "optional": True, "help_key": "cred.help_vin_multi"},
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


# Only this code means the car's privacy mode for sure (renault-api maps it to
# PrivacyModeOnException).
PRIVACY_CODES = {'err.func.privacy.on'}
# A refusal WITHOUT a stated reason. v3.0.165 read lkcd as "privacy mode"
# from one foreign log; an owner with privacy mode off got it too (issue #4),
# and renault-api documents no meaning. So: say it is refused, not why.
DENIED_CODES = {'err.func.wired.lkcd-authorization.failure', 'err.func.403'}


def _privacy_on(exc) -> bool:
    names = {c.__name__ for c in type(exc).__mro__}
    return ('PrivacyModeOnException' in names
            or getattr(exc, 'error_code', None) in PRIVACY_CODES)


def _data_denied(exc) -> bool:
    """Renault refused this request. Matched by code only — a server error
    must never pass for a refusal."""
    names = {c.__name__ for c in type(exc).__mro__}
    return (_privacy_on(exc) or 'AccessDeniedException' in names
            or getattr(exc, 'error_code', None) in DENIED_CODES)


def _mask_vin(vin) -> str:
    vin = str(vin or '')
    return vin[:3] + '…' + vin[-4:] if len(vin) > 7 else '…'


def _label(link) -> str:
    d = getattr(link, 'vehicleDetails', None)
    m = getattr(d, 'model', None) if d else None
    return (getattr(m, 'label', None) or '').strip()


def _car(vin, label) -> str:
    return ('%s (%s)' % (label, _mask_vin(vin))) if label else _mask_vin(vin)


def _log_denied(what, exc, account_type, asked, account_country, vin):
    """Every refusal, not only the battery one: the owner's log is the only
    place that says WHICH request Renault refused, for which car."""
    logger.info(f"Renault {what} refused on {account_type}: "
                f"{getattr(exc, 'error_code', type(exc).__name__)}: {getattr(exc, 'error_details', exc)}; "
                f"asked as {asked}, account country {account_country}, car {_mask_vin(vin)}")


def _country(locale) -> str:
    return (locale or '').split('_')[-1].upper()


class _Refused(Exception):
    """Renault gave nothing at all for the car; carries what a retry needs."""

    def __init__(self, key, cause, asked, account_country, **kw):
        super().__init__(key)
        self.key, self.cause, self.kw = key, cause, kw
        self.asked, self.account_country = asked, account_country


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

    def _locale(self):
        # ``or``: the region field is optional, and an empty value is ''
        # rather than missing — renault-api cannot look up a '' locale.
        return self.credentials.get('locale') or 'de_DE'

    def _client(self, websession, country=None):
        # ``websession=``, not ``session=``: ``session`` is renault-api's
        # own RenaultSession. Handing it the aiohttp session made
        # ``client.session`` the aiohttp object, and every sign-in ended
        # in "'ClientSession' object has no attribute 'login'".
        # ``country`` goes into every Kamereon request; by default it
        # follows the region. A remembered account country overrides it.
        kw = {'country': country} if country else {}
        return RenaultClient(websession=websession, locale=self._locale(), **kw)

    @staticmethod
    def _vehicle_for(acc, link):
        """The car with the details we already hold. Without them renault-api
        GETs ``…/vehicles/{vin}/details`` only to pick the endpoint — one more
        request Renault can refuse, and its refusal looks like a refusal of
        the data itself (Home Assistant never sends it)."""
        if not getattr(link, 'vehicleDetails', None):
            return None
        try:
            from renault_api.renault_vehicle import RenaultVehicle
            return RenaultVehicle(account_id=acc.account_id, vin=link.vin,
                                  session=acc.session, vehicle_details=link.vehicleDetails)
        except Exception:                  # another renault-api: the old way
            return None

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
        user = self.credentials['username']
        merk = _load_state(user).get('country')
        try:
            return await self._fetch_once(request_code, merk)
        except _Refused as r:
            # Renault refused the data, and the account belongs to another
            # country than the one we asked with (an empty region means
            # Germany): ask once more with the account's country, and
            # remember it when that works.
            if r.account_country and r.account_country != r.asked:
                logger.info(f"Renault: refused with country {r.asked}, account is "
                            f"{r.account_country} — retrying with {r.account_country}")
                try:
                    st = await self._fetch_once(request_code, r.account_country)
                except _Refused as r2:
                    raise RuntimeError(_t(r2.key, **r2.kw)) from r2.cause
                state = _load_state(user)
                state['country'] = r.account_country
                _save_state(user, state)
                return st
            raise RuntimeError(_t(r.key, **r.kw)) from r.cause

    async def _fetch_once(self, request_code, country):
        async with ClientSession() as websession:
            client = self._client(websession, country)
            asked = (country or _country(self._locale())).upper()
            await self._login(client, request_code=request_code)
            person = await client.get_person()
            # The models are camelCase (accountId, accountType) — the
            # snake_case names this used to read do not exist.
            accounts = [a for a in (person.accounts or []) if a.accountId]
            wanted = {'renault': 'MYRENAULT', 'dacia': 'MYDACIA'}.get(self.BRAND)
            accounts.sort(key=lambda a: a.accountType != wanted)
            if not accounts:
                raise RuntimeError(_t('renault.err_no_account'))

            # Which car? (v3.0.171, issue #4.) A VIN entered by the owner, or
            # the car that delivered data before, IS the car — never another
            # one, whose values would land in this history. Only without
            # either the tracker looks for it, the way Home Assistant does:
            # every car on every account, cars Renault sends no details for
            # skipped (sold or half-linked ones). Until now it took the FIRST
            # car Renault listed — an owner whose list starts with such a car
            # got "access denied" for everything, while Home Assistant, which
            # goes through all cars, showed his Mégane.
            user = self.credentials['username']
            want_vin = (self.credentials.get('vin') or '').strip().upper()
            memo = {} if want_vin else _load_state(user)
            pin = want_vin or (memo.get('vin') or '').upper()
            cars = {}                   # vin -> [(account type, account, vehicle list, link)], in order
            offered = {}                # vin -> label, every car seen
            acc_country = None
            for a in accounts:
                acc = await client.get_api_account(a.accountId)
                try:
                    vs = await acc.get_vehicles()
                    links = [x for x in (vs.vehicleLinks or []) if x.vin]
                except Exception as e:
                    logger.info(f"Renault account {a.accountType}: no vehicles ({e})")
                    continue
                logger.info(f"Renault account {a.accountType}: {len(links)} car(s)"
                            + (': ' + ', '.join(_car(x.vin, _label(x)) for x in links) if links else ''))
                acc_country = acc_country or (getattr(vs, 'country', None) or '').upper() or None
                for l in links:
                    v_up = l.vin.upper()
                    offered.setdefault(v_up, _label(l))
                    if pin and v_up != pin:
                        continue
                    if not pin and not getattr(l, 'vehicleDetails', None):
                        # Like Home Assistant: ask for the details once; a car
                        # without any is not one we can read.
                        try:
                            l.vehicleDetails = await (await acc.get_api_vehicle(l.vin)).get_details()
                        except Exception:
                            pass
                        if not getattr(l, 'vehicleDetails', None):
                            logger.info(f"Renault account {a.accountType}: skipping {_mask_vin(l.vin)}"
                                        f" — Renault sends no details for it")
                            continue
                    cars.setdefault(v_up, []).append((a.accountType, acc, vs, l))
                    offered[v_up] = offered[v_up] or _label(l)
            if not cars:
                if pin and not want_vin:
                    # The remembered car is gone from the account: forget it
                    # and look again.
                    memo.pop('vin', None)
                    _save_state(user, memo)
                    return await self._fetch_once(request_code, country)
                raise RuntimeError(_t('renault.err_no_vehicle'))
            if not pin:
                logger.info("Renault: no VIN entered — trying the cars in the order Renault lists them")

            vin = link = vehicle = battery = cockpit = location = None
            partial = False
            refused = None              # first refusal: (error, account country)
            privacy_sure = False
            for v_up, entries in cars.items():
                first_refused = None    # (vehicle, link, account type, vehicle list)
                for typ, acc, vs, l in entries:
                    v = self._vehicle_for(acc, l) or await acc.get_api_vehicle(l.vin)
                    try:
                        battery = await v.get_battery_status()
                    except Exception as e:
                        if not _data_denied(e):
                            raise
                        privacy_sure = privacy_sure or _privacy_on(e)
                        _log_denied('battery-status', e, typ, asked, getattr(vs, 'country', None), l.vin)
                        refused = refused or (e, getattr(vs, 'country', None))
                        first_refused = first_refused or (v, l, typ, vs)
                        continue
                    vehicle, link = v, l
                    break
                if not link and first_refused:
                    # No account handed out this car's battery. Like Home
                    # Assistant, keep what Renault does give (odometer,
                    # position) — from the account that listed it first.
                    v, l, typ, vs = first_refused
                    for what, call in (('cockpit', v.get_cockpit), ('location', v.get_location)):
                        try:
                            got = await call()
                        except Exception as e:
                            _log_denied(what, e, typ, asked, getattr(vs, 'country', None), l.vin)
                            continue
                        if what == 'cockpit':
                            cockpit = got
                        else:
                            location = got
                    if cockpit is not None or location is not None:
                        vehicle, link, partial = v, l, True
                if link:
                    vin = link.vin
                    break
                # Nothing at all for this car: with a VIN that is the answer;
                # without one, the next car may be the owner's.
                logger.info(f"Renault: nothing at all for {_car(v_up, offered.get(v_up))}"
                            + ("" if pin else " — trying the next car"))
            if not link:
                if privacy_sure:
                    raise _Refused('renault.err_privacy', refused[0], asked, acc_country)
                if not pin and len(cars) > 1:
                    # every car on the account tried, none answered
                    raise _Refused('renault.err_data_denied_all_cars', refused[0], asked, acc_country,
                                   n=len(cars), cars=', '.join(_car(v_, offered.get(v_)) for v_ in cars))
                raise _Refused('renault.err_data_denied', refused[0], asked, acc_country)

            if not partial:
                # Battery came; odometer and position are extras.
                try:
                    cockpit = await vehicle.get_cockpit()
                except Exception as e:
                    _log_denied('cockpit', e, '-', asked, acc_country, vin)
                try:
                    location = await vehicle.get_location()
                except Exception as e:
                    _log_denied('location', e, '-', asked, acc_country, vin)
            if not want_vin and (memo.get('vin') or '').upper() != vin.upper():
                # Found it: from now on this is THE car.
                memo = _load_state(user)
                memo['vin'] = vin.upper()
                _save_state(user, memo)
                logger.info(f"Renault: using {_car(vin, offered.get(vin.upper()))} from now on")

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
                raw_data={'vin': vin, 'picked': not want_vin and len(offered) > 1,
                          'car': _car(vin, offered.get(vin.upper())),
                          'others': [_car(v_, offered[v_]) for v_ in offered if v_ != vin.upper()]},
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

    def verify_credentials(self):
        # Let the real reason through: "wrong password", "no car on this
        # account" and a library error each need something different from
        # the owner, and the generic "rejected" hid the bug above.
        # A person pressed Test: this is the one place allowed to have
        # Renault mail a confirmation code.
        st = _run_async(self._fetch(request_code=True))
        # Connected, but Renault kept the battery data back: say so next to
        # "connected" instead of letting it look complete.
        notes = []
        if st is not None and st.soc_percent is None:
            notes.append(_t('renault.partial_no_soc'))
        raw = (st.raw_data or {}) if st is not None else {}
        if raw.get('picked'):
            # Several cars, no VIN: say which one answered.
            notes.append(_t('renault.picked_car', car=raw.get('car'), others=', '.join(raw.get('others') or [])))
        return ' '.join(notes) or None

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
