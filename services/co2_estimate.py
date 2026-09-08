"""Fallback CO2 estimate for charges the grid data cannot reach.

Why this exists
---------------
ENTSO-E is the only source of real grid intensity, and when the platform
is unreachable — as it was on 2026-09-08 — every lookup comes back empty.
Until now a charge then simply stayed without CO2, and the whole period
was blank in the app.

The energy analyzer solves the same problem by filling the gap with an
estimate that carries its own source label, so a real value overwrites it
on the next successful fetch. This module does the same, with two
deliberate differences:

* **The baseline comes from this install's own real values**, bucketed by
  the hour the charging started. No weather model: with a handful of
  charges a day that would be false precision, and it would add an
  outbound dependency where the point is that the network is unreliable.
* **Nothing is invented.** With no real history there is no estimate —
  the field stays empty, which is the honest answer. A constant would be
  the 441 g/kWh ghost of v3.0.15 all over again.

Every estimate is marked with ``Charge.co2_estimated``. The backfill keeps
those rows in its sights and replaces them the moment ENTSO-E answers.
"""
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: How far back real values are read for the baseline.
BASELINE_DAYS = 120
#: Real values needed in one hour bucket before that bucket is trusted.
MIN_SAMPLES_PER_HOUR = 3
#: Real values needed at all before any estimate is made.
MIN_SAMPLES_TOTAL = 5


def _median(values):
    vals = sorted(values)
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def baseline_from_history(days=BASELINE_DAYS):
    """Return ``(by_hour, overall)`` medians of this install's REAL values.

    Estimated rows are excluded so the baseline can never feed on itself —
    the same rule the energy analyzer applies to its forecast rows.
    """
    from models.database import Charge
    since = date.today() - timedelta(days=days)
    rows = (Charge.query
            .filter(Charge.charge_type != 'PV')
            .filter(Charge.co2_g_per_kwh.isnot(None))
            .filter(Charge.co2_g_per_kwh > 0)
            .filter(Charge.date >= since)
            .all())
    by_hour = {}
    everything = []
    for c in rows:
        if getattr(c, 'co2_estimated', False):
            continue
        val = float(c.co2_g_per_kwh)
        everything.append(val)
        if c.charge_hour is not None:
            by_hour.setdefault(int(c.charge_hour), []).append(val)

    if len(everything) < MIN_SAMPLES_TOTAL:
        return {}, None

    hourly = {h: _median(v) for h, v in by_hour.items()
              if len(v) >= MIN_SAMPLES_PER_HOUR}
    return hourly, _median(everything)


def estimate_for(charge, by_hour, overall):
    """Estimated intensity for one charge, or None when nothing is known."""
    if overall is None:
        return None
    hour = charge.charge_hour
    if hour is not None:
        val = by_hour.get(int(hour))
        if val is not None:
            return int(round(val))
    return int(round(overall))


def fill_estimates(app):
    """Give every grid charge without CO2 a marked estimate.

    Returns how many rows were filled. Rows that already carry an estimate
    are left alone — they are already covered and the backfill will
    replace them with the real value.
    """
    with app.app_context():
        from models.database import db, Charge
        from sqlalchemy import or_

        by_hour, overall = baseline_from_history()
        if overall is None:
            logger.info("CO2 estimate: not enough real history yet — "
                        "leaving the gap empty")
            return 0

        open_rows = (Charge.query
                     .filter(Charge.charge_type != 'PV')
                     .filter(or_(Charge.co2_g_per_kwh.is_(None),
                                 Charge.co2_g_per_kwh == 0))
                     .all())
        filled = 0
        for c in open_rows:
            val = estimate_for(c, by_hour, overall)
            if not val:
                continue
            c.co2_g_per_kwh = val
            c.co2_estimated = True
            if c.kwh_loaded:
                c.co2_kg = round(c.kwh_loaded * val / 1000, 2)
            filled += 1
        if filled:
            db.session.commit()
            logger.info(
                f"CO2 estimate: filled {filled} charge(s) from own history "
                f"(median {int(round(overall))} g/kWh) — marked as estimates"
            )
        return filled
