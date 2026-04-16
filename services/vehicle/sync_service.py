"""Background service for periodic vehicle status sync."""
import json
import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_sync_thread = None
_sync_running = False

MIN_INTERVAL_HOURS = 1  # cached/force modes: 1 hour minimum
DEFAULT_INTERVAL_HOURS = 4

# Smart mode defaults: sample every 10 min between 06:00 and 22:00, sleep at night.
DEFAULT_SMART_INTERVAL_MIN = 10
MIN_SMART_INTERVAL_MIN = 5
DEFAULT_SMART_START_HOUR = 6
DEFAULT_SMART_END_HOUR = 22


def log_sync_result(status, mode_label: str, source: str) -> None:
    """Unified one-line summary of a completed vehicle sync.

    mode_label: 'cached' | 'force' | 'smart->cached' | 'smart->force'
    source:     'bg-loop' | 'trips-auto' | 'manual' | 'settings'

    Reads the current daily API counter for the trailing api=N/200 suffix so
    the user can see budget consumption in the /logs feed without guessing.
    """
    try:
        from models.database import AppConfig
        counter = AppConfig.get('vehicle_api_counter', '0')
    except Exception:
        counter = '?'
    has_gps = getattr(status, 'location_lat', None) is not None
    logger.info(
        f"Vehicle sync [{mode_label}, src={source}]: "
        f"SoC={status.soc_percent}%, "
        f"odo={status.odometer_km}km, "
        f"GPS={'yes' if has_gps else 'no'}, "
        f"charging={bool(status.is_charging)}, "
        f"api={counter}/200"
    )


def _in_smart_window(hour: int, start_h: int, end_h: int) -> bool:
    """Active window check, handling the (unlikely) wrap-past-midnight case."""
    if start_h <= end_h:
        return start_h <= hour < end_h
    # e.g. 22..6 = active from 22:00 through 05:59
    return hour >= start_h or hour < end_h


def _compute_sleep_secs(app) -> tuple[int, bool]:
    """Return (sleep_seconds, should_sync_now).

    For smart mode, respects the configurable active window and
    fine-grained interval; outside the window we sleep until it opens
    again and report should_sync_now=False. For cached/force, keeps the
    hourly cadence of previous versions.
    """
    with app.app_context():
        from models.database import AppConfig
        mode = AppConfig.get('vehicle_sync_mode', 'cached')
        if mode == 'smart':
            try:
                start_h = int(AppConfig.get('smart_active_start_hour', str(DEFAULT_SMART_START_HOUR)))
                end_h = int(AppConfig.get('smart_active_end_hour', str(DEFAULT_SMART_END_HOUR)))
                interval_min = int(AppConfig.get('smart_active_interval_min', str(DEFAULT_SMART_INTERVAL_MIN)))
            except (ValueError, TypeError):
                start_h = DEFAULT_SMART_START_HOUR
                end_h = DEFAULT_SMART_END_HOUR
                interval_min = DEFAULT_SMART_INTERVAL_MIN
            interval_min = max(interval_min, MIN_SMART_INTERVAL_MIN)

            now = datetime.now()
            if _in_smart_window(now.hour, start_h, end_h):
                return (interval_min * 60, True)

            # Outside window: sleep until it opens. If start==end (disabled),
            # fall back to cached behavior.
            if start_h == end_h:
                return (interval_min * 60, True)
            target = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
            if start_h <= end_h and now.hour >= end_h:
                target += timedelta(days=1)
            elif start_h > end_h and now.hour >= end_h and now.hour < start_h:
                pass  # target is already today
            secs = int((target - now).total_seconds())
            return (max(secs, 60), False)

        # cached / force: hourly cadence
        try:
            hours = float(AppConfig.get('vehicle_sync_interval_hours', str(DEFAULT_INTERVAL_HOURS)))
        except (ValueError, TypeError):
            hours = DEFAULT_INTERVAL_HOURS
        hours = max(hours, MIN_INTERVAL_HOURS)
        return (int(hours * 3600), True)


def _do_sync(app):
    """Fetch vehicle status and store as VehicleSync row."""
    with app.app_context():
        from models.database import AppConfig
        from services.vehicle import get_connector

        brand = AppConfig.get('vehicle_api_brand', '')
        if not brand:
            return None

        # Rate limit check (200/day for Kia EU)
        from datetime import date
        today_str = date.today().isoformat()
        counter_date = AppConfig.get('vehicle_api_counter_date', '')
        if counter_date != today_str:
            AppConfig.set('vehicle_api_counter_date', today_str)
            AppConfig.set('vehicle_api_counter', '0')
        api_count = int(AppConfig.get('vehicle_api_counter', '0'))
        if api_count >= 190:
            logger.warning(f"Vehicle sync skipped: daily API limit reached ({api_count}/200)")
            return None
        AppConfig.set('vehicle_api_counter', str(api_count + 1))

        creds = {
            'username': AppConfig.get('vehicle_api_username', ''),
            'password': AppConfig.get('vehicle_api_password', ''),
            'pin': AppConfig.get('vehicle_api_pin', ''),
            'region': AppConfig.get('vehicle_api_region', 'EU'),
            'vin': AppConfig.get('vehicle_api_vin', ''),
        }

        # ── Determine effective force flag based on mode ─────
        # 'cached' = always cached (cheap, no GPS most of the time)
        # 'force'  = always force (wakes the car, gets GPS, but burns 12V)
        # 'smart'  = cached by default, but force if:
        #            - last cached sync shows odometer changed (car was moved)
        #              compared to the previous sync row, OR
        #            - the latest VehicleSync row with GPS data is older than
        #              the smart_force_max_hours threshold (default 6h),
        #            …and the car is not currently charging.
        mode = AppConfig.get('vehicle_sync_mode', 'cached')
        force = (mode == 'force')
        mode_label = mode  # 'cached' | 'force' | 'smart' (overridden below)

        if mode == 'smart':
            mode_label = 'smart->cached'
            try:
                from datetime import datetime, timedelta
                from models.database import VehicleSync
                # Find the last sync with GPS to know how stale our location is
                last_with_gps = (VehicleSync.query
                                 .filter(VehicleSync.location_lat.isnot(None))
                                 .order_by(VehicleSync.timestamp.desc())
                                 .first())
                # Find the absolutely-last sync to check charging state
                last_sync = (VehicleSync.query
                             .order_by(VehicleSync.timestamp.desc())
                             .first())
                # Charging skip
                is_charging = bool(last_sync.is_charging) if last_sync else False
                # Threshold for force re-fetch (default 6 hours)
                try:
                    max_hours = float(AppConfig.get('smart_force_max_hours', '6'))
                except (ValueError, TypeError):
                    max_hours = 6.0
                stale = True
                if last_with_gps:
                    age_hours = (datetime.now() - last_with_gps.timestamp).total_seconds() / 3600
                    stale = (age_hours >= max_hours)
                if not is_charging and stale:
                    force = True
                    mode_label = 'smart->force'
                    logger.info(
                        f"Smart sync: forcing live refresh (gps stale, "
                        f"max_hours={max_hours}, charging={is_charging})"
                    )
            except Exception as e:
                logger.warning(f"Smart-mode decision failed, using cached: {e}")
                force = False
                mode_label = 'smart->cached'

        connector = get_connector(brand, creds)
        status = connector.get_status(force=force)

        # Track the timestamp of the last successful force-refresh, so the
        # smart-mode decision logic above has something to compare against.
        if force:
            from datetime import datetime as _dt
            AppConfig.set('last_force_refresh_at', _dt.now().isoformat())

        from app import _save_vehicle_sync, _get_battery_kwh
        sync = _save_vehicle_sync(status, _get_battery_kwh(),
                                  raw_json=json.dumps(status.raw_data, default=str))

        log_sync_result(status, mode_label=mode_label, source='bg-loop')
        return sync


def _sync_loop(app):
    """Background loop that syncs at configured interval.

    Smart mode: fine-grained cadence (every 10min by default) during the
    active window, full stop at night. Cached/force: hourly cadence.
    """
    global _sync_running
    _sync_running = True
    logger.info("Vehicle sync service started")

    while _sync_running:
        sleep_secs, should_sync = _compute_sleep_secs(app)
        if should_sync:
            try:
                _do_sync(app)
            except Exception as e:
                logger.error(f"Vehicle sync error: {e}")
        else:
            logger.info(
                f"Vehicle sync: outside smart-mode active window, "
                f"sleeping {sleep_secs // 60} min"
            )

        # Sleep in small increments so we can stop quickly
        slept = 0
        while slept < sleep_secs and _sync_running:
            time.sleep(min(10, sleep_secs - slept))
            slept += 10

    _sync_running = False
    logger.info("Vehicle sync service stopped")


def start_sync(app):
    """Start periodic vehicle sync in a background thread."""
    global _sync_thread, _sync_running

    if _sync_running:
        logger.info("Vehicle sync already running")
        return False

    with app.app_context():
        from models.database import AppConfig
        brand = AppConfig.get('vehicle_api_brand', '')
        enabled = AppConfig.get('vehicle_sync_enabled', 'false')
        if not brand or enabled != 'true':
            return False

    logger.info("Starting vehicle sync service")
    _sync_thread = threading.Thread(target=_sync_loop, args=(app,), daemon=True)
    _sync_thread.start()
    return True


def stop_sync():
    """Stop the sync thread."""
    global _sync_running
    _sync_running = False


def is_running():
    """Check if sync is currently running."""
    return _sync_running
