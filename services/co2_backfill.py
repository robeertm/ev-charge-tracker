"""Background service to backfill missing CO2 data from ENTSO-E."""
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_backfill_thread = None
_backfill_running = False

RETRY_INTERVAL = 60  # seconds between retries after rate limit
BATCH_DELAY = 2  # seconds between successful API calls


def get_missing_count(app):
    """Count charges without CO2 data."""
    with app.app_context():
        from models.database import Charge
        return Charge.query.filter(
            Charge.co2_g_per_kwh.is_(None),
            Charge.charge_type != 'PV',
        ).count()


def backfill_co2(app):
    """Backfill missing CO2 values from ENTSO-E. Runs in background thread."""
    global _backfill_running
    _backfill_running = True
    logger.info("CO2 backfill started")

    while _backfill_running:
        with app.app_context():
            from models.database import db, Charge, AppConfig
            from config import Config

            api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
            if not api_key:
                logger.info("CO2 backfill: no API key, stopping")
                break

            # Get next charge without CO2
            charge = Charge.query.filter(
                Charge.co2_g_per_kwh.is_(None),
                Charge.charge_type != 'PV',
            ).order_by(Charge.date).first()

            if not charge:
                logger.info("CO2 backfill complete — no more missing values")
                break

            try:
                from services.entsoe_service import get_co2_intensity
                co2 = get_co2_intensity(
                    api_key,
                    datetime.combine(charge.date, datetime.min.time()),
                    hour=charge.charge_hour,
                )

                if co2:
                    charge.co2_g_per_kwh = co2
                    if charge.kwh_loaded:
                        charge.co2_kg = round(charge.kwh_loaded * co2 / 1000, 2)
                    db.session.commit()
                    logger.info(f"CO2 backfill: {charge.date} → {co2} g/kWh")
                    time.sleep(BATCH_DELAY)
                else:
                    # No data available for this date, skip it
                    logger.warning(f"CO2 backfill: no data for {charge.date}, skipping")
                    charge.co2_g_per_kwh = 0  # mark as attempted
                    db.session.commit()
                    time.sleep(BATCH_DELAY)

            except Exception as e:
                error_msg = str(e).lower()
                if 'rate' in error_msg or '429' in error_msg or 'too many' in error_msg:
                    logger.warning(f"CO2 backfill: rate limited, waiting {RETRY_INTERVAL}s")
                    time.sleep(RETRY_INTERVAL)
                else:
                    logger.error(f"CO2 backfill error for {charge.date}: {e}")
                    time.sleep(RETRY_INTERVAL)

    _backfill_running = False
    logger.info("CO2 backfill thread finished")


def start_backfill(app):
    """Start backfill in a background thread if not already running."""
    global _backfill_thread, _backfill_running

    if _backfill_running:
        logger.info("CO2 backfill already running")
        return False

    missing = get_missing_count(app)
    if missing == 0:
        return False

    logger.info(f"Starting CO2 backfill for {missing} entries")
    _backfill_thread = threading.Thread(target=backfill_co2, args=(app,), daemon=True)
    _backfill_thread.start()
    return True


def stop_backfill():
    """Stop the backfill thread."""
    global _backfill_running
    _backfill_running = False


def is_running():
    """Check if backfill is currently running."""
    return _backfill_running
