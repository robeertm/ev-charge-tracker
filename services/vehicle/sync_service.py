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
        from models.database import db, AppConfig, VehicleSync
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

        force = AppConfig.get('vehicle_sync_mode', 'cached') == 'force'
        connector = get_connector(brand, creds)
        status = connector.get_status(force=force)

        sync = VehicleSync(
            soc_percent=status.soc_percent,
            odometer_km=status.odometer_km,
            is_charging=status.is_charging,
            charge_power_kw=status.charge_power_kw,
            estimated_range_km=status.estimated_range_km,
            raw_json=json.dumps(status.raw_data),
        )
        db.session.add(sync)
        db.session.commit()

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
