"""Clock times of a charge: the minutes inside the stored start/end hours.

A ``Charge`` row keeps whole hours (``charge_hour`` / ``charge_end_hour``) —
the unit the CO2 figure is calculated in — and, since v3.0.200, the minute
inside each of them (``charge_start_min`` / ``charge_end_min``, NULL = :00).
"""
import re
from datetime import datetime

_MATE = re.compile(r'leapmotor-mate:\d+@(\d+)-(\d+)')
_DATA_ACT = re.compile(r'data-act:(\d+)-(\d+)')


def norm_min(v):
    """A minute 0-59 from a form value / number, else None (empty, junk)."""
    try:
        if v is None or str(v).strip() == '':
            return None
        m = int(float(str(v).strip().replace(',', '.')))
    except (TypeError, ValueError):
        return None
    return m if 0 <= m <= 59 else None


def clock(hour, minute):
    """``HH:MM`` for an hour and a possibly empty minute, else None."""
    if hour is None:
        return None
    return '%02d:%02d' % (int(hour), int(minute or 0))


def parts_of(ts):
    """(hour, minute) of an epoch / datetime in local time."""
    d = ts if isinstance(ts, datetime) else datetime.fromtimestamp(int(ts))
    return d.hour, d.minute


def apply_form(charge, form):
    """Set the minute fields of ``charge`` from a posted form.

    A minute without its hour means nothing and is dropped, so a cleared hour
    never leaves a stale minute behind."""
    charge.charge_start_min = (norm_min(form.get('charge_start_min'))
                               if charge.charge_hour is not None else None)
    charge.charge_end_min = (norm_min(form.get('charge_end_min'))
                             if charge.charge_end_hour is not None else None)


def backfill_from_markers(db):
    """Fill the minutes of rows that carry their exact window in the note
    (Leapmotor Mate and data-export charges). Only rows whose both minutes are
    still empty; returns the number of rows changed. Does not commit.

    Plain SQL on the few columns it needs, so it also works on an install
    whose table is still missing columns that later migrations add."""
    from sqlalchemy import text
    n = 0
    rows = db.session.execute(text(
        "SELECT id, date, charge_hour, charge_end_hour, notes FROM charges "
        "WHERE charge_hour IS NOT NULL AND charge_start_min IS NULL "
        "AND charge_end_min IS NULL "
        "AND (notes LIKE '%leapmotor-mate:%' OR notes LIKE '%data-act:%')")).fetchall()
    for rid, day, hour, end_hour, notes in rows:
        m = _MATE.search(notes or '') or _DATA_ACT.search(notes or '')
        if not m:
            continue
        s = datetime.fromtimestamp(int(m.group(1)))
        e = datetime.fromtimestamp(int(m.group(2)))
        # Only when the stored hours really are the marker's hours.
        if s.hour != hour or str(s.date()) != str(day)[:10]:
            continue
        end_min = e.minute if end_hour == e.hour else None
        db.session.execute(text(
            "UPDATE charges SET charge_start_min = :a, charge_end_min = :b WHERE id = :i"),
            {'a': s.minute, 'b': end_min, 'i': rid})
        n += 1
    return n
