"""Thin sync wrapper around the `myskoda` async library.

Why this exists: ``carconnectivity-connector-skoda`` surfaces SoC / odo /
live charging state but does not map the v3 MySkoda endpoints for
position, parking, trips or charging history. The dedicated
``myskoda`` Python lib (used by the Home Assistant integration) does.

This module talks to MySkoda directly and offers a sync API that mirrors
the call shapes our Flask code already expects. Auth uses the same
email/password the existing SkodaConnector already stores; the refresh
token returned by ``MySkoda.connect`` is cached in ``DATA_DIR/myskoda/``
so subsequent calls skip the full OAuth dance.

Import is lazy: the lib is only required on Skoda hosts. On any other
host, ``HAS_MYSKODA`` stays False and the public entry points return
None instead of raising — callers that brand-gate (``brand == 'skoda'``)
won't reach this anyway, but the guard keeps shared code paths safe.
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Optional

from config import DATA_DIR

logger = logging.getLogger(__name__)

try:
    import aiohttp  # noqa: F401  pulled in by myskoda anyway, but we use it directly
    from myskoda import MySkoda
    HAS_MYSKODA = True
except Exception as _imp_err:  # ImportError or missing system deps
    HAS_MYSKODA = False
    logger.debug(f"myskoda lib not available, skoda v3 endpoints disabled: {_imp_err}")

_TOKEN_DIR = os.path.join(DATA_DIR, 'myskoda')
try:
    os.makedirs(_TOKEN_DIR, exist_ok=True)
except OSError:
    pass


def _token_path(email: str) -> str:
    safe = email.replace('@', '_at_').replace('/', '_').replace('\\', '_')
    return os.path.join(_TOKEN_DIR, f'{safe}.refresh_token')


def _load_refresh_token(email: str) -> Optional[str]:
    p = _token_path(email)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            rt = f.read().strip()
        return rt or None
    except OSError:
        return None


def _save_refresh_token(email: str, rt: str) -> None:
    if not rt:
        return
    try:
        p = _token_path(email)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(rt)
        # Mode 600 — token grants full account access.
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        logger.debug(f"myskoda refresh-token persist failed: {e}")


async def _run_call(email: str, password: str, fn):
    """Open one aiohttp session, log in, call ``fn(ms)``, log out."""
    async with aiohttp.ClientSession() as session:
        ms = MySkoda(session, mqtt_enabled=False)
        rt = _load_refresh_token(email)
        used_rt = False
        if rt:
            try:
                await ms.connect(refresh_token=rt)
                used_rt = True
            except Exception as e:
                logger.info(
                    "myskoda refresh-token reuse failed, falling back to password: "
                    f"{type(e).__name__}: {e}"
                )
        if not used_rt:
            await ms.connect(email=email, password=password)
        # Token rotates on every connect; always re-persist.
        try:
            new_rt = await ms.get_refresh_token()
            if new_rt and new_rt != rt:
                _save_refresh_token(email, new_rt)
        except Exception as e:
            logger.debug(f"could not read fresh myskoda refresh-token: {e}")
        try:
            return await fn(ms)
        finally:
            try:
                await ms.disconnect()
            except Exception:
                pass


def _run_sync(coro):
    """Drive an async coroutine to completion in a fresh event loop."""
    try:
        running = asyncio.get_event_loop().is_running()
    except RuntimeError:
        running = False
    if running:
        # The Flask request handler never runs inside an event loop;
        # this is purely defensive against a future background worker
        # that does.
        raise RuntimeError(
            "myskoda_client: refuses to run inside an active event loop")
    return asyncio.run(coro)


class MySkodaSync:
    """Sync facade over the async ``myskoda`` library.

    Construct with the same email + password the existing
    ``SkodaConnector`` uses (``AppConfig`` ``vehicle_api_username`` /
    ``vehicle_api_password`` / ``vehicle_api_vin``). All public methods
    return the native ``myskoda.models.*`` dataclasses or ``None`` on
    failure (callers should brand-gate and try/except).
    """

    def __init__(self, email: str, password: str, vin: Optional[str] = None):
        self.email = email
        self.password = password
        self.vin = vin

    def _require(self) -> bool:
        if not HAS_MYSKODA:
            logger.warning("MySkodaSync called but myskoda lib not installed")
            return False
        if not self.email or not self.password:
            logger.warning("MySkodaSync missing email/password")
            return False
        if not self.vin:
            logger.warning("MySkodaSync missing VIN")
            return False
        return True

    # ── Position ──────────────────────────────────────────────────
    def get_parking_position(self) -> Optional[Any]:
        """``ParkingPositionV3`` with ``parking_position.gps_coordinates``
        and ``formatted_address``. Returns None on any failure."""
        if not self._require():
            return None

        async def fn(ms):
            return await ms.get_parking_position(self.vin)
        try:
            return _run_sync(_run_call(self.email, self.password, fn))
        except Exception as e:
            logger.warning(f"myskoda get_parking_position failed: {type(e).__name__}: {e}")
            return None

    def get_positions(self) -> Optional[Any]:
        """Live ``Positions`` (typically only useful while driving)."""
        if not self._require():
            return None

        async def fn(ms):
            return await ms.get_positions(self.vin)
        try:
            return _run_sync(_run_call(self.email, self.password, fn))
        except Exception as e:
            logger.warning(f"myskoda get_positions failed: {type(e).__name__}: {e}")
            return None

    # ── Trips ─────────────────────────────────────────────────────
    def get_single_trip_statistics(self, start: datetime, end: datetime) -> Optional[Any]:
        """``SingleTrips`` with ``daily_trips: list[DailyTrip]`` covering
        the closed range ``[start, end]``. Both arguments must be naive
        or aware ``datetime`` objects."""
        if not self._require():
            return None

        async def fn(ms):
            return await ms.get_single_trip_statistics(
                self.vin, start=start, end=end)
        try:
            return _run_sync(_run_call(self.email, self.password, fn))
        except Exception as e:
            logger.warning(
                f"myskoda get_single_trip_statistics failed: {type(e).__name__}: {e}")
            return None

    # ── Charging history ─────────────────────────────────────────
    def get_charging_history(self, start=None, end=None, limit: int = 50) -> Optional[Any]:
        """``ChargingHistory`` with ``periods: list[ChargingPeriod]``."""
        if not self._require():
            return None

        async def fn(ms):
            return await ms.get_charging_history(
                self.vin, start=start, end=end, limit=limit)
        try:
            return _run_sync(_run_call(self.email, self.password, fn))
        except Exception as e:
            logger.warning(
                f"myskoda get_charging_history failed: {type(e).__name__}: {e}")
            return None
