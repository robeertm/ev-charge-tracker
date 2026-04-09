"""Background service for periodic vehicle status sync."""
import json
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_sync_thread = None
_sync_running = False

MIN_INTERVAL_HOURS = 1  # 1 hour minimum (Kia EU: 200 calls/day, protect 12V)
DEFAULT_INTERVAL_HOURS = 4


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

        if mode == 'smart':
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
                    logger.info(
                        f"Smart sync: forcing live refresh (gps stale, "
                        f"max_hours={max_hours}, charging={is_charging})"
                    )

                # Also: do a quick cached pre-fetch to detect movement?
                # No — that would double the API call cost. We rely on the
                # next cycle catching new positions instead.
            except Exception as e:
                logger.warning(f"Smart-mode decision failed, using cached: {e}")
                force = False

        connector = get_connector(brand, creds)
        status = connector.get_status(force=force)

        # Track the timestamp of the last successful force-refresh, so the
        # smart-mode decision logic above has something to compare against.
        if force:
            from datetime import datetime as _dt
            AppConfig.set('last_force_refresh_at', _dt.now().isoformat())

        from app import _save_vehicle_sync, _get_battery_kwh
        sync = _save_vehicle_sync(status, _get_battery_kwh(),
                                  raw_json=json.dumps(status.raw_data))

        logger.info(
            f"Vehicle sync: SoC={status.soc_percent}%, "
            f"odo={status.odometer_km}km, charging={status.is_charging}"
        )
        return sync


def _sync_loop(app):
    """Background loop that syncs at configured interval."""
    global _sync_running
    _sync_running = True
    logger.info("Vehicle sync service started")

    while _sync_running:
        try:
            _do_sync(app)
        except Exception as e:
            logger.error(f"Vehicle sync error: {e}")

        # Read interval from config each cycle (allows live changes)
        with app.app_context():
            from models.database import AppConfig
            try:
                interval = float(AppConfig.get('vehicle_sync_interval_hours', str(DEFAULT_INTERVAL_HOURS)))
            except (ValueError, TypeError):
                interval = DEFAULT_INTERVAL_HOURS
            interval = max(interval, MIN_INTERVAL_HOURS)

        sleep_secs = interval * 3600
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
