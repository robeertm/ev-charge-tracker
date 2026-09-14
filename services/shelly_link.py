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

    Idempotent by ``(device_key, source_id)``: the analyzer only hands out
    charges that are over, so their ids no longer move and a re-poll of the same
    window finds the same rows instead of duplicating them.
    """
    from models.database import db, WallboxCharge
    key_default = str((payload.get('wallbox') or {}).get('device_key') or '')
    neu = upd = 0
    for c in payload.get('charges') or []:
        sid = str(c.get('id') or '').strip()
        if not sid:
            continue
        dev = str(c.get('device_key') or key_default)
        row = WallboxCharge.query.filter_by(device_key=dev, source_id=sid).first()
        if row is None:
            row = WallboxCharge(device_key=dev, source_id=sid,
                                match_state='unmatched')
            db.session.add(row)
            neu += 1
        else:
            upd += 1
        row.start_ts = int(c.get('start_ts') or 0)
        row.end_ts = int(c.get('end_ts') or 0)
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
    if wc.prev_co2_g_per_kwh is None:
        wc.prev_co2_g_per_kwh = charge.co2_g_per_kwh
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
    _pv_g = pv_co2() if callable(pv_co2) else pv_co2
    _misch = co2_aus_mischung(wc, wc.prev_co2_g_per_kwh, _pv_g)
    if _misch is not None:
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
    tally = {'applied': 0, 'retyped': 0}
    for wc in rows:
        charge = Charge.query.get(wc.charge_id)
        if charge is None:
            continue
        if wc.applied_at is None:
            bk, eff = (specs or _default_specs)(charge.vehicle_id)
            apply_measurement(wc, charge, bk, eff, pv_co2)
            tally['applied'] += 1
        elif wc.prev_needs_review is None:
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
    if tally['applied'] or tally['retyped']:
        db.session.commit()
        logger.info('Wallbox link: caught up %d adoption(s), %d re-typed',
                    tally['applied'], tally['retyped'])
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
    tally = {'matched': 0, 'ambiguous': 0, 'unmatched': 0, 'applied': 0}
    open_rows = (WallboxCharge.query
                 .filter(WallboxCharge.match_state != 'matched')
                 .order_by(WallboxCharge.start_ts.asc())
                 .all())
    for wc in open_rows:
        state, charge, note = match_one(wc, vehicles, cfg['tolerance_min'])
        wc.match_state = state
        wc.match_note = note[:200] if note else None
        wc.matched_at = datetime.now()
        if state == 'matched' and charge is not None:
            wc.charge_id = charge.id
            wc.vehicle_id = charge.vehicle_id
            cs, _ = charge_window(charge)
            wc.match_delta_s = int(abs((cs - datetime.fromtimestamp(wc.start_ts))
                                       .total_seconds()))
            charge.wallbox_charge_id = wc.id
            if _apply_wanted(cfg['apply_mode'], charge):
                bk, eff = (specs or _default_specs)(charge.vehicle_id)
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
               'retyped': 0,
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
                    payload = fetch_charges(cfg, since_ts=last_ts)
            else:
                payload = fetch_charges(cfg, days=int(days))
            neu, upd = store_charges(payload)
            tally = match_all(cfg, dev, specs=specs, pv_co2=pv_co2)
            nach = hole_versaeumtes_nach(cfg['apply_mode'], specs=specs,
                                         pv_co2=pv_co2)
            tally['applied'] = tally.get('applied', 0) + nach['applied']
            tally['retyped'] = nach['retyped']
            res.update({'ok': True, 'new': neu, 'updated': upd,
                        'pending_settle': int(payload.get('pending_settle') or 0),
                        'wallbox': (payload.get('wallbox') or {}).get('name') or dev})
            res.update({k: tally.get(k, 0)
                        for k in ('matched', 'ambiguous', 'unmatched',
                                  'applied', 'retyped')})
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
