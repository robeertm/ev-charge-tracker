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

**A flag that is fresh but false.** The age bound only helps when rows
stop arriving. In November 2026 a vendor library release set "charging"
on every cached status that carried a remaining-charge time — cable out,
0 kW, the car being driven. Rows kept arriving (SoC and odometer moved),
so every one of them was fresh and the gate refused every update for a
week. A charging flag is therefore checked against the raw values that
travel with it, per vehicle:

* **Signs of a real charge** — any power above zero in the last three
  hours, or SoC rising between two syncs while the odometer stands. Any
  one of them keeps the gate shut, whatever else the row says.
* **Evidence against** — four independent observations:
  ``zero_power`` (power reported, and 0, on every flagged row of the last
  three hours), ``no_cable`` (the vendor payload says unplugged or charge
  state 0), ``drove`` (two consecutive syncs both flagged "charging" while
  the odometer rose) and ``soc_fell`` (the same, with SoC falling).
* **Immediately** the flag is overruled only when three of the four agree.
* **After 24 hours** of a flag with no sign of a real charge, two suffice,
  provided one of them is physical (``drove`` or ``no_cable``): a charge
  cannot run while the car drives or without a cable. That is the "defer
  when unsure, but never for more than a day against clear evidence" rule.

Missing values are never evidence: a connector that reports no power
cannot contribute ``zero_power``. Mixed values keep the gate shut. Every
decision that keeps the gate shut or overrules a flag is appended, with
its reasons, to ``charge_gate.log`` in the data folder.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

#: A charging row older than this cannot claim a charge is still running.
CHARGE_ROW_MAX_AGE_H = 3.0

#: How far back the plausibility check looks for a real charge.
RECENT_H = 3.0

#: After this long without any sign of a real charge, two pieces of
#: evidence (one of them physical) overrule the flag instead of three.
LONG_CLAIM_H = 24.0

#: How much history is read at all (the long claim needs 24 h behind it).
LOOKBACK_H = 48.0

#: Power at or below this counts as "no power" (rounding noise).
ZERO_KW = 0.05

EVIDENCE = ('zero_power', 'no_cable', 'drove', 'soc_fell')
PHYSICAL = ('drove', 'no_cable')

_COLS = ('vehicle_id', 'timestamp', 'is_charging', 'soc_percent',
         'odometer_km', 'charge_power_kw', 'raw_json')

LOG_NAME = 'charge_gate.log'
LOG_MAX_BYTES = 256 * 1024


def _db_path() -> str:
    from config import Config
    uri = getattr(Config, 'SQLALCHEMY_DATABASE_URI', '') or ''
    if uri.startswith('sqlite:///'):
        return uri[len('sqlite:///'):]
    return ''


def _parse_ts(stempel):
    if stempel is None or isinstance(stempel, datetime):
        return stempel
    text = str(stempel).strip()
    for form in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                 '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, form)
        except ValueError:
            continue
    # A timestamp we cannot read is unclear evidence, not absence.
    raise ValueError('unlesbarer Zeitstempel: %r' % text[:40])


def _rows_orm():
    """Recent syncs as plain dicts, oldest first, through the ORM."""
    from models.database import VehicleSync
    neueste = (VehicleSync.query
               .order_by(VehicleSync.timestamp.desc())
               .first())
    if neueste is None:
        return []
    ab = neueste.timestamp - timedelta(hours=LOOKBACK_H)
    zeilen = (VehicleSync.query
              .filter(VehicleSync.timestamp >= ab)
              .order_by(VehicleSync.timestamp.asc())
              .all())
    return [{c: getattr(z, c, None) for c in _COLS} for z in zeilen]


def _rows_file():
    """Recent syncs straight from the file, read-only, oldest first.

    A missing file is "no data", not "unreadable": that is a fresh
    install, and the caller must be allowed to proceed. Columns an older
    schema lacks are simply absent — a missing value is never evidence.
    """
    pfad = _db_path()
    if not pfad or not os.path.isfile(pfad):
        return []
    con = sqlite3.connect('file:%s?mode=ro' % pfad, uri=True)
    try:
        vorhanden = {r[1] for r in con.execute(
            'PRAGMA table_info(vehicle_syncs)').fetchall()}
        if not vorhanden:
            raise sqlite3.DatabaseError('keine Tabelle vehicle_syncs')
        spalten = [c for c in _COLS if c in vorhanden]
        oben = con.execute(
            'SELECT timestamp FROM vehicle_syncs'
            ' ORDER BY timestamp DESC LIMIT 1').fetchone()
        if oben is None:
            return []
        ab = _parse_ts(oben[0]) - timedelta(hours=LOOKBACK_H)
        roh = con.execute(
            'SELECT %s FROM vehicle_syncs WHERE timestamp >= ?'
            ' ORDER BY timestamp ASC' % ', '.join(spalten),
            (ab.strftime('%Y-%m-%d %H:%M:%S.%f'),)).fetchall()
    finally:
        con.close()
    zeilen = []
    for r in roh:
        d = {c: None for c in _COLS}
        d.update(zip(spalten, r))
        d['timestamp'] = _parse_ts(d['timestamp'])
        zeilen.append(d)
    return zeilen


# ── the plausibility check ───────────────────────────────────────────

def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _dig(d, *pfad):
    for k in pfad:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _says_no_cable(raw_json) -> bool:
    """True only when the vendor payload says, explicitly, that no charge
    can be running: unplugged, connector not fastened, or charge state 0.
    Absent fields say nothing."""
    if not raw_json:
        return False
    try:
        d = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (ValueError, TypeError):
        return False
    if not isinstance(d, dict):
        return False
    for k in ('is_plugged_in', 'ev_battery_is_plugged_in', 'plugged_in',
              'isPluggedIn'):
        if d.get(k) is False:
            return True
    fest = d.get('ev_connector_fastening_state')
    if fest is None:
        fest = _dig(d, 'data', 'Green', 'ChargingInformation',
                    'ConnectorFastening', 'State')
    zustand = _dig(d, 'data', 'Green', 'ChargingInformation',
                   'Charging', 'State')
    # Charge state 0 alone is no proof (a car waiting for a timer can
    # report it), so it only counts together with an unfastened plug.
    if fest is not None and _num(fest) == 0:
        return zustand is None or _num(zustand) == 0
    return False


def assess(rows, now: datetime) -> dict:
    """Judge one vehicle's newest charging flag against its raw values.

    ``rows`` are that vehicle's syncs, oldest first, newest flagged
    "charging". Returns ``{'plausible': bool, 'evidence': [...],
    'signs': [...], 'claim_h': float}``.
    """
    neu = rows[-1]
    t_neu = neu['timestamp']
    seit = t_neu - timedelta(hours=RECENT_H)
    zeichen = []      # signs of a real charge
    belege = []       # evidence against

    letzte_ladung = None
    for vor, nach in zip(rows, rows[1:]):
        s0, s1 = _num(vor.get('soc_percent')), _num(nach.get('soc_percent'))
        o0, o1 = _num(vor.get('odometer_km')), _num(nach.get('odometer_km'))
        if (s0 is not None and s1 is not None and s1 > s0
                and (o0 is None or o1 is None or o1 <= o0)):
            letzte_ladung = nach['timestamp']
            if nach['timestamp'] >= seit:
                zeichen.append('soc_rose %g->%g' % (s0, s1))
    for z in rows:
        p = _num(z.get('charge_power_kw'))
        if p is not None and p > ZERO_KW:
            if letzte_ladung is None or z['timestamp'] > letzte_ladung:
                letzte_ladung = z['timestamp']
            if z['timestamp'] >= seit:
                zeichen.append('power %.1f kW' % p)

    jung_flag = [z for z in rows if z.get('is_charging') and z['timestamp'] >= seit]
    leistungen = [_num(z.get('charge_power_kw')) for z in jung_flag]
    if leistungen and all(p is not None and p <= ZERO_KW for p in leistungen):
        belege.append('zero_power')
    if _says_no_cable(neu.get('raw_json')):
        belege.append('no_cable')

    gefahren = gefallen = False
    for vor, nach in zip(rows, rows[1:]):
        if not (vor.get('is_charging') and nach.get('is_charging')):
            continue
        if letzte_ladung is not None and vor['timestamp'] < letzte_ladung:
            continue
        o0, o1 = _num(vor.get('odometer_km')), _num(nach.get('odometer_km'))
        s0, s1 = _num(vor.get('soc_percent')), _num(nach.get('soc_percent'))
        if o0 is not None and o1 is not None and o1 >= o0 + 1:
            gefahren = True
        if s0 is not None and s1 is not None and s1 <= s0 - 1:
            gefallen = True
    if gefahren:
        belege.append('drove')
    if gefallen:
        belege.append('soc_fell')

    # How long has the flag been claiming a charge without any sign of
    # one? From the oldest flagged row after the last real-charge sign.
    geflaggt = [z['timestamp'] for z in rows if z.get('is_charging')
                and (letzte_ladung is None or z['timestamp'] > letzte_ladung)]
    behauptet_h = ((now - min(geflaggt)).total_seconds() / 3600.0
                   if geflaggt else 0.0)

    if zeichen:
        falsch = False
    elif len(belege) >= 3:
        falsch = True
    elif (behauptet_h >= LONG_CLAIM_H and len(belege) >= 2
          and any(b in PHYSICAL for b in belege)):
        falsch = True
    else:
        falsch = False
    return {'plausible': not falsch, 'evidence': belege,
            'signs': zeichen, 'claim_h': round(behauptet_h, 1)}


def _describe(a: dict) -> str:
    teile = []
    if a['signs']:
        teile.append('signs of charging: ' + ', '.join(a['signs']))
    teile.append('evidence against: ' + (', '.join(a['evidence']) or 'none'))
    teile.append('flag held %.1f h' % a['claim_h'])
    return '; '.join(teile)


def charge_state(now: datetime | None = None) -> dict:
    """``{'charging': bool, 'row_at': datetime|None, 'reason': str}``.

    ``reason`` is for the log and the API message, never for a decision —
    callers branch on ``charging`` alone. Every vehicle's newest row is
    asked: a restart stops the sync loop for the whole fleet.
    """
    jetzt = now or datetime.now()
    try:
        from flask import has_app_context
        im_kontext = has_app_context()
    except Exception:
        im_kontext = False
    try:
        zeilen = _rows_orm() if im_kontext else _rows_file()
    except Exception as e:
        return {'charging': True, 'row_at': None,
                'reason': 'unreadable: %s' % e}
    if not zeilen:
        return {'charging': False, 'row_at': None, 'reason': 'no data'}

    je_auto = {}
    for z in zeilen:
        je_auto.setdefault(z.get('vehicle_id'), []).append(z)

    ergebnisse = []
    for auto, reihe in je_auto.items():
        neu = reihe[-1]
        stempel = neu['timestamp']
        if not neu.get('is_charging'):
            ergebnisse.append((False, stempel, 'idle'))
            continue
        if stempel is not None and jetzt - stempel > timedelta(hours=CHARGE_ROW_MAX_AGE_H):
            ergebnisse.append((False, stempel,
                               'stale (older than %.0f h)' % CHARGE_ROW_MAX_AGE_H))
            continue
        a = assess(reihe, jetzt)
        if a['plausible']:
            grund = 'charging' if not (a['evidence'] or a['signs']) \
                else 'charging (%s)' % _describe(a)
            ergebnisse.append((True, stempel, grund))
        else:
            ergebnisse.append((False, stempel,
                               'implausible charging flag (%s)' % _describe(a)))

    neueste = max(zeilen, key=lambda z: z['timestamp'])['timestamp']
    laedt = [e for e in ergebnisse if e[0]]
    if laedt:
        e = max(laedt, key=lambda e: e[1])
        return {'charging': True, 'row_at': e[1], 'reason': e[2]}
    # Not charging: report the most telling reason (an overruled flag
    # beats "stale" beats "idle"), with the newest row's time.
    for wort in ('implausible', 'stale', 'idle'):
        for e in ergebnisse:
            if e[2].startswith(wort):
                return {'charging': False, 'row_at': e[1] if wort != 'idle' else neueste,
                        'reason': e[2]}
    return {'charging': False, 'row_at': neueste, 'reason': 'idle'}


def charge_in_progress(now: datetime | None = None) -> bool:
    """True while an update must wait. See ``charge_state`` for the why."""
    return bool(charge_state(now)['charging'])


def record(state: dict, source: str) -> None:
    """Append a gate decision to the updater log — best effort, never raises.

    Only decisions worth reading later are written: a refusal, and a
    charging flag the gate overruled. "idle" and "no data" are routine.
    """
    grund = state.get('reason') or ''
    if not (state.get('charging') or grund.startswith(('implausible', 'stale'))):
        return
    try:
        from config import DATA_DIR
        pfad = os.path.join(DATA_DIR, LOG_NAME)
        if os.path.isfile(pfad) and os.path.getsize(pfad) > LOG_MAX_BYTES:
            os.replace(pfad, pfad + '.1')
        zeit = state.get('row_at')
        with open(pfad, 'a', encoding='utf-8') as fh:
            fh.write('%s %s %s (newest sync %s): %s\n' % (
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'), source,
                'REFUSED' if state.get('charging') else 'ALLOWED',
                zeit.isoformat(sep=' ', timespec='seconds') if isinstance(zeit, datetime) else '-',
                grund))
    except Exception:
        pass
