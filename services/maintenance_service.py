"""Maintenance log service."""
from datetime import date, timedelta
from typing import Optional

from models.database import db, MaintenanceEntry


# Default reminder intervals (used as suggestions in the UI form)
DEFAULT_INTERVALS = {
    'inspection': {'months': 12, 'km': 30000},
    'tires_winter': {'months': 6, 'km': None},
    'tires_summer': {'months': 6, 'km': None},
    'brakes': {'months': None, 'km': 50000},
    'wiper': {'months': 12, 'km': None},
    'battery_12v': {'months': 36, 'km': None},
    'tuv': {'months': 24, 'km': None},
    'cabin_filter': {'months': 12, 'km': 15000},
}


def add_entry(date_, item_type, title=None, odometer_km=None, cost_eur=None,
              notes=None, next_due_km=None, next_due_date=None):
    entry = MaintenanceEntry(
        date=date_,
        item_type=item_type,
        title=title,
        odometer_km=odometer_km,
        cost_eur=cost_eur,
        notes=notes,
        next_due_km=next_due_km,
        next_due_date=next_due_date,
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def update_entry(entry_id: int, **fields) -> Optional[MaintenanceEntry]:
    entry = MaintenanceEntry.query.get(entry_id)
    if not entry:
        return None
    for k, v in fields.items():
        if hasattr(entry, k):
            setattr(entry, k, v)
    db.session.commit()
    return entry


def delete_entry(entry_id: int) -> bool:
    entry = MaintenanceEntry.query.get(entry_id)
    if not entry:
        return False
    db.session.delete(entry)
    db.session.commit()
    return True


def list_entries():
    return MaintenanceEntry.query.order_by(MaintenanceEntry.date.desc()).all()


def get_due_items(current_odo_km: Optional[int] = None):
    """Return items due within the next 30 days or 1500 km.

    Each item is returned with a `severity`: 'overdue' | 'due_soon' | 'ok'.
    """
    today = date.today()
    out = []
    for entry in MaintenanceEntry.query.all():
        severity = None
        reasons = []

        from services.i18n import t as _t
        if entry.next_due_date is not None:
            days_left = (entry.next_due_date - today).days
            if days_left < 0:
                severity = 'overdue'
                reasons.append(_t('maint.overdue_days', days=abs(days_left)))
            elif days_left <= 30:
                severity = severity or 'due_soon'
                reasons.append(_t('maint.due_in_days', days=days_left))

        if entry.next_due_km is not None and current_odo_km is not None:
            km_left = entry.next_due_km - current_odo_km
            if km_left < 0:
                severity = 'overdue'
                reasons.append(_t('maint.overdue_km', km=f'{abs(km_left):,}'.replace(',', '.')))
            elif km_left <= 1500:
                severity = severity or 'due_soon'
                reasons.append(_t('maint.due_in_km', km=f'{km_left:,}'.replace(',', '.')))

        if severity is not None:
            out.append({
                'id': entry.id,
                'item_type': entry.item_type,
                'title': entry.title or entry.item_type,
                'severity': severity,
                'reasons': reasons,
                'next_due_km': entry.next_due_km,
                'next_due_date': entry.next_due_date.isoformat() if entry.next_due_date else None,
            })

    # overdue first
    out.sort(key=lambda x: 0 if x['severity'] == 'overdue' else 1)
    return out


def get_summary():
    entries = MaintenanceEntry.query.all()
    total_cost = sum((e.cost_eur or 0) for e in entries)
    return {
        'count': len(entries),
        'total_cost': round(total_cost, 2),
        'last_inspection': max(
            (e.date for e in entries if e.item_type == 'inspection'),
            default=None,
        ),
    }
