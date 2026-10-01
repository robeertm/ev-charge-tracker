"""The wallbox link — pulling home charges from a Shelly Energy Analyzer.

Two programs know half of a home charge each. This one knows *which* car was
plugged in, what its state of charge did and how far it then drove. The energy
analyzer in the house knows how many kilowatt-hours actually went through the
wallbox and — where a grid meter and a PV or battery series cover the window —
how many of them came from the sun, from the house battery and from the grid,
and what that really cost. Neither can work out the other's half.

This module fetches the analyzer's half and files it against our charges.

Four rules it is built on, each of them a way it could have gone wrong quietly:

* **We fetch; nothing is pushed at us.** The analyzer never writes here. A push
  would arrive with no idea which car it belongs to, and matching is precisely
  the thing only this side can do.
* **A measurement is kept, not merged.** Every fetched charge becomes a
  :class:`WallboxCharge` row of its own. What the meter saw stays intact even
  when the user edits the charge entry, and adoption into the entry keeps the
  previous values so it can be undone.
* **A match must be unambiguous or it is not a match.** With two cars on one
  wallbox the meter cannot say which was plugged in. Where more than one car
  fits the window, the reading is filed as *ambiguous* and left for a human —
  never guessed, and never silently attached to the first candidate.
* **An unmeasured share is null, not zero.** "No sun measured" and "no sun"
  are different statements, and only one of them may be shown as 0 %.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Configuration keys (AppConfig) ────────────────────────────────────────
K_ENABLED = 'shelly_enabled'
K_URL = 'shelly_url'
K_TOKEN = 'shelly_token'
K_VERIFY = 'shelly_verify_ssl'
K_TOL = 'shelly_match_tolerance_min'
K_APPLY = 'shelly_apply_mode'
K_BACKFILL = 'shelly_backfill_days'
K_LAST_TS = 'shelly_last_sync_ts'
K_LAST_RESULT = 'shelly_last_result'

# How far the two clocks may disagree and still describe the same charge.
# Our own window comes from car syncs — the cloud reports a state change minutes
# after it happened, and an auto-detected charge is bracketed by two polls that
# can be an hour apart. The wallbox, by contrast, knows to the second. 90
# minutes is wide enough for that lag and far narrower than the gap between two
# charges of the same car on the same day.
DEFAULT_TOLERANCE_MIN = 90

# Two candidates whose start times lie this close together cannot be told apart
# by timing alone. If they belong to different cars, that is an ambiguity and
# not a ranking.
AMBIGUOUS_WITHIN_S = 30 * 60

DEFAULT_BACKFILL_DAYS = 90

#: How far behind the last poll a routine poll asks. The analyzer withholds a
#: charge until it has been over for its settle window; a charge that was still
#: settling on our last poll ended less than that before it — asking "since
#: that poll" would skip it for good. Filing is by id, so an overlap costs
#: nothing. The hour on top is for a session the analyzer's log closes late.
SINCE_OVERLAP_GRACE_S = 3600
DEFAULT_SETTLE_MIN = 20


def since_for(last_ts, info=None):
    """The ``since`` a routine poll should ask with, given our last poll.

    🔴 Not ``last_ts`` itself. That lost a real charge (2026-09-15): it ended
    a minute before a poll, was withheld as "still settling", and the next
    poll asked since that first poll — the charge, now settled, ended before
    it. At a 30-minute poll and a 20-minute settle two charges in three went
    that way; only the backfill ever delivered anything.
    """
    try:
        settle_min = int(float((info or {}).get('settle_minutes')))
    except (TypeError, ValueError, AttributeError):
        settle_min = DEFAULT_SETTLE_MIN
    if settle_min < 0:
        settle_min = DEFAULT_SETTLE_MIN
    return max(0, int(last_ts) - settle_min * 60 - SINCE_OVERLAP_GRACE_S)
# What the analyzer's link will hand out in one request.
MAX_DAYS = 400

_sync_lock = threading.Lock()
_sync_running = False
_sync_thread = None


# ══════════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════════

def _truthy(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def settings():
    """The link's configuration, already coerced. Call inside an app context."""
    from models.database import AppConfig
    try:
        tol = int(float(AppConfig.get(K_TOL, DEFAULT_TOLERANCE_MIN)))
    except (TypeError, ValueError):
        tol = DEFAULT_TOLERANCE_MIN
    try:
        days = int(float(AppConfig.get(K_BACKFILL, DEFAULT_BACKFILL_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_BACKFILL_DAYS
    apply_mode = str(AppConfig.get(K_APPLY, 'auto') or 'auto').strip().lower()
    if apply_mode not in ('auto', 'always', 'never'):
        apply_mode = 'auto'
    return {
        'enabled': _truthy(AppConfig.get(K_ENABLED), False),
        'url': str(AppConfig.get(K_URL, '') or '').strip().rstrip('/'),
        'token': str(AppConfig.get(K_TOKEN, '') or '').strip(),
        # The analyzer serves HTTPS with a certificate it signed itself — it is
        # a box on the home network, not a public site, and there is no
        # authority that could vouch for it. Verification is therefore off by
        # default and can be turned on by anyone who installed a real
        # certificate. The link token is what actually protects the channel.
        'verify_ssl': _truthy(AppConfig.get(K_VERIFY), False),
        'tolerance_min': max(5, min(720, tol)),
        'apply_mode': apply_mode,
        'backfill_days': max(1, min(MAX_DAYS, days)),
    }


def configured(cfg=None):
    cfg = cfg or settings()
    return bool(cfg['enabled'] and cfg['url'] and cfg['token'])


# ══════════════════════════════════════════════════════════════════════════
# Talking to the analyzer
# ══════════════════════════════════════════════════════════════════════════

class LinkError(RuntimeError):
    """A failure worth showing the user verbatim — it names what to fix."""


def _get(cfg, route, params=None, timeout=25):
    import requests
    url = '%s/api/v1/ev/%s' % (cfg['url'], route)
    try:
        r = requests.get(
            url, params=params or {}, timeout=timeout,
            verify=cfg['verify_ssl'],
            headers={'X-EV-Link-Token': cfg['token'],
                     'Accept': 'application/json'},
        )
    except requests.exceptions.SSLError as e:
        raise LinkError('TLS: %s' % e) from e
    except requests.exceptions.RequestException as e:
        raise LinkError('unreachable: %s' % e) from e
    if r.status_code in (401, 403):
        raise LinkError('rejected (%d) — the link token does not match, or the '
                        'link is switched off in the analyzer' % r.status_code)
    if r.status_code == 404:
        raise LinkError('no link endpoint (404) — the analyzer is older than '
                        'the wallbox link')
    if r.status_code >= 400:
        raise LinkError('HTTP %d' % r.status_code)
    try:
        data = r.json()
    except ValueError as e:
        # An HTML login page answering 200 is the classic shape of this: the
        # request reached *something*, just not the link.
        raise LinkError('not a JSON answer — is the address the analyzer?') from e
    if not isinstance(data, dict) or not data.get('ok'):
        raise LinkError(str((data or {}).get('error') or 'refused'))
    return data.get('data') or {}


def probe(cfg=None):
    """Ask who is at the other end. Used by the "test" button and before a sync."""
    cfg = cfg or settings()
    if not cfg['url']:
        raise LinkError('no address configured')
    if not cfg['token']:
        raise LinkError('no link token configured')
    return _get(cfg, 'info', timeout=12)


def fetch_charges(cfg, days=None, since_ts=None):
    params = {}
    if since_ts:
        params['since'] = int(since_ts)
    else:
        params['days'] = int(days or DEFAULT_BACKFILL_DAYS)
    return _get(cfg, 'charges', params, timeout=60)


def fetch_curve(cfg, start_ts, end_ts):
    return _get(cfg, 'curve', {'start': int(start_ts), 'end': int(end_ts)}, timeout=45)


# ══════════════════════════════════════════════════════════════════════════
# Storing what came back
# ══════════════════════════════════════════════════════════════════════════

def store_charges(payload):
    """Upsert the fetched charges. Returns (new, updated).

    Idempotent by ``(device_key, source_id)`` — and, because that is not
    enough, by the charge's end as well.

    🔴 The id is ``md5(device:start:end)``, and this used to say it stops moving
    once a charge is over. It does not. The analyzer answers out of the samples
    it has: a charge whose first minutes have not reached its database yet comes
    back with a LATER start, which is a different id, which is a second row for
    one physical charge. Measured on a real installation — one charge offered
    five times, its start creeping forward by exactly the poll interval each
    time, until 14 of 27 open readings were ghosts of four real charges.

    A meter cannot end two sessions on one device in the same second, so a row
    with the same device and the same end IS this charge. Of two answers about
    it, keep the one reporting MORE energy: an incomplete answer always loses
    some — a truncated head, or a hole in the middle that reads as a pause —
    and never invents any. Deciding on the start instead would have thrown away
    the very case this exists for, where a charge came back with the right
    window and half the kilowatt-hours.

    Only for readings no charge entry is built on yet: a matched reading is
    somebody's evidence, and a fetch must not rewrite it underneath them.
    """
    from models.database import db, WallboxCharge
    key_default = str((payload.get('wallbox') or {}).get('device_key') or '')
    neu = upd = 0
    for c in payload.get('charges') or []:
        sid = str(c.get('id') or '').strip()
        if not sid:
            continue
        dev = str(c.get('device_key') or key_default)
        anfang = int(c.get('start_ts') or 0)
        ende = int(c.get('end_ts') or 0)
        row = WallboxCharge.query.filter_by(device_key=dev, source_id=sid).first()
        if row is None and ende > 0:
            zwilling = (WallboxCharge.query
                        .filter(WallboxCharge.device_key == dev,
                                WallboxCharge.end_ts == ende,
                                WallboxCharge.match_state != 'matched')
                        .order_by(WallboxCharge.start_ts.asc())
                        .first())
            if zwilling is not None:
                if _f(c.get('energy_kwh')) is None or (
                        float(c.get('energy_kwh') or 0)
                        <= float(zwilling.energy_kwh or 0)):
                    # The view we already hold saw at least as much: this one
                    # carries nothing new. Note that we looked.
                    zwilling.fetched_at = datetime.now()
                    upd += 1
                    continue
                # This one saw more of the same charge. It takes over the row
                # that is already there, so nothing pointing at it is
                # orphaned. Counted once, below.
                row = zwilling
                row.source_id = sid
        if row is None:
            row = WallboxCharge(device_key=dev, source_id=sid,
                                match_state='unmatched')
            db.session.add(row)
            neu += 1
        else:
            upd += 1
        row.start_ts = anfang
        row.end_ts = ende
        row.energy_kwh = _f(c.get('energy_kwh'))
        # Passed through exactly as sent — the analyzer sends null where nothing
        # was measured, and turning that into 0.0 here would invent a fact.
        row.solar_kwh = _f(c.get('solar_kwh'))
        row.battery_kwh = _f(c.get('battery_kwh'))
        row.grid_kwh = _f(c.get('grid_kwh'))
        row.cost_eur = _f(c.get('cost_eur'))
        row.cost_model = str(c.get('cost_model') or 'fixed')
        row.coverage = _f(c.get('coverage'))
        row.avg_power_w = _f(c.get('avg_power_w'))
        row.peak_power_w = _f(c.get('peak_power_w'))
        row.session_count = int(c.get('session_count') or 1)
        row.fetched_at = datetime.now()
    db.session.commit()
    return neu, upd


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════
# Matching a reading to a charge
# ══════════════════════════════════════════════════════════════════════════

def charge_window(c):
    """(start, end) of one of our charge entries, as local naive datetimes.

    The entry stores a date and whole hours, not timestamps — that is the
    resolution the app has always had. So the window is the hour the charge
    started through the end of the hour it finished in, rolling over midnight
    when the end hour is the smaller one. Where no end hour was ever recorded
    (legacy rows, manual entries) the window is the start hour alone and the
    match tolerance does the rest.
    """
    if c.date is None:
        return None, None
    start = datetime(c.date.year, c.date.month, c.date.day,
                     int(c.charge_hour or 0), 0, 0)
    if c.charge_end_hour is None:
        return start, start + timedelta(hours=1)
    end = datetime(c.date.year, c.date.month, c.date.day,
                   int(c.charge_end_hour), 0, 0) + timedelta(hours=1)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _overlaps(a0, a1, b0, b1, tol):
    """Do the two windows meet, allowing each side to be off by ``tol``?"""
    return (a0 - tol) < b1 and (a1 + tol) > b0


def linked_vehicles(device_key=''):
    """Cars whose owner said they charge on this wallbox.

    A car with no explicit device key is bound to *the* wallbox — the one the
    analyzer itself is set up for — which is the whole answer in a household
    with one box. A car that names a different box is not a candidate here.
    """
    from models.database import Vehicle
    out = []
    for v in Vehicle.query.filter_by(wallbox_link_enabled=True).all():
        own = str(v.wallbox_device_key or '').strip()
        if own and device_key and own != device_key:
            continue
        out.append(v)
    return out


def _candidates(wc, vehicles, tol_min):
    """Our charge entries that could be this wallbox reading. Ranked.

    Every gate here says no for a reason worth being able to quote:

    * a DC charge happened at a fast charger, not on a wallbox at home;
    * an entry already tied to a *different* reading is taken — a wallbox
      charge and a charge entry are one to one;
    * a car that was not bound to this wallbox is nobody's candidate;
    * and the windows have to actually meet.
    """
    from models.database import Charge
    if not vehicles:
        return []
    vids = [v.id for v in vehicles]
    ws = datetime.fromtimestamp(int(wc.start_ts))
    we = datetime.fromtimestamp(int(wc.end_ts))
    tol = timedelta(minutes=tol_min)
    # One day either side of the wallbox window is all a match can ever span;
    # the tolerance is hours, not days.
    lo = (ws - timedelta(days=1)).date()
    hi = (we + timedelta(days=1)).date()

    rows = (Charge.query
            .filter(Charge.vehicle_id.in_(vids))
            .filter(Charge.date >= lo, Charge.date <= hi)
            .all())
    out = []
    for c in rows:
        if (c.charge_type or 'AC').upper() == 'DC':
            continue
        if c.wallbox_charge_id and c.wallbox_charge_id != wc.id:
            continue
        cs, ce = charge_window(c)
        if cs is None:
            continue
        if not _overlaps(cs, ce, ws, we, tol):
            continue
        delta = abs((cs - ws).total_seconds())
        # Energy is the tie-breaker, never a gate: the wallbox measures at the
        # wall and our entry may hold a theoretical figure derived from the
        # state of charge, so they legitimately differ by the charge losses.
        de = (abs(float(c.kwh_loaded) - float(wc.energy_kwh or 0))
              if c.kwh_loaded is not None and wc.energy_kwh else 1e6)
        out.append((delta, de, c))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def match_one(wc, vehicles, tol_min):
    """Decide what this reading belongs to. Returns (state, charge, note)."""
    cands = _candidates(wc, vehicles, tol_min)
    if not cands:
        return 'unmatched', None, 'no charge of a linked car falls in this window'
    best = cands[0]
    rivals = [c for c in cands[1:]
              if c[2].vehicle_id != best[2].vehicle_id
              and abs(c[0] - best[0]) < AMBIGUOUS_WITHIN_S]
    if rivals:
        # 🔴 Never resolved by "whoever is closer". Two cars, two charges within
        # half an hour of the same window — the wallbox saw one of them and
        # cannot say which. Guessing here would put a stranger's kilowatt-hours
        # into someone's running costs and nobody would ever notice.
        names = sorted({str(c[2].vehicle_id) for c in [best] + rivals})
        return ('ambiguous', None,
                'more than one car has a charge in this window (vehicle ids %s)'
                % ', '.join(names))
    return 'matched', best[2], ''


#: Above this share of sun + house battery a home charge is a PV charge. The
#: app has had that category since long before the meter existed; what was
#: missing was somebody to tick it. 90 % leaves room for the few minutes of
#: grid a charge takes while a cloud passes without demoting the whole charge.
PV_SCHWELLE = 0.90


def _apply_wanted(mode, charge):
    """Should the meter's kWh and cost be taken over into the entry?

    ``never``  — annotate only; the entry keeps whatever it says.
    ``always`` / ``auto`` — the meter wins.

    🔑 A charge at your own wallbox is the one case where there is nothing to
    weigh up: the meter sat in the wire, the entry holds whatever the car
    reported about its own battery, and the two are not equal — charge losses
    alone are several percent. "auto" used to protect a typed-in price here,
    which sounds careful but leaves the worse number standing in exactly the
    place where a better one exists. Every adoption stays reversible (the old
    values live on the reading), so nothing is lost by preferring the meter.
    """
    return mode != 'never'


#: Smallest share of the battery's own SoC gain a reading may report before it
#: is treated as incomplete rather than low.
#:
#: 🔑 A meter in the wall cannot have delivered LESS energy than the battery
#: demonstrably gained: everything it counted went through the charger, and a
#: charger loses energy rather than making it. So a reading below the SoC gain
#: is not a small charge, it is a PARTIAL ANSWER — the log computes sessions
#: out of the samples it has, and while a long charge is still moving from the
#: live buffer into the database, a hole in the middle reads as two sessions
#: with a pause and only the covered parts get counted. Taking that over
#: silently halves a charge, and nothing in the entry looks wrong afterwards.
#:
#: The comparison needs room: SoC resolution is one percent, usable capacity is
#: an estimate and the BMS recalibrates. Measured over 32 real charges of one
#: car, metered energy ÷ SoC gain ran from 0.88 to 2.28 (median 1.11) — while
#: the two readings that were provably incomplete sat at 0.59 and 0.48. 0.75
#: has clear air on both sides.
#:
#: 🔴 Only the LOW side is a gate. A high ratio is ordinary — a charge that
#: tops off an almost full battery moves the SoC hardly at all — and must never
#: block anything.
MIN_SHARE_OF_SOC_GAIN = 0.75


def contradicts_the_battery(wc, charge, battery_kwh):
    """Why this reading must not be taken over, or ``None`` when it may.

    Silent about everything it cannot judge: no SoC window, no capacity or no
    energy means no evidence, and no evidence is not an objection.
    """
    gain = charge.soc_charged
    if gain is None and charge.soc_from is not None and charge.soc_to is not None:
        gain = charge.soc_to - charge.soc_from
    if not gain or gain <= 0 or not battery_kwh or not wc.energy_kwh:
        return None
    net = float(gain) / 100.0 * float(battery_kwh)
    share = float(wc.energy_kwh) / net if net > 0 else None
    if share is None or share >= MIN_SHARE_OF_SOC_GAIN:
        return None
    return ('meter reports %.3f kWh but the battery gained %d %% = %.2f kWh — '
            'the reading looks incomplete, not low' % (wc.energy_kwh, gain, net))


def typ_aus_anteil(wc):
    """'PV' or 'AC' for a measured home charge — or None when unmeasured.

    Only a KNOWN split may re-type a charge. Where the analyzer reports
    nothing (no meters in that house), the entry keeps the type it has: an
    unmeasured charge is not evidence of grid power.

    🔴 "Known" is not "cost_model == 'source'". That says how the price was
    worked out; a house with no PV pays a flat tariff and still knows its mix
    exactly — all grid. Asking the wrong one would have left those houses
    without the very typing they benefit from.
    """
    if not wc.energy_kwh:
        return None
    if wc.solar_kwh is None or wc.battery_kwh is None or wc.grid_kwh is None:
        return None
    eigen = float(wc.solar_kwh or 0) + float(wc.battery_kwh or 0)
    return 'PV' if (eigen / float(wc.energy_kwh)) >= PV_SCHWELLE else 'AC'


#: The note the app writes onto a charge it detected by itself. A charge the
#: meter has confirmed must not keep asking to be checked — so this exact text
#: (and only this one) is replaced. Anything a person typed stays untouched.
_GEMESSEN_NOTIZ = 'Automatisch erkannt \u00b7 an der Wallbox gemessen'
_PRUEFNOTIZ_ZURUECK = 'Automatisch erkannt (keine App-Session) \u2014 bitte pr\u00fcfen'


def _ist_pruefnotiz(text):
    """Is this the app's own "please check" note, rather than a person's?

    The app writes two of them (no app session / SoC jump), both ending in the
    same words. Matching on those two ends — and not on a substring somewhere
    in the middle — keeps a note somebody typed themselves out of reach.
    """
    t = (text or '').strip()
    return t.startswith('Automatisch erkannt') and t.endswith('bitte pr\u00fcfen')


def co2_aus_mischung(wc, netz_g, pv_g):
    """The charge's own CO2 intensity, weighted by where its kWh came from.

    The app books one intensity per charge. For a charge at a wallbox that is
    a fiction: 5.63 kWh of sunshine and 0.011 kWh of grid were booked at the
    grid mix, which overstated a nearly carbon-free charge roughly tenfold.

    ``netz_g`` is the grid intensity for the charge window (ENTSO-E, exactly
    what the entry carried before), ``pv_g`` the owner's own PV intensity from
    Settings — production CO2 amortised over the system's yield and lifetime.

    🔑 The house battery counts as own generation, the same way the analyzer
    prices it: what comes out of it went in from the roof. A house that charges
    its battery off the grid at night would be flattered here — the analyzer
    cannot tell those apart today, and inventing a third number would be worse
    than saying so.

    Returns None where anything needed is missing: an unmeasured mix, no PV
    figure, no grid figure. **Never** a guess.
    """
    if netz_g is None or pv_g is None:
        return None
    if not wc.energy_kwh or wc.solar_kwh is None or wc.battery_kwh is None \
            or wc.grid_kwh is None:
        return None
    eigen = float(wc.solar_kwh) + float(wc.battery_kwh)
    netz = float(wc.grid_kwh)
    summe = eigen + netz
    if summe <= 0:
        return None
    return int(round((eigen * float(pv_g) + netz * float(netz_g)) / summe))


def apply_measurement(wc, charge, battery_kwh=None, efficiency=None, pv_co2=None):
    """Take the meter's numbers into the charge entry — reversibly.

    What stood there before is kept on the reading, so the adoption can be
    undone exactly. Only the energy and the money move: state of charge,
    odometer, location and notes belong to the car and the meter knows nothing
    about them.
    """
    if wc.applied_at is None:
        wc.prev_kwh_loaded = charge.kwh_loaded
        wc.prev_total_cost = charge.total_cost
        wc.prev_eur_per_kwh = charge.eur_per_kwh
        wc.prev_needs_review = charge.needs_review
        wc.prev_charge_type = charge.charge_type
    charge.kwh_loaded = round(float(wc.energy_kwh or 0), 3)
    # The per-kWh price becomes an *effective* one: with surplus charging the
    # sun and the battery cost nothing, so the mixed price of a charge is lower
    # than the tariff — and that is exactly the number the running-cost figures
    # should be built on.
    if wc.energy_kwh:
        charge.eur_per_kwh = round(float(wc.cost_eur or 0) / float(wc.energy_kwh), 4)
    # calculate_fields would multiply price × kWh again and round to cents;
    # letting it do so keeps every derived figure consistent with the rest of
    # the app rather than introducing a second way of computing a total.
    # The CO2 of a mixed charge is a mix too. Always computed from the ORIGINAL
    # grid intensity (kept on the reading), never from a value this function
    # wrote itself — otherwise a second pass would mix the mix.
    # 🔴 The base is the ORIGINAL grid intensity, and it is recorded only when
    # something actually changes. Recording it on every adoption looked
    # harmless and quietly disabled the catch-up: a row whose CO2 could not be
    # mixed yet (no PV figure in Settings) was marked "done" and never looked
    # at again.
    _pv_g = pv_co2() if callable(pv_co2) else pv_co2
    _basis = (wc.prev_co2_g_per_kwh if wc.prev_co2_g_per_kwh is not None
              else charge.co2_g_per_kwh)
    _misch = co2_aus_mischung(wc, _basis, _pv_g)
    if _misch is not None and _misch != charge.co2_g_per_kwh:
        if wc.prev_co2_g_per_kwh is None:
            wc.prev_co2_g_per_kwh = charge.co2_g_per_kwh
        charge.co2_g_per_kwh = _misch

    charge.calculate_fields(battery_kwh, efficiency)

    # 🔑 A charge the house meter has measured is not a charge that needs
    # checking. The flag asks "is this entry right?" — and the answer just
    # arrived from the wire, with a curve behind it. Leaving the red row
    # standing would tell the owner to go and verify a number that is now
    # better than anything they could type.
    charge.needs_review = False
    if _ist_pruefnotiz(charge.notes):
        charge.notes = _GEMESSEN_NOTIZ

    # The app has had a PV category since long before the meter existed; what
    # was missing was somebody to tick it. Only a MEASURED split may do so —
    # see typ_aus_anteil.
    typ = typ_aus_anteil(wc)
    if typ:
        charge.charge_type = typ

    wc.applied_at = datetime.now()
    wc.undone_at = None          # taking it over again settles the argument


def hole_versaeumtes_nach(mode, specs=None, pv_co2=None):
    """Settle matched readings the matcher will never look at again.

    ``match_all`` deliberately retries only what is NOT matched — a settled
    match should not be re-decided on every pass. The price is that two kinds
    of reading get stuck once they are matched:

    * one the rule of the day declined (``never``, or the old ``auto`` that
      spared a typed-in price). Changing the setting afterwards, or changing
      the rule as v3.0.129 does, would otherwise reach new charges only;
    * one taken over before v3.0.129, which is filed but still asks to be
      checked and is still typed by hand.

    Both are settled here, once each: the first gets the measurement, the
    second gets the flag and the type. 🔴 A reading somebody has explicitly
    undone is left alone — an automatism that re-does what a person just undid
    is not an automatism, it is a fight.
    """
    if mode == 'never':
        return {'applied': 0, 'retyped': 0}
    from models.database import db, Charge, WallboxCharge
    rows = (WallboxCharge.query
            .filter(WallboxCharge.match_state == 'matched',
                    WallboxCharge.charge_id.isnot(None),
                    WallboxCharge.undone_at.is_(None))
            .all())
    tally = {'applied': 0, 'retyped': 0, 'co2': 0}
    _pv_g = pv_co2() if callable(pv_co2) else pv_co2
    for wc in rows:
        charge = Charge.query.get(wc.charge_id)
        if charge is None:
            continue
        if wc.applied_at is None:
            bk, eff = (specs or _default_specs)(charge.vehicle_id)
            apply_measurement(wc, charge, bk, eff, pv_co2)
            tally['applied'] += 1
            continue

        # 🔴 Each of the two jobs below has to be its own question. They were
        # written as one chain, and a row that had already been re-typed then
        # never had its CO2 looked at again — the very rows on the screen that
        # started this.
        if wc.prev_co2_g_per_kwh is None:
            misch = co2_aus_mischung(wc, charge.co2_g_per_kwh, _pv_g)
            if misch is not None and misch != charge.co2_g_per_kwh:
                wc.prev_co2_g_per_kwh = charge.co2_g_per_kwh
                charge.co2_g_per_kwh = misch
                bk, eff = (specs or _default_specs)(charge.vehicle_id)
                charge.calculate_fields(bk, eff)
                tally['co2'] += 1

        if wc.prev_needs_review is None:
            # Filed before the typing rule existed: record what we found (the
            # undo needs it) and bring the entry along.
            wc.prev_needs_review = bool(charge.needs_review)
            wc.prev_charge_type = charge.charge_type
            charge.needs_review = False
            if _ist_pruefnotiz(charge.notes):
                charge.notes = _GEMESSEN_NOTIZ
            typ = typ_aus_anteil(wc)
            if typ:
                charge.charge_type = typ
            tally['retyped'] += 1
    if any(tally.values()):
        db.session.commit()
        logger.info('Wallbox link: caught up %d adoption(s), %d re-typed, '
                    '%d CO2 mixes', tally['applied'], tally['retyped'],
                    tally['co2'])
    return tally


def unapply_measurement(wc, charge, battery_kwh=None, efficiency=None):
    """Put back what the entry said before the meter's numbers were adopted."""
    if wc.applied_at is None:
        return False
    charge.kwh_loaded = wc.prev_kwh_loaded
    charge.eur_per_kwh = wc.prev_eur_per_kwh
    charge.total_cost = wc.prev_total_cost
    # Undo is not undo if it puts back two of four things. The review flag and
    # the type were changed by the adoption, so they come back with it — and
    # the note goes back to asking, because that is what the entry said.
    if wc.prev_needs_review is not None:
        charge.needs_review = bool(wc.prev_needs_review)
        if charge.needs_review and charge.notes == _GEMESSEN_NOTIZ:
            charge.notes = _PRUEFNOTIZ_ZURUECK
    if wc.prev_charge_type is not None:
        charge.charge_type = wc.prev_charge_type
    if wc.prev_co2_g_per_kwh is not None:
        charge.co2_g_per_kwh = wc.prev_co2_g_per_kwh
    charge.calculate_fields(battery_kwh, efficiency)
    wc.applied_at = None
    wc.undone_at = datetime.now()
    wc.prev_kwh_loaded = wc.prev_total_cost = wc.prev_eur_per_kwh = None
    wc.prev_needs_review = wc.prev_charge_type = None
    wc.prev_co2_g_per_kwh = None
    return True


def _default_specs(vehicle_id):
    """(battery kWh, charge efficiency) — the fallback when nobody passes one.

    The app has its own self-calibrating versions of both; they live next to
    the routes and pulling them in from here would make this module import the
    application it is a part of. So the caller hands them over, and this is
    what is used when it does not: the car's own capacity, and the fleet
    average loss the app falls back to before it has learned anything.
    """
    from config import Config
    from models.database import Vehicle
    v = Vehicle.query.get(vehicle_id) if vehicle_id else None
    bk = float(getattr(v, 'battery_kwh', None) or 0) or float(
        getattr(Config, 'BATTERY_CAPACITY_KWH', 0) or 0) or None
    return bk, 0.88


# ══════════════════════════════════════════════════════════════════════════
# A reading nobody claims can become the charge itself
# ══════════════════════════════════════════════════════════════════════════

#: 🔑 The meter is the one witness the brand's cloud cannot mislead. Until
#: now it could only ever DECORATE an entry the car-side detector had
#: already found — so every weakness of that detector cost a whole charge,
#: while the measurement sat next to it holding exactly what was missing.
#: Three times in four days, each time a different weakness:
#:
#:   28.09.  the drive home was not subtracted from the starting SoC
#:   29.09.  a stale charging flag aborted the fallback detector
#:   30.09.  the only sync before the charging flag was already an HOUR
#:           INSIDE the charge — no sync at all between 07:02 and 16:40 —
#:           so the "SoC before the charge" was in truth a SoC during it:
#:           85 -> 87, a 2 % gain, under the 3 % threshold, whole charge
#:           dropped, while the meter had 4.956 kWh to the watt-hour.
#:
#: Patching the fourth weakness the same way would be a treadmill. So the
#: meter may file the charge itself where the car can be SHOWN to have been
#: charging — and stays silent where it cannot.
#:
#: 🔴 Silent is the important half. Two of this wallbox's readings belong to
#: a visitor's car (26.07. 13.2 kWh, 31.07. 31.9 kWh) and look exactly like
#: a big home charge. Neither route below fires for them.

#: The note a charge carries that the meter filed on its own. It says where
#: the number came from; the kilowatt-hours are measured, so nothing here
#: asks to be checked.
_AUS_MESSUNG_NOTIZ = ('Aus der Wallbox-Messung angelegt \u00b7 '
                      'Ladestand nicht erfasst')


def _steht_zuhause(sync):
    """Was the car at its own place when this sync was taken?

    Uses the app's own answer to that question — the same 200 m around the
    saved home that the trip list and the car-side detector use. A second
    radius of my own would be a second truth.
    """
    if sync is None or sync.location_lat is None or sync.location_lon is None:
        return False
    from services.trips_service import _classify_location
    return _classify_location(sync.location_lat, sync.location_lon)[0] == 'home'


def _letzter_sync(vehicle_id, bis):
    from models.database import VehicleSync
    return (VehicleSync.query
            .filter(VehicleSync.vehicle_id == vehicle_id,
                    VehicleSync.timestamp <= bis)
            .order_by(VehicleSync.timestamp.desc()).first())


def _erster_sync(vehicle_id, ab):
    from models.database import VehicleSync
    return (VehicleSync.query
            .filter(VehicleSync.vehicle_id == vehicle_id,
                    VehicleSync.timestamp > ab)
            .order_by(VehicleSync.timestamp.asc()).first())


def beleg_fuer_dieses_auto(wc, vehicle, tol_min):
    """Proof that THIS car was the one drawing — or ``None``.

    Two independent routes, either is enough, neither is a guess:

    A  the car itself reported ``is_charging`` inside the window;
    B  the car stood at home across the whole window — same odometer on
       both sides — and its battery was fuller afterwards. A battery that
       gains while the car does not move was charging; there is no other
       way for it to gain.

    Measured over all 45 readings of one wallbox, the two that provably
    belong to a visitor are refused by both: through the first the house car
    sat at 100 % and never moved, and over the second it LOST charge.
    """
    from models.database import VehicleSync
    ws = datetime.fromtimestamp(int(wc.start_ts))
    we = datetime.fromtimestamp(int(wc.end_ts))
    tol = timedelta(minutes=int(tol_min or 90))
    im_fenster = (VehicleSync.query
                  .filter(VehicleSync.vehicle_id == vehicle.id,
                          VehicleSync.timestamp >= ws,
                          VehicleSync.timestamp <= we)
                  .order_by(VehicleSync.timestamp.asc()).all())
    if any(s.is_charging for s in im_fenster):
        return 'the car reported charging inside this window'

    vor = (VehicleSync.query
           .filter(VehicleSync.vehicle_id == vehicle.id,
                   VehicleSync.timestamp >= ws - tol,
                   VehicleSync.timestamp <= ws)
           .order_by(VehicleSync.timestamp.desc()).first())
    nach = (VehicleSync.query
            .filter(VehicleSync.vehicle_id == vehicle.id,
                    VehicleSync.timestamp >= we,
                    VehicleSync.timestamp <= we + tol)
            .order_by(VehicleSync.timestamp.asc()).first())
    if not _steht_zuhause(vor) or not _steht_zuhause(nach):
        return None
    if (vor.odometer_km is not None and nach.odometer_km is not None
            and nach.odometer_km != vor.odometer_km):
        return None
    if (vor.soc_percent is None or nach.soc_percent is None
            or nach.soc_percent <= vor.soc_percent):
        return None
    return ('the car stood at home and its battery gained %s -> %s %%'
            % (vor.soc_percent, nach.soc_percent))


def aus_der_messung_anlegen(wc, vehicles, cfg, specs=None):
    """Make this reading its own charge entry. Returns ``(charge, reason)``.

    ``charge`` is ``None`` when nothing was filed; ``reason`` then says why,
    in words worth putting in front of the owner.
    """
    from models.database import db, Charge, AppConfig
    # Imported here on purpose: the threshold belongs to the car-side
    # detector and must stay ONE number. A copy in this file would drift.
    from app import _AUTO_CHARGE_MIN_SOC_GAIN

    if len(vehicles) != 1:
        return None, 'more than one car is linked to this wallbox'
    v = vehicles[0]
    bk, eff = (specs or _default_specs)(v.id)
    energie = float(wc.energy_kwh or 0.0)
    if energie <= 0 or not bk:
        return None, 'nothing measured'

    # As large as the smallest thing this app calls a charge. The car-side
    # detector draws that line at a SoC gain of _AUTO_CHARGE_MIN_SOC_GAIN;
    # at the wall the same gain is that share of the battery plus the charge
    # losses. Same bar, measured instead of inferred — on one wallbox it
    # lands at 1.89 kWh and separates eight short plug-ins (0.33 … 1.74 kWh,
    # nobody ever called them charges) from every reading ever matched to
    # one (4.16 kWh and up).
    mindest = _AUTO_CHARGE_MIN_SOC_GAIN / 100.0 * float(bk) / float(eff or 1.0)
    if energie < mindest:
        return None, ('%.3f kWh is under what counts as a charge here (%.2f kWh)'
                      % (energie, mindest))

    we = datetime.fromtimestamp(int(wc.end_ts))
    # 🔴 The car side has to have had its turn first. Its trigger is the sync
    # that reports is_charging=0 after a charge — before that arrives it may
    # still file the entry itself, and the day would end up with two.
    danach = _erster_sync(v.id, we)
    if danach is None:
        return None, 'no sync after this window yet — the car side may still file it'

    beleg = beleg_fuer_dieses_auto(wc, v, cfg.get('tolerance_min'))
    if not beleg:
        return None, 'nothing shows that this car was the one charging'

    # The state of charge it ENDED at — but only when the car demonstrably
    # did not move in between, otherwise that reading is from after a drive.
    bezug = _letzter_sync(v.id, we)
    soc_to = None
    if (danach.soc_percent is not None and _steht_zuhause(danach)
            and bezug is not None and bezug.odometer_km is not None
            and danach.odometer_km == bezug.odometer_km):
        soc_to = danach.soc_percent

    ws = datetime.fromtimestamp(int(wc.start_ts))
    heim = AppConfig.get('home_label', 'Home') or 'Home'
    _lat = AppConfig.get('home_lat', '')
    _lon = AppConfig.get('home_lon', '')
    c = Charge(
        vehicle_id=v.id,
        date=ws.date(),
        charge_hour=ws.hour,
        charge_end_hour=we.hour,
        odometer=(bezug.odometer_km if bezug is not None else None),
        kwh_loaded=round(energie, 3),
        charge_type='AC',
        # 🔴 Left empty on purpose. The whole reason this entry exists is
        # that nobody measured the state of charge before the charge — the
        # app supports an opaque charge, and an invented pair of bounds
        # would poison the SoC statistics and the efficiency base for good.
        soc_from=None,
        soc_to=soc_to,
        location_lat=(float(_lat) if _lat not in (None, '') else None),
        location_lon=(float(_lon) if _lon not in (None, '') else None),
        location_name=heim,
        operator=heim,
        notes=_AUS_MESSUNG_NOTIZ,
        needs_review=True,
    )
    db.session.add(c)
    db.session.flush()            # the id is needed to tie the reading to it
    logger.info(
        "Wallbox link: filed charge %s from reading %s (%.3f kWh, %s - %s) — %s"
        % (c.id, wc.id, energie, ws.strftime('%Y-%m-%d %H:%M'),
           we.strftime('%H:%M'), beleg)
    )
    return c, beleg


def match_all(cfg=None, device_key='', specs=None, pv_co2=None):
    """Re-decide every reading that is not settled yet. Returns a tally.

    Readings already matched are left alone; *ambiguous* and *unmatched* ones
    are retried on every pass, because the missing half often arrives later —
    the car syncs, the entry appears, and the same reading then matches
    cleanly. That is why nothing is ever thrown away for being unmatched.
    """
    from models.database import db, WallboxCharge
    cfg = cfg or settings()
    vehicles = linked_vehicles(device_key)
    tally = {'matched': 0, 'ambiguous': 0, 'unmatched': 0, 'applied': 0,
             'conflict': 0, 'created': 0}
    open_rows = (WallboxCharge.query
                 .filter(WallboxCharge.match_state != 'matched')
                 .order_by(WallboxCharge.start_ts.asc())
                 .all())
    for wc in open_rows:
        state, charge, note = match_one(wc, vehicles, cfg['tolerance_min'])
        # Nobody claimed it. Before it is written off as open, the meter gets
        # to file the charge itself — see aus_der_messung_anlegen. Bound to
        # the same switch that governs taking numbers over: on "annotate
        # only" the link may not create entries either.
        if state == 'unmatched' and _apply_wanted(cfg['apply_mode'], None):
            neu, grund = aus_der_messung_anlegen(wc, vehicles, cfg, specs)
            if neu is not None:
                state, charge, note = 'matched', neu, ''
                tally['created'] += 1
            elif grund:
                # 🔴 Why it was NOT filed belongs in front of the owner. An
                # open reading with no reason beside it is the thing nobody
                # can act on.
                note = '%s; not filed: %s' % (note, grund)
        wc.match_state = state
        wc.match_note = note[:200] if note else None
        wc.matched_at = datetime.now()
        if state == 'matched' and charge is not None:
            bk, eff = (specs or _default_specs)(charge.vehicle_id)
            einwand = contradicts_the_battery(wc, charge, bk)
            if einwand:
                # 🔴 Found the right entry and still writes nothing. The
                # reading claims less energy than the battery gained, so it
                # cannot be the whole charge — and half a charge taken over
                # silently is worse than none, because afterwards the entry
                # looks measured. It stays open, names the entry it belongs
                # to and says why, and every later pass tries again: the log
                # usually answers completely once its samples have settled.
                state = 'conflict'
                wc.match_state = state
                wc.match_note = einwand[:200]
                wc.charge_id = charge.id
                wc.vehicle_id = charge.vehicle_id
                tally[state] = tally.get(state, 0) + 1
                continue
            wc.charge_id = charge.id
            wc.vehicle_id = charge.vehicle_id
            cs, _ = charge_window(charge)
            wc.match_delta_s = int(abs((cs - datetime.fromtimestamp(wc.start_ts))
                                       .total_seconds()))
            charge.wallbox_charge_id = wc.id
            if _apply_wanted(cfg['apply_mode'], charge):
                apply_measurement(wc, charge, bk, eff, pv_co2)
                tally['applied'] += 1
        else:
            wc.charge_id = None
            wc.vehicle_id = None
        tally[state] = tally.get(state, 0) + 1
    db.session.commit()
    return tally


# ══════════════════════════════════════════════════════════════════════════
# One full pass
# ══════════════════════════════════════════════════════════════════════════

def sync(app, days=None, full=False, specs=None, pv_co2=None):
    """Fetch, store, match. Returns a result dict; never raises at the caller."""
    from models.database import AppConfig
    import json as _json
    with app.app_context():
        cfg = settings()
        res = {'ok': False, 'ts': int(time.time()), 'new': 0, 'updated': 0,
               'matched': 0, 'ambiguous': 0, 'unmatched': 0, 'applied': 0,
               'conflict': 0, 'retyped': 0, 'co2': 0,
               'error': None}
        if not configured(cfg):
            res['error'] = 'not configured'
            return res
        try:
            info = probe(cfg)
            dev = str((info.get('wallbox') or {}).get('device_key') or '')
            if days is None:
                last = AppConfig.get(K_LAST_TS, '')
                try:
                    last_ts = int(float(last)) if last else 0
                except (TypeError, ValueError):
                    last_ts = 0
                # A first run, or one after a long outage, walks the configured
                # backfill window; a routine one asks only for what is new.
                if full or not last_ts or (time.time() - last_ts) > cfg['backfill_days'] * 86400:
                    payload = fetch_charges(cfg, days=cfg['backfill_days'])
                else:
                    payload = fetch_charges(cfg, since_ts=since_for(last_ts, info))
            else:
                payload = fetch_charges(cfg, days=int(days))
            neu, upd = store_charges(payload)
            tally = match_all(cfg, dev, specs=specs, pv_co2=pv_co2)
            nach = hole_versaeumtes_nach(cfg['apply_mode'], specs=specs,
                                         pv_co2=pv_co2)
            tally['applied'] = tally.get('applied', 0) + nach['applied']
            tally['retyped'] = nach['retyped']
            tally['co2'] = nach['co2']
            res.update({'ok': True, 'new': neu, 'updated': upd,
                        'pending_settle': int(payload.get('pending_settle') or 0),
                        'wallbox': (payload.get('wallbox') or {}).get('name') or dev})
            res.update({k: tally.get(k, 0)
                        for k in ('matched', 'ambiguous', 'unmatched',
                                  'conflict', 'applied', 'retyped', 'co2',
                                  'created')})
            # 🔴 A charge this link filed itself has no grid intensity yet.
            # The car-side detector fetches one for its own window; the
            # self-healing backfill otherwise only runs at boot — so a
            # meter-filed charge would sit without CO2 until the next
            # restart, which is exactly the kind of half-filled row that
            # looks complete. Rate limited and a no-op when nothing is
            # missing, same call the sync paths already make.
            if tally.get('created'):
                try:
                    from services.co2_backfill import start_backfill
                    start_backfill(app)
                except Exception as e:      # noqa: BLE001
                    logger.warning('CO2 backfill could not be kicked: %s', e)
            AppConfig.set(K_LAST_TS, res['ts'])
        except LinkError as e:
            res['error'] = str(e)
            logger.warning('Wallbox link: %s', e)
        except Exception as e:            # noqa: BLE001 — a background pass must not die
            res['error'] = '%s: %s' % (type(e).__name__, e)
            logger.exception('Wallbox link sync failed')
        try:
            AppConfig.set(K_LAST_RESULT, _json.dumps(res))
        except Exception:
            logger.debug('could not store the link result', exc_info=True)
        return res


def last_result():
    """The last pass, for the settings page. Never raises."""
    from models.database import AppConfig
    import json as _json
    try:
        raw = AppConfig.get(K_LAST_RESULT, '')
        return _json.loads(raw) if raw else None
    except Exception:
        return None


def is_running():
    return _sync_running


def start_sync(app, days=None, full=False, specs=None, pv_co2=None):
    """Run a pass in the background. False if one is already going."""
    global _sync_running, _sync_thread
    with _sync_lock:
        if _sync_running:
            return False
        _sync_running = True

    def _run():
        global _sync_running
        try:
            sync(app, days=days, full=full, specs=specs, pv_co2=pv_co2)
        finally:
            _sync_running = False

    _sync_thread = threading.Thread(target=_run, daemon=True,
                                    name='wallbox-link-sync')
    _sync_thread.start()
    return True


def start_loop(app, interval_s=1800, specs=None, pv_co2=None):
    """A quiet poll while the app runs.

    Half-hourly on purpose: the analyzer withholds a charge until it has been
    over for about twenty minutes anyway, so polling faster only asks the same
    question more often. A charge that finishes now shows up within the hour,
    which is the same order of delay the car's own cloud imposes.
    """
    def _loop():
        # Let the app finish booting — the first pass runs a full backfill and
        # there is no reason for it to compete with startup.
        time.sleep(90)
        while True:
            try:
                with app.app_context():
                    on = configured()
                if on:
                    sync(app, specs=specs, pv_co2=pv_co2)
            except Exception:
                logger.debug('wallbox link loop', exc_info=True)
            time.sleep(max(300, int(interval_s)))

    threading.Thread(target=_loop, daemon=True, name='wallbox-link-loop').start()
