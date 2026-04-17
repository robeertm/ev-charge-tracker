"""Range-bounded aggregations for the interactive /report page.

Distinct from services/report_service.py (the PDF generator) — this
module computes JSON-serialisable stats for a user-picked window
(today, week, month, quarter, half-year, year, custom) and feeds the
Chart.js plots on /report. The PDF generator still exists for the
"Export PDF" button and uses these same aggregations so the two
outputs stay in sync.
"""
from __future__ import annotations

from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from models.database import db, Charge, VehicleTrip, ParkingEvent, AppConfig


DAY_NAMES_DE = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
DAY_NAMES_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def resolve_range(preset: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> tuple[date, date, str]:
    """Turn a preset name (or custom start/end strings) into a concrete
    [start_date, end_date] pair (inclusive) plus a human-readable label.

    Supported presets: day, week, month, quarter, half, year, all, custom.
    'quarter' = current calendar quarter; 'half' = current half-year.
    'all' = from the earliest charge/trip to today.
    Custom requires both start and end in YYYY-MM-DD form.
    """
    today = date.today()
    p = (preset or 'month').lower()

    if p == 'custom' and start and end:
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
            if s > e:
                s, e = e, s
            return s, e, f'{s.isoformat()} – {e.isoformat()}'
        except ValueError:
            pass  # fall through to month

    if p == 'day':
        return today, today, today.isoformat()
    if p == 'week':
        start_dt = today - timedelta(days=today.weekday())  # Monday
        end_dt = start_dt + timedelta(days=6)
        return start_dt, end_dt, f'KW {start_dt.isocalendar()[1]}/{start_dt.year}'
    if p == 'month':
        s = today.replace(day=1)
        last_day = monthrange(today.year, today.month)[1]
        e = today.replace(day=last_day)
        return s, e, s.strftime('%B %Y')
    if p == 'quarter':
        q = (today.month - 1) // 3 + 1
        qs_month = (q - 1) * 3 + 1
        s = date(today.year, qs_month, 1)
        qe_month = qs_month + 2
        last_day = monthrange(today.year, qe_month)[1]
        e = date(today.year, qe_month, last_day)
        return s, e, f'Q{q} {today.year}'
    if p == 'half':
        h = 1 if today.month <= 6 else 2
        s = date(today.year, 1 if h == 1 else 7, 1)
        e = date(today.year, 6, 30) if h == 1 else date(today.year, 12, 31)
        return s, e, f'H{h} {today.year}'
    if p == 'year':
        s = date(today.year, 1, 1)
        e = date(today.year, 12, 31)
        return s, e, str(today.year)
    if p == 'all':
        earliest_charge = (Charge.query
                           .order_by(Charge.date.asc())
                           .with_entities(Charge.date).first())
        earliest_trip = (VehicleTrip.query
                         .order_by(VehicleTrip.trip_date.asc())
                         .with_entities(VehicleTrip.trip_date).first())
        candidates = [d[0] for d in (earliest_charge, earliest_trip) if d and d[0]]
        s = min(candidates) if candidates else today
        return s, today, 'Gesamtzeitraum'

    # Default fallback: current month.
    s = today.replace(day=1)
    last_day = monthrange(today.year, today.month)[1]
    return s, today.replace(day=last_day), s.strftime('%B %Y')


def _bucket_interval(start: date, end: date) -> str:
    """Pick a sensible aggregation bucket size for the chart X-axis.
    Up to ~14 days: daily. Up to ~13 weeks: weekly. Longer: monthly."""
    days = (end - start).days + 1
    if days <= 14:
        return 'day'
    if days <= 95:
        return 'week'
    return 'month'


def _iter_buckets(start: date, end: date, size: str):
    """Yield (bucket_start, bucket_end, label) for each bucket in the
    range. Labels are ISO dates for daily, KW-NN for weekly, yyyy-MM for
    monthly — UI-ready without further formatting."""
    if size == 'day':
        d = start
        while d <= end:
            yield d, d, d.isoformat()
            d = d + timedelta(days=1)
    elif size == 'week':
        d = start - timedelta(days=start.weekday())  # snap to Monday
        while d <= end:
            we = d + timedelta(days=6)
            label_w = d.isocalendar()[1]
            yield max(d, start), min(we, end), f'KW{label_w:02d}'
            d = d + timedelta(days=7)
    else:  # month
        d = start.replace(day=1)
        while d <= end:
            last = monthrange(d.year, d.month)[1]
            me = d.replace(day=last)
            yield max(d, start), min(me, end), d.strftime('%Y-%m')
            # advance to 1st of next month
            if d.month == 12:
                d = date(d.year + 1, 1, 1)
            else:
                d = date(d.year, d.month + 1, 1)


def _safe(v, default=0):
    return default if v is None else v


def build_report(start: date, end: date) -> dict:
    """The heavy lifter — queries Charge + VehicleTrip + ParkingEvent for
    the window and shapes them into the JSON structure the /report page
    consumes. All numbers are rounded server-side so the frontend can
    render without worrying about precision."""
    charges = (Charge.query
               .filter(Charge.date >= start, Charge.date <= end)
               .order_by(Charge.date.asc()).all())
    trips = (VehicleTrip.query
             .filter(VehicleTrip.trip_date >= start, VehicleTrip.trip_date <= end)
             .order_by(VehicleTrip.start_time.asc()).all())

    bucket_size = _bucket_interval(start, end)
    buckets = list(_iter_buckets(start, end, bucket_size))
    bucket_labels = [b[2] for b in buckets]

    # ── Buckets: kWh, cost, CO2, km ─────────────────────────────────
    buck_kwh = [0.0] * len(buckets)
    buck_cost = [0.0] * len(buckets)
    buck_co2 = [0.0] * len(buckets)
    buck_km = [0.0] * len(buckets)
    buck_trips = [0] * len(buckets)
    buck_charges = [0] * len(buckets)

    def _bucket_index(target: date) -> int:
        for i, (bs, be, _) in enumerate(buckets):
            if bs <= target <= be:
                return i
        return -1

    for c in charges:
        i = _bucket_index(c.date)
        if i < 0:
            continue
        buck_kwh[i] += _safe(c.kwh_loaded)
        buck_cost[i] += _safe(c.total_cost)
        buck_co2[i] += _safe(c.co2_kg)
        buck_charges[i] += 1

    for t in trips:
        i = _bucket_index(t.trip_date)
        if i < 0:
            continue
        buck_km[i] += _safe(t.distance_km)
        buck_trips[i] += 1

    # ── Totals ──────────────────────────────────────────────────────
    total_kwh = sum(_safe(c.kwh_loaded) for c in charges)
    total_cost = sum(_safe(c.total_cost) for c in charges)
    total_co2_kg = sum(_safe(c.co2_kg) for c in charges)
    total_km = sum(_safe(t.distance_km) for t in trips)
    total_drive_min = sum(_safe(t.drive_minutes) for t in trips)
    count_charges = len(charges)
    count_trips = len(trips)

    # ICE comparison: user-configurable fossil CO2/km (default 164 g)
    try:
        fossil_co2_per_km = float(AppConfig.get('fossil_co2_per_km', '164'))
    except (TypeError, ValueError):
        fossil_co2_per_km = 164.0
    # ICE fuel equivalent: a modest ICE uses ~7 L/100km * ~1.65 €/L = ~11.55 €/100km
    try:
        ice_cost_per_100km = float(AppConfig.get('ice_cost_per_100km', '11.55'))
    except (TypeError, ValueError):
        ice_cost_per_100km = 11.55

    ice_co2_kg = total_km * fossil_co2_per_km / 1000
    co2_saved_kg = max(ice_co2_kg - total_co2_kg, 0)
    ice_cost = total_km * ice_cost_per_100km / 100
    cost_saved_eur = max(ice_cost - total_cost, 0)

    avg_efficiency = (total_kwh / total_km * 100) if total_km > 0 else 0

    # ── Charge type split (AC/DC/PV) ───────────────────────────────
    type_totals = Counter()
    type_kwh = defaultdict(float)
    type_cost = defaultdict(float)
    for c in charges:
        k = (c.charge_type or 'AC').upper()
        type_totals[k] += 1
        type_kwh[k] += _safe(c.kwh_loaded)
        type_cost[k] += _safe(c.total_cost)

    # ── Hour-of-day charge distribution ────────────────────────────
    hour_kwh = [0.0] * 24
    for c in charges:
        h = c.charge_hour
        if h is None or not (0 <= h < 24):
            continue
        hour_kwh[h] += _safe(c.kwh_loaded)

    # ── Day-of-week pattern ────────────────────────────────────────
    lang = AppConfig.get('app_language', 'de')
    day_names = DAY_NAMES_DE if lang == 'de' else DAY_NAMES_EN
    dow_kwh = [0.0] * 7
    dow_trips = [0] * 7
    dow_km = [0.0] * 7
    for c in charges:
        dow_kwh[c.date.weekday()] += _safe(c.kwh_loaded)
    for t in trips:
        dow_trips[t.trip_date.weekday()] += 1
        dow_km[t.trip_date.weekday()] += _safe(t.distance_km)

    # ── Trip length distribution ───────────────────────────────────
    trip_buckets = [('< 5 km', 0), ('5–20', 0), ('20–50', 0), ('50–100', 0), ('100+', 0)]
    for t in trips:
        d = _safe(t.distance_km)
        if d < 5:        trip_buckets[0] = (trip_buckets[0][0], trip_buckets[0][1] + 1)
        elif d < 20:     trip_buckets[1] = (trip_buckets[1][0], trip_buckets[1][1] + 1)
        elif d < 50:     trip_buckets[2] = (trip_buckets[2][0], trip_buckets[2][1] + 1)
        elif d < 100:    trip_buckets[3] = (trip_buckets[3][0], trip_buckets[3][1] + 1)
        else:            trip_buckets[4] = (trip_buckets[4][0], trip_buckets[4][1] + 1)

    # ── Top operators + stations ───────────────────────────────────
    op_kwh = defaultdict(float); op_cost = defaultdict(float); op_cnt = Counter()
    for c in charges:
        key = (c.operator or '').strip() or '—'
        op_kwh[key] += _safe(c.kwh_loaded)
        op_cost[key] += _safe(c.total_cost)
        op_cnt[key] += 1
    top_operators = sorted(op_kwh.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top_operators = [{'name': k, 'kwh': round(v, 1), 'cost': round(op_cost[k], 2),
                      'count': op_cnt[k]} for k, v in top_operators if v > 0]

    # ── Top destinations from parking events (not range-filtered
    # perfectly, but close: we look at arrivals within the window) ──
    pe_rows = (ParkingEvent.query
               .filter(ParkingEvent.arrived_at >= datetime.combine(start, datetime.min.time()),
                       ParkingEvent.arrived_at <= datetime.combine(end, datetime.max.time()))
               .all())
    loc_cnt = Counter()
    for pe in pe_rows:
        if pe.favorite_name:
            loc_cnt[pe.favorite_name] += 1
        elif pe.label in ('home', 'work'):
            loc_cnt[pe.label.capitalize()] += 1
        elif pe.address:
            loc_cnt[pe.address[:40]] += 1
    top_locations = [{'name': k, 'count': v}
                     for k, v in loc_cnt.most_common(8)]

    # ── Price development (per-charge avg EUR/kWh over time) ───────
    price_points = [{'x': c.date.isoformat(), 'y': round(c.eur_per_kwh, 4)}
                    for c in charges
                    if c.eur_per_kwh is not None and c.eur_per_kwh > 0]

    # ── SoC usage depth per charge ─────────────────────────────────
    soc_points = [{'x': c.date.isoformat(),
                   'from': c.soc_from, 'to': c.soc_to}
                  for c in charges
                  if c.soc_from is not None and c.soc_to is not None]

    # ── Efficiency over time (kWh/100km, one point per bucket) ─────
    efficiency_by_bucket = []
    for i, lbl in enumerate(bucket_labels):
        if buck_km[i] > 0:
            efficiency_by_bucket.append({'x': lbl, 'y': round(buck_kwh[i] / buck_km[i] * 100, 2)})

    # ── Biggest / fastest / longest (highlights for the window) ────
    def _longest_trip():
        if not trips: return None
        t = max(trips, key=lambda x: _safe(x.distance_km))
        return {'date': t.trip_date.isoformat(), 'km': round(_safe(t.distance_km), 1),
                'drive_min': t.drive_minutes, 'max_speed': t.max_speed_kmh}
    def _biggest_charge():
        if not charges: return None
        c = max(charges, key=lambda x: _safe(x.kwh_loaded))
        return {'date': c.date.isoformat(), 'kwh': round(_safe(c.kwh_loaded), 1),
                'cost': round(_safe(c.total_cost), 2), 'type': c.charge_type}
    def _cheapest_charge():
        priced = [c for c in charges if c.eur_per_kwh and c.eur_per_kwh > 0]
        if not priced: return None
        c = min(priced, key=lambda x: x.eur_per_kwh)
        return {'date': c.date.isoformat(), 'eur_per_kwh': round(c.eur_per_kwh, 4),
                'kwh': round(_safe(c.kwh_loaded), 1)}

    return {
        'range': {
            'start': start.isoformat(),
            'end': end.isoformat(),
            'bucket': bucket_size,
            'days': (end - start).days + 1,
        },
        'summary': {
            'total_kwh': round(total_kwh, 1),
            'total_cost': round(total_cost, 2),
            'total_co2_kg': round(total_co2_kg, 1),
            'total_km': round(total_km, 1),
            'total_drive_min': total_drive_min,
            'count_charges': count_charges,
            'count_trips': count_trips,
            'avg_efficiency_kwh_per_100km': round(avg_efficiency, 2),
            'avg_eur_per_kwh': round(total_cost / total_kwh, 4) if total_kwh > 0 else 0,
            'avg_eur_per_100km': round(total_cost / total_km * 100, 2) if total_km > 0 else 0,
            'ice_co2_kg': round(ice_co2_kg, 1),
            'co2_saved_kg': round(co2_saved_kg, 1),
            'ice_cost_eur': round(ice_cost, 2),
            'cost_saved_eur': round(cost_saved_eur, 2),
        },
        'series': {
            'bucket_labels': bucket_labels,
            'kwh': [round(v, 2) for v in buck_kwh],
            'cost': [round(v, 2) for v in buck_cost],
            'co2_kg': [round(v, 2) for v in buck_co2],
            'km': [round(v, 1) for v in buck_km],
            'trips_count': buck_trips,
            'charges_count': buck_charges,
        },
        'charge_type': {
            'labels': list(type_totals.keys()),
            'counts': list(type_totals.values()),
            'kwh': [round(type_kwh[k], 1) for k in type_totals.keys()],
            'cost': [round(type_cost[k], 2) for k in type_totals.keys()],
        },
        'hour_of_day': {
            'labels': [f'{h:02d}' for h in range(24)],
            'kwh': [round(v, 1) for v in hour_kwh],
        },
        'day_of_week': {
            'labels': day_names,
            'kwh': [round(v, 1) for v in dow_kwh],
            'trips': dow_trips,
            'km': [round(v, 1) for v in dow_km],
        },
        'trip_length_dist': {
            'labels': [b[0] for b in trip_buckets],
            'counts': [b[1] for b in trip_buckets],
        },
        'top_operators': top_operators,
        'top_locations': top_locations,
        'price_points': price_points,
        'soc_points': soc_points,
        'efficiency_by_bucket': efficiency_by_bucket,
        'highlights': {
            'longest_trip': _longest_trip(),
            'biggest_charge': _biggest_charge(),
            'cheapest_charge': _cheapest_charge(),
        },
    }
