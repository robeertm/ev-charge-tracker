"""Background service to backfill missing CO2 data from ENTSO-E.

v3.0.92: a charge counts as "missing CO2" when co2_g_per_kwh is NULL
*or* 0. The old code poisoned a row to 0 after a single failed lookup
to mark it "attempted", which froze a physically-impossible 0 g/kWh
onto grid charges (grid mix is never exactly 0 — even a near-100%
renewable hour carries some fossil) and, worse, meant the row was
never re-fetched once ENTSO-E finally published its data a day or two
later. That left whole days without CO2. We now bound retries with the
``co2_attempts`` column instead, and always look the value up *from the
charge's own date and time* (window → start-hour → daily average).
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

logger = logging.getLogger(__name__)

_backfill_thread = None
_backfill_running = False

RETRY_INTERVAL = 60  # seconds between retries after rate limit
BATCH_DELAY = 2  # seconds between successful API calls
# Give up polling ENTSO-E for a charge after this many failed lookups.
# Grid data for any real historical date is available within a couple of
# days, so a genuinely unfillable date is rare; this only stops us from
# polling such a date forever. Recent charges get plenty of retries
# (once per backfill run, i.e. per boot / manual trigger) to catch the
# publish delay.
CO2_MAX_ATTEMPTS = 12


def missing_co2_filter(Charge):
    """SQLAlchemy predicate for "grid charge still without CO2".

    NULL = never fetched; 0 = legacy poison marker from a failed lookup.
    PV charges get their CO2 from the lifecycle estimate, never ENTSO-E,
    so they are excluded.
    """
    return and_(
        or_(Charge.co2_g_per_kwh.is_(None), Charge.co2_g_per_kwh == 0),
        Charge.charge_type != 'PV',
    )


def get_missing_count(app):
    """Count grid charges without CO2 data (NULL or poisoned 0)."""
    with app.app_context():
        from models.database import Charge
        return Charge.query.filter(missing_co2_filter(Charge)).count()


def _lookup_co2(api_key, charge):
    """Look up CO2 intensity for a charge *from its own date and time*.

    Escalating fallback so a missing bucket for the exact hour still
    yields a sensible value from the same day's grid mix:
      1. time-weighted window (when start != end hour, both known)
      2. the charging start-hour snapshot
      3. the daily average
    Every step keys off ``charge.date`` and ``charge.charge_hour`` — the
    Ladeuhrzeit — so the number reflects the grid the EV actually drew
    from. Returns None only when ENTSO-E has no data for that day at all
    (typical for a charge created before ENTSO-E published the day).
    """
    from services.entsoe_service import (
        get_co2_intensity, get_co2_intensity_window,
    )
    base_dt = datetime.combine(charge.date, datetime.min.time())

    co2 = None
    if (charge.charge_hour is not None
            and charge.charge_end_hour is not None
            and charge.charge_end_hour != charge.charge_hour):
        start = base_dt.replace(hour=charge.charge_hour)
        end_off = 1 if charge.charge_end_hour < charge.charge_hour else 0
        end = (base_dt.replace(hour=charge.charge_end_hour)
               + timedelta(days=end_off)
               + timedelta(hours=1))  # include end-hour bucket
        co2 = get_co2_intensity_window(api_key, start, end)

    if co2 is None and charge.charge_hour is not None:
        co2 = get_co2_intensity(api_key, base_dt, hour=charge.charge_hour)

    if co2 is None:
        # Last resort: the whole day's average grid intensity.
        co2 = get_co2_intensity(api_key, base_dt, hour=None)

    return co2


def backfill_co2(app):
    """Backfill missing CO2 values from ENTSO-E. Runs in background thread."""
    global _backfill_running
    _backfill_running = True
    logger.info("CO2 backfill started")

    # IDs that returned no data this run — skipped so the loop makes
    # progress instead of re-selecting the same NULL row forever. Their
    # co2_attempts counter is bumped so a genuinely unfillable date is
    # eventually dropped across runs (see CO2_MAX_ATTEMPTS).
    skip_ids = set()

    while _backfill_running:
        with app.app_context():
            from models.database import db, Charge, AppConfig
            from config import Config

            api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
            if not api_key:
                logger.info("CO2 backfill: no API key, stopping")
                break

            q = Charge.query.filter(missing_co2_filter(Charge)).filter(
                or_(Charge.co2_attempts.is_(None),
                    Charge.co2_attempts < CO2_MAX_ATTEMPTS)
            )
            if skip_ids:
                q = q.filter(~Charge.id.in_(skip_ids))
            charge = q.order_by(Charge.date).first()

            if not charge:
                logger.info("CO2 backfill complete — no more missing values")
                break

            try:
                co2 = _lookup_co2(api_key, charge)

                if co2:
                    charge.co2_g_per_kwh = co2
                    charge.co2_attempts = 0
                    if charge.kwh_loaded:
                        charge.co2_kg = round(charge.kwh_loaded * co2 / 1000, 2)
                    db.session.commit()
                    logger.info(f"CO2 backfill: {charge.date} → {co2} g/kWh")
                    time.sleep(BATCH_DELAY)
                else:
                    # No data yet for this date — count the attempt and
                    # skip it this run (do NOT freeze a fake 0). Recent
                    # charges retry on the next run once ENTSO-E catches
                    # up; a date that never fills is dropped after
                    # CO2_MAX_ATTEMPTS.
                    charge.co2_attempts = (charge.co2_attempts or 0) + 1
                    db.session.commit()
                    skip_ids.add(charge.id)
                    logger.warning(
                        f"CO2 backfill: no data for {charge.date} "
                        f"(attempt {charge.co2_attempts}/{CO2_MAX_ATTEMPTS})"
                    )
                    time.sleep(BATCH_DELAY)

            except Exception as e:
                error_msg = str(e).lower()
                if 'rate' in error_msg or '429' in error_msg or 'too many' in error_msg:
                    logger.warning(f"CO2 backfill: rate limited, waiting {RETRY_INTERVAL}s")
                    time.sleep(RETRY_INTERVAL)
                else:
                    logger.error(f"CO2 backfill error for {charge.date}: {e}")
                    # Don't spin on a persistently-erroring row.
                    skip_ids.add(charge.id)
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
