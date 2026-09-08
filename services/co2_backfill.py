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
# Wall-clock of the last kick, so the per-sync kick (v3.0.114) cannot
# spin up a run on every manual refresh. Boot and the explicit buttons
# pass force=True and ignore it.
_last_kick_ts = 0.0

RETRY_INTERVAL = 60  # seconds between retries after rate limit
BATCH_DELAY = 2  # seconds between successful API calls
# Give up polling ENTSO-E for a charge after this many failed lookups.
# Grid data for any real historical date is available within a couple of
# days, so a genuinely unfillable date is rare; this only stops us from
# polling such a date forever. Recent charges get plenty of retries
# (once per backfill run, i.e. per boot / manual trigger) to catch the
# publish delay.
CO2_MAX_ATTEMPTS = 12
# Minimum wall-clock distance between two unforced kicks.
MIN_KICK_INTERVAL_S = 1800


def missing_co2_filter(Charge):
    """SQLAlchemy predicate for "grid charge still without a real CO2 value".

    NULL = never fetched; 0 = legacy poison marker from a failed lookup;
    v3.0.116 adds rows carrying a fallback estimate — they hold a number
    for the user, but the real one is still owed, so the backfill must
    keep them and overwrite them once ENTSO-E answers.
    PV charges get their CO2 from the lifecycle estimate, never ENTSO-E,
    so they are excluded.
    """
    return and_(
        or_(Charge.co2_g_per_kwh.is_(None),
            Charge.co2_g_per_kwh == 0,
            Charge.co2_estimated.is_(True)),
        Charge.charge_type != 'PV',
    )


def get_missing_count(app):
    """Count grid charges without CO2 data (NULL or poisoned 0)."""
    with app.app_context():
        from models.database import Charge
        return Charge.query.filter(missing_co2_filter(Charge)).count()


def reset_attempts_for_missing(app):
    """Give every still-missing grid charge its retry budget back.

    The attempts cap exists to stop polling a date ENTSO-E will never
    have. It was never meant to write off a charge that simply arrived
    while the backfill had no way of running again (before v3.0.114 the
    thread only started at boot or on a button). Returns how many rows
    were unfrozen.
    """
    with app.app_context():
        from models.database import db, Charge
        stuck = (Charge.query
                 .filter(missing_co2_filter(Charge))
                 .filter(Charge.co2_attempts.isnot(None))
                 .filter(Charge.co2_attempts > 0)
                 .all())
        for c in stuck:
            c.co2_attempts = 0
        if stuck:
            db.session.commit()
        return len(stuck)


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
    unreachable = False

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
                    charge.co2_estimated = False    # the real number wins
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
                    # v3.0.115: tell an outage apart from an honest
                    # "no data". While the platform is unreachable every
                    # lookup returns None, so a multi-day outage used to
                    # spend every charge's retry budget and write the
                    # whole period off — the charges would stay empty
                    # even after the platform came back. Stop the run
                    # instead; the next sync picks it up again.
                    from services import entsoe_service as _entsoe
                    if _entsoe.last_call_failed():
                        logger.warning(
                            "CO2 backfill: ENTSO-E unreachable — stopping "
                            "this run, no attempt counted"
                        )
                        unreachable = True
                        break

                    # v3.0.114: a charge from TODAY is not a failed
                    # lookup — ENTSO-E has simply not published that part
                    # of the day yet. Counting it would spend the whole
                    # retry budget within hours now that every vehicle
                    # sync kicks this thread, and the charge would be
                    # written off before the data ever appeared. Skip it
                    # for this run without a strike; tomorrow it counts.
                    if charge.date < datetime.now().date():
                        charge.co2_attempts = (charge.co2_attempts or 0) + 1
                        db.session.commit()
                        logger.warning(
                            f"CO2 backfill: no data for {charge.date} "
                            f"(attempt {charge.co2_attempts}/{CO2_MAX_ATTEMPTS})"
                        )
                    else:
                        logger.info(
                            f"CO2 backfill: {charge.date} not published yet — "
                            f"retrying after the next sync"
                        )
                    skip_ids.add(charge.id)
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

    # v3.0.116: the platform is down, so nothing can be looked up right
    # now — fill the gap with a marked estimate instead of leaving the
    # user with empty CO2 for the whole outage. Every one of those rows
    # stays on the backfill's list and is replaced by the real number as
    # soon as ENTSO-E answers again.
    if unreachable:
        try:
            from services.co2_estimate import fill_estimates
            n = fill_estimates(app)
            if n:
                logger.info(
                    f"CO2 backfill: platform unreachable — {n} charge(s) "
                    f"filled with a marked estimate for now"
                )
        except Exception as e:
            logger.warning(f"CO2 estimate fallback failed: {e}")

    _backfill_running = False
    logger.info("CO2 backfill thread finished")


def start_backfill(app, force=False, min_interval_s=MIN_KICK_INTERVAL_S):
    """Start backfill in a background thread if not already running.

    ``force=True`` is for the deliberate kicks — boot, the settings
    button, a charge type change. Everything else (the per-sync kick
    added in v3.0.114) is rate limited to one run per
    ``min_interval_s``, so a user hammering "sync now" cannot burn the
    per-charge retry budget.
    """
    global _backfill_thread, _backfill_running, _last_kick_ts

    if _backfill_running:
        logger.info("CO2 backfill already running")
        return False

    if not force:
        since = time.time() - _last_kick_ts
        if _last_kick_ts and since < min_interval_s:
            return False

    missing = get_missing_count(app)
    if missing == 0:
        return False

    logger.info(f"Starting CO2 backfill for {missing} entries")
    # Claim the slot synchronously *before* spawning the thread. The
    # thread target sets this again at its top, but there's a window
    # between start() and the target actually running; without this a
    # second start_backfill() call in the same tick (e.g. the boot
    # self-heal firing right after the v3.0.65 cleanup already kicked)
    # would spawn a duplicate thread and double-commit.
    _backfill_running = True
    _last_kick_ts = time.time()
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
