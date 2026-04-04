"""Statistics and aggregation service."""
from datetime import date
from sqlalchemy import func, extract
from models.database import db, Charge


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

    # CO2 equivalent for fossil car (assuming 120g/km average)
    # and 19.86 kWh/100km for the EV
    avg_consumption = total_kwh / 1 if len(charges) == 0 else total_kwh
    fossil_co2_per_km = 0.12  # 120g CO2/km for average fossil car
    # Approximate km from kWh (using ~20 kWh/100km)
    est_km = total_kwh / 0.20

    return {
        'total_charges': len(charges),
        'total_kwh': round(total_kwh, 1),
        'total_cost': round(total_cost, 2),
        'total_co2_kg': round(total_co2, 2),
        'fossil_co2_kg': round(est_km * fossil_co2_per_km, 2),
        'co2_savings_pct': round((1 - total_co2 / (est_km * fossil_co2_per_km)) * 100, 1) if est_km > 0 and total_co2 > 0 else 0,
        'avg_eur_per_kwh': round(avg_eur, 2),
        'ac_count': ac_count,
        'dc_count': dc_count,
        'first_charge': min(c.date for c in charges) if charges else None,
        'last_charge': max(c.date for c in charges) if charges else None,
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
    ).group_by(
        extract('year', Charge.date),
        extract('month', Charge.date)
    ).order_by(
        extract('year', Charge.date),
        extract('month', Charge.date)
    ).all()

    months = []
    for r in results:
        months.append({
            'year': int(r.year),
            'month': int(r.month),
            'label': f"{int(r.month):02d}/{int(r.year)}",
            'kwh': round(r.kwh or 0, 1),
            'cost': round(r.cost or 0, 2),
            'co2': round(r.co2 or 0, 2),
            'count': r.count,
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

    return [{
        'year': int(r.year),
        'kwh': round(r.kwh or 0, 1),
        'cost': round(r.cost or 0, 2),
        'co2': round(r.co2 or 0, 2),
        'count': r.count,
    } for r in results]


def get_ac_dc_stats():
    """Get AC vs DC comparison stats."""
    stats = {}
    for ct in ['AC', 'DC']:
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

    return {
        'monthly_labels': [m['label'] for m in monthly],
        'monthly_cost': [m['cost'] for m in monthly],
        'monthly_kwh': [m['kwh'] for m in monthly],
        'monthly_count': [m['count'] for m in monthly],
        'monthly_cost_per_kwh': [m['cost_per_kwh'] for m in monthly],
        'cumulative_labels': [c['label'] for c in cumulative],
        'cumulative_cost': [c['cost'] for c in cumulative],
        'cumulative_kwh': [c['kwh'] for c in cumulative],
    }
