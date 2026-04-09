"""Statistics and aggregation service."""
from datetime import date, datetime, timedelta
from sqlalchemy import func, extract
from models.database import db, Charge, ThgQuota, AppConfig, VehicleSync


def get_summary_stats():
    """Get overall summary statistics."""
    charges = Charge.query.all()
    if not charges:
        return {}

    total_kwh = sum(c.kwh_loaded or 0 for c in charges)
    total_cost = sum(c.total_cost or 0 for c in charges)
    total_co2 = sum(c.co2_kg or 0 for c in charges)
    ac_count = sum(1 for c in charges if c.charge_type == 'AC')
    dc_count = sum(1 for c in charges if c.charge_type == 'DC')
    avg_eur = total_cost / total_kwh if total_kwh > 0 else 0

    total_thg = sum(t.amount_eur for t in ThgQuota.query.all())
    net_cost = total_cost - total_thg

    # Total km = last (highest) odometer reading
    odo_values = [c.odometer for c in charges if c.odometer]
    total_km = max(odo_values) if odo_values else 0

    # Config values
    try:
        battery_kwh = float(AppConfig.get('battery_kwh', '64'))
    except (ValueError, TypeError):
        battery_kwh = 64.0
    try:
        recup_kwh_per_km = float(AppConfig.get('recuperation_kwh_per_km', '0.086'))
    except (ValueError, TypeError):
        recup_kwh_per_km = 0.086

    # Recuperation & consumption
    total_recuperation = round(total_km * recup_kwh_per_km, 1) if total_km > 0 else 0
    # Verbrauch mit Rekup. = was aus dem Netz geladen wurde pro 100km
    consumption_with_recup = round(total_kwh / (total_km / 100), 3) if total_km > 0 else 0
    # Verbrauch ohne Rekup. = tatsächlicher Gesamtverbrauch des Autos pro 100km
    consumption_without_recup = round((total_kwh + total_recuperation) / (total_km / 100), 3) if total_km > 0 else 0
    # km extra durch Rekuperation
    recup_extra_km = round(total_recuperation / (consumption_without_recup / 100), 0) if consumption_without_recup > 0 else 0
    # Ladezyklen
    charge_cycles = round(total_kwh / battery_kwh, 1) if battery_kwh > 0 else 0
    recup_cycles = round(total_recuperation / battery_kwh, 1) if battery_kwh > 0 else 0
    # Kosten pro 100km
    cost_per_100km = round(total_cost / total_km * 100, 2) if total_km > 0 else 0
    net_cost_per_100km = round((total_cost - total_thg) / total_km * 100, 2) if total_km > 0 else 0

    # CO2 comparison — Well-to-Wheel
    try:
        fossil_co2_g_per_km = float(AppConfig.get('fossil_co2_per_km', '164'))
    except (ValueError, TypeError):
        fossil_co2_g_per_km = 164.0
    fossil_co2_per_km = fossil_co2_g_per_km / 1000  # g → kg
    km_for_co2 = total_km if total_km > 0 else total_kwh * 5
    fossil_co2_kg = km_for_co2 * fossil_co2_per_km

    return {
        'total_charges': len(charges),
        'total_kwh': round(total_kwh, 1),
        'total_cost': round(total_cost, 2),
        'total_thg_eur': round(total_thg, 2),
        'net_cost': round(net_cost, 2),
        'total_co2_kg': round(total_co2, 2),
        'fossil_co2_kg': round(fossil_co2_kg, 2),
        'co2_savings_pct': round((1 - total_co2 / fossil_co2_kg) * 100, 1) if fossil_co2_kg > 0 and total_co2 > 0 else 0,
        'avg_eur_per_kwh': round(avg_eur, 2),
        'ac_count': ac_count,
        'dc_count': dc_count,
        'first_charge': min(c.date for c in charges) if charges else None,
        'last_charge': max(c.date for c in charges) if charges else None,
        'total_km': total_km,
        'consumption_with_recup': consumption_with_recup,
        'consumption_without_recup': consumption_without_recup,
        'total_recuperation': total_recuperation,
        'recup_extra_km': int(recup_extra_km),
        'charge_cycles': charge_cycles,
        'recup_cycles': recup_cycles,
        'cost_per_100km': cost_per_100km,
        'net_cost_per_100km': net_cost_per_100km,
    }


def get_monthly_stats():
    """Get monthly aggregated statistics."""
    results = db.session.query(
        extract('year', Charge.date).label('year'),
        extract('month', Charge.date).label('month'),
        func.sum(Charge.kwh_loaded).label('kwh'),
        func.sum(Charge.total_cost).label('cost'),
        func.sum(Charge.co2_kg).label('co2'),
        func.count(Charge.id).label('count'),
        func.avg(Charge.loss_pct).label('avg_loss_pct'),
        func.min(Charge.odometer).label('odo_min'),
        func.max(Charge.odometer).label('odo_max'),
    ).group_by(
        extract('year', Charge.date),
        extract('month', Charge.date)
    ).order_by(
        extract('year', Charge.date),
        extract('month', Charge.date)
    ).all()

    months = []
    for r in results:
        km = (r.odo_max - r.odo_min) if r.odo_min and r.odo_max and r.odo_max > r.odo_min else 0
        months.append({
            'year': int(r.year),
            'month': int(r.month),
            'label': f"{int(r.month):02d}/{int(r.year)}",
            'kwh': round(r.kwh or 0, 1),
            'cost': round(r.cost or 0, 2),
            'co2': round(r.co2 or 0, 2),
            'count': r.count,
            'km': km,
            'avg_loss_pct': round(r.avg_loss_pct or 0, 1),
            'cost_per_kwh': round((r.cost / r.kwh), 2) if r.kwh and r.kwh > 0 else 0,
        })
    return months


def get_yearly_stats():
    """Get yearly aggregated statistics."""
    results = db.session.query(
        extract('year', Charge.date).label('year'),
        func.sum(Charge.kwh_loaded).label('kwh'),
        func.sum(Charge.total_cost).label('cost'),
        func.sum(Charge.co2_kg).label('co2'),
        func.count(Charge.id).label('count'),
    ).group_by(
        extract('year', Charge.date)
    ).order_by(
        extract('year', Charge.date)
    ).all()

    thg_map = {}
    for t in ThgQuota.query.all():
        for y in range(t.year_from, t.year_to + 1):
            thg_map[y] = thg_map.get(y, 0) + t.amount_eur / (t.year_to - t.year_from + 1)

    return [{
        'year': int(r.year),
        'kwh': round(r.kwh or 0, 1),
        'cost': round(r.cost or 0, 2),
        'co2': round(r.co2 or 0, 2),
        'count': r.count,
        'thg': round(thg_map.get(int(r.year), 0), 2),
        'net_cost': round((r.cost or 0) - thg_map.get(int(r.year), 0), 2),
    } for r in results]


def get_ac_dc_stats():
    """Get AC vs DC comparison stats."""
    stats = {}
    for ct in ['AC', 'DC', 'PV']:
        charges = Charge.query.filter_by(charge_type=ct).all()
        if charges:
            total_kwh = sum(c.kwh_loaded or 0 for c in charges)
            total_cost = sum(c.total_cost or 0 for c in charges)
            stats[ct] = {
                'count': len(charges),
                'total_kwh': round(total_kwh, 1),
                'total_cost': round(total_cost, 2),
                'avg_eur_per_kwh': round(total_cost / total_kwh, 2) if total_kwh > 0 else 0,
                'avg_kwh_per_charge': round(total_kwh / len(charges), 1),
                'avg_loss_pct': round(sum(c.loss_pct or 0 for c in charges if c.loss_pct) / max(sum(1 for c in charges if c.loss_pct), 1), 1),
            }
    return stats


def get_vehicle_history(days=None):
    """Return time series of tracked vehicle metrics from VehicleSync rows.

    days: optional, limit to last N days. None = all history.
    """
    q = VehicleSync.query.order_by(VehicleSync.timestamp.asc())
    if days:
        cutoff = datetime.now() - timedelta(days=days)
        q = q.filter(VehicleSync.timestamp >= cutoff)
    rows = q.all()
    if not rows:
        return None

    series = {
        'timestamps': [r.timestamp.isoformat() for r in rows],
        'soc': [r.soc_percent for r in rows],
        'range_km': [r.estimated_range_km for r in rows],
        'odometer_km': [r.odometer_km for r in rows],
        'battery_12v': [r.battery_12v_percent for r in rows],
        'soh': [r.battery_soh_percent for r in rows],
        'regen_kwh': [r.total_regenerated_kwh for r in rows],
        'consumption_30d': [r.consumption_30d_kwh_per_100km for r in rows],
        'lat': [r.location_lat for r in rows],
        'lon': [r.location_lon for r in rows],
    }

    last = rows[-1]
    summary = {
        'count': len(rows),
        'first_seen': rows[0].timestamp.isoformat(),
        'last_seen': last.timestamp.isoformat(),
        'last': {
            'soc': last.soc_percent,
            'range_km': last.estimated_range_km,
            'odometer_km': last.odometer_km,
            'battery_12v': last.battery_12v_percent,
            'soh': last.battery_soh_percent,
            'regen_kwh': last.total_regenerated_kwh,
            'consumption_30d': last.consumption_30d_kwh_per_100km,
            'lat': last.location_lat,
            'lon': last.location_lon,
        },
    }

    # Compute deltas from first → last where meaningful
    def _first_non_null(values):
        for v in values:
            if v is not None:
                return v
        return None

    def _last_non_null(values):
        for v in reversed(values):
            if v is not None:
                return v
        return None

    odo_first = _first_non_null(series['odometer_km'])
    odo_last = _last_non_null(series['odometer_km'])
    summary['km_driven'] = (odo_last - odo_first) if (odo_first and odo_last) else 0

    soh_first = _first_non_null(series['soh'])
    soh_last = _last_non_null(series['soh'])
    summary['soh_delta'] = round(soh_last - soh_first, 2) if (soh_first is not None and soh_last is not None) else None

    regen_first = _first_non_null(series['regen_kwh'])
    regen_last = _last_non_null(series['regen_kwh'])
    summary['regen_delta'] = round(regen_last - regen_first, 1) if (regen_first is not None and regen_last is not None) else None

    return {'series': series, 'summary': summary}


def get_chart_data():
    """Get data formatted for Chart.js."""
    monthly = get_monthly_stats()

    # Cumulative data
    cum_cost = 0
    cum_kwh = 0
    cumulative = []
    for m in monthly:
        cum_cost += m['cost']
        cum_kwh += m['kwh']
        cumulative.append({'label': m['label'], 'cost': round(cum_cost, 2), 'kwh': round(cum_kwh, 1)})

    # Cumulative CO2 and CO2 savings — Well-to-Wheel
    try:
        fossil_co2_g_per_km = float(AppConfig.get('fossil_co2_per_km', '164'))
    except (ValueError, TypeError):
        fossil_co2_g_per_km = 164.0
    fossil_co2_per_km = fossil_co2_g_per_km / 1000  # g → kg
    total_kwh_all = sum(m['kwh'] for m in monthly)
    odo_max = 0
    for c in Charge.query.all():
        if c.odometer and c.odometer > odo_max:
            odo_max = c.odometer
    # km per kWh ratio from odometer, or fallback
    km_per_kwh = odo_max / total_kwh_all if odo_max > 0 and total_kwh_all > 0 else 5.0

    cum_co2 = 0
    cum_savings = 0
    cumulative_co2 = []
    cumulative_co2_savings = []
    for m in monthly:
        cum_co2 += m['co2']
        est_km = m['kwh'] * km_per_kwh
        fossil_co2 = est_km * fossil_co2_per_km
        cum_savings += (fossil_co2 - m['co2'])
        cumulative_co2.append(round(cum_co2, 2))
        cumulative_co2_savings.append(round(cum_savings, 2))

    # Battery production CO2 for break-even line
    try:
        battery_kwh = float(AppConfig.get('battery_kwh', '64'))
    except (ValueError, TypeError):
        battery_kwh = 64.0
    try:
        co2_per_kwh = float(AppConfig.get('battery_co2_per_kwh', '100'))
    except (ValueError, TypeError):
        co2_per_kwh = 100.0
    battery_production_co2 = round(battery_kwh * co2_per_kwh, 0)

    return {
        'monthly_labels': [m['label'] for m in monthly],
        'monthly_cost': [m['cost'] for m in monthly],
        'monthly_kwh': [m['kwh'] for m in monthly],
        'monthly_co2': [m['co2'] for m in monthly],
        'monthly_count': [m['count'] for m in monthly],
        'monthly_cost_per_kwh': [m['cost_per_kwh'] for m in monthly],
        'cumulative_labels': [c['label'] for c in cumulative],
        'cumulative_cost': [c['cost'] for c in cumulative],
        'cumulative_kwh': [c['kwh'] for c in cumulative],
        'cumulative_co2': cumulative_co2,
        'cumulative_co2_savings': cumulative_co2_savings,
        'battery_production_co2': battery_production_co2,
    }
