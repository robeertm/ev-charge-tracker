# -*- coding: utf-8 -*-
"""One answer to "is a charge running right now?" — for every caller.

An update stops the sync loop, and the loop is what notices a charge
ending. Miss that transition and the charge is filed wrong or not at all,
so both ways of updating have to ask first: swapping the app's files, and
recreating its container.

**Why this is a module and not two queries.** The question used to be
written out inline in ``/api/update/install``. The moment an automatic
updater needs the same answer, a second copy appears — and in this project
a rule that lived in four places had two of those copies disagreeing. One
definition, two ways in:

* **With an application context** — the endpoint's path. Uses the ORM, so
  it sees the session it is already in.
* **Without one** — the Watchtower ``pre-update`` hook, which runs as a
  bare ``docker exec`` next to the app. It reads the database file
  read-only. 🔴 It must NOT build the application to ask: ``create_app()``
  starts the sync loop, the wallbox poll and the geocode loop, and a
  throwaway process that does that on a live install is a measured
  mistake, not a theory.

**Fail closed, but only where the evidence is actually unclear** — the
same split ``update_hold`` makes, for the same reason:

===========================  =====================  ===================
Evidence                     Answer                 Why
===========================  =====================  ===================
No database, no rows         not charging           A fresh install has
                                                    no car yet. Failing
                                                    closed here would
                                                    block every update
                                                    forever on day one.
Newest row says charging     **charging**           The obvious case.
Newest row too old to count  not charging           See the bound below.
Database unreadable          **charging**           Unreadable evidence
                                                    keeps the gate that
                                                    protects shut.
===========================  =====================  ===================

**The age bound exists because a charge end does not write a row.**
``VehicleSync`` rows are written only when a tracked field differs, and
``is_charging`` is deliberately not one of those fields — so the newest
row can keep saying "charging" long after the cable came out. For a
button that is an annoyance with a "force" option next to it. For an
automatic updater it would be a silent, permanent block.

Three hours is measured, not picked: across 294 charging rows of a real
install the longest gap to the next row was **1.95 h**, and none exceeded
three hours. While a car really is charging its SoC moves, so rows keep
arriving; three hours of silence means the charge ended or the loop died —
and if the loop died, updating is the repair.
"""
import os
import sqlite3
from datetime import datetime, timedelta

#: A charging row older than this cannot claim a charge is still running.
CHARGE_ROW_MAX_AGE_H = 3.0


def _db_path() -> str:
    from config import Config
    uri = getattr(Config, 'SQLALCHEMY_DATABASE_URI', '') or ''
    if uri.startswith('sqlite:///'):
        return uri[len('sqlite:///'):]
    return ''


def _newest_row_orm():
    from models.database import VehicleSync
    row = (VehicleSync.query
           .order_by(VehicleSync.timestamp.desc())
           .first())
    if row is None:
        return None
    return bool(row.is_charging), row.timestamp


def _newest_row_file():
    """Read the newest sync straight from the file, read-only.

    A missing file is "no data", not "unreadable": that is a fresh
    install, and the caller must be allowed to proceed.
    """
    pfad = _db_path()
    if not pfad or not os.path.isfile(pfad):
        return None
    con = sqlite3.connect('file:%s?mode=ro' % pfad, uri=True)
    try:
        zeile = con.execute(
            'SELECT is_charging, timestamp FROM vehicle_syncs'
            ' ORDER BY timestamp DESC LIMIT 1').fetchone()
    finally:
        con.close()
    if zeile is None:
        return None
    laedt, stempel = bool(zeile[0]), zeile[1]
    if isinstance(stempel, str):
        text = stempel.strip()
        for form in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                     '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
            try:
                stempel = datetime.strptime(text, form)
                break
            except ValueError:
                continue
        else:
            # A timestamp we cannot read is unclear evidence, not absence.
            raise ValueError('unlesbarer Zeitstempel: %r' % text[:40])
    return laedt, stempel


def charge_state(now: datetime | None = None) -> dict:
    """``{'charging': bool, 'row_at': datetime|None, 'reason': str}``.

    ``reason`` is for the log and the API message, never for a decision —
    callers branch on ``charging`` alone.
    """
    jetzt = now or datetime.now()
    try:
        from flask import has_app_context
        im_kontext = has_app_context()
    except Exception:
        im_kontext = False
    try:
        neueste = _newest_row_orm() if im_kontext else _newest_row_file()
    except Exception as e:
        return {'charging': True, 'row_at': None,
                'reason': 'unreadable: %s' % e}
    if neueste is None:
        return {'charging': False, 'row_at': None, 'reason': 'no data'}
    laedt, stempel = neueste
    if not laedt:
        return {'charging': False, 'row_at': stempel, 'reason': 'idle'}
    if stempel is not None and jetzt - stempel > timedelta(hours=CHARGE_ROW_MAX_AGE_H):
        return {'charging': False, 'row_at': stempel,
                'reason': 'stale (older than %.0f h)' % CHARGE_ROW_MAX_AGE_H}
    return {'charging': True, 'row_at': stempel, 'reason': 'charging'}


def charge_in_progress(now: datetime | None = None) -> bool:
    """True while an update must wait. See ``charge_state`` for the why."""
    return bool(charge_state(now)['charging'])
