# -*- coding: utf-8 -*-
"""Find sync rows that cannot belong to the vehicle they are filed under.

Until v3.0.125 the dashboard asked the FIRST vehicle for live data no
matter which car the picker was on, and then filed the answer under the
**picked** car. On a two-car installation that wrote one vehicle's
odometer into the other vehicle's history — and the parking-event state
machine and the charge auto-detect both run on every stored row, so the
mistake could travel further than the charts.

v3.0.125 stopped it happening. It could not undo what had already been
written, and there was no way to even see it: the raw-data list showed
every vehicle's rows mixed together with nothing saying which car each
one came from.

## How a wrong row is recognised

Not by guessing, and not by brand-specific payload shapes: **an odometer
only ever goes up.** Within one vehicle, ordered by time, a reading that
is lower than a reading before it is impossible. A foreign row therefore
announces itself twice over — it jumps away from the vehicle's own
mileage, and the next genuine row drops back.

That test needs no knowledge of brands, no VIN in the payload and no
threshold anyone has to tune. It is the vehicle's own history judging
itself, which is why it is also honest about its limits:

* Two cars with **almost the same mileage** produce rows that are wrong
  but not impossible, and those are not detected. The check reports what
  it can prove, and says so rather than implying completeness.
* A genuine odometer correction by hand looks the same. That is why
  nothing is deleted automatically and every row is shown with its
  values before anyone confirms.
* **A cached reading re-served after a fresher one looks the same too**,
  and some brands do that routinely. What separates the two is the
  SIZE of the step: a stale echo is a few kilometres, another car is
  usually thousands. So every finding carries ``delta_km`` and the list
  leads with the largest — the number a person needs in order to judge
  it, rather than a verdict the data cannot support.

A tolerance of one kilometre absorbs rounding between brands that report
whole kilometres and brands that report metres.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Below this, a backwards step is rounding, not a foreign vehicle.
TOLERANZ_KM = 1


def _rows_of(vehicle_id: int) -> list:
    from models.database import VehicleSync
    return (VehicleSync.query
            .filter(VehicleSync.vehicle_id == vehicle_id)
            .filter(VehicleSync.odometer_km.isnot(None))
            .order_by(VehicleSync.timestamp.asc(), VehicleSync.id.asc())
            .all())


def implausible_rows(vehicle_id: int) -> List[dict]:
    """Rows whose odometer breaks the "mileage only rises" rule.

    Walks forward keeping the highest reading seen so far. A row below
    that high-water mark is impossible; a row above it that the NEXT row
    falls back from is the other half of the same event — the foreign
    reading itself. Reporting only the first kind would leave the actual
    intruder in place and delete the innocent row after it.
    """
    rows = _rows_of(vehicle_id)
    if len(rows) < 2:
        return []

    verdaechtig: Dict[int, dict] = {}
    hoechster = rows[0].odometer_km
    hoechster_idx = 0
    for i in range(1, len(rows)):
        km = rows[i].odometer_km
        if km + TOLERANZ_KM < hoechster:
            # Either this row is foreign, or the peak before it was.
            # The peak is the suspect when the mileage returns to the
            # level it had before the peak — one row up and back down.
            vorher = rows[hoechster_idx - 1].odometer_km if hoechster_idx > 0 else None
            spitze_ist_ausreisser = (
                vorher is not None and abs(km - vorher) <= max(TOLERANZ_KM,
                                                              abs(hoechster - km) * 0.05))
            if spitze_ist_ausreisser:
                r = rows[hoechster_idx]
                verdaechtig[r.id] = _describe(r, hoechster, km, 'peak')
                hoechster = km
                hoechster_idx = i
            else:
                r = rows[i]
                verdaechtig[r.id] = _describe(r, hoechster, km, 'drop')
        elif km > hoechster:
            hoechster = km
            hoechster_idx = i
    # Largest discrepancy first: that is the order in which a person can
    # decide fastest, and the obvious cases stop hiding under the noise.
    return sorted(verdaechtig.values(), key=lambda r: -r['delta_km'])


def _describe(row, hoechster, km, art: str) -> dict:
    import json as _json
    marke = ''
    try:
        raw = _json.loads(row.raw_json or '{}')
        if isinstance(raw, dict):
            # Only as a hint in the listing — never as the criterion.
            marke = str(raw.get('vin') or raw.get('VIN') or '')[:20]
    except (ValueError, TypeError):
        pass
    erwartet = km if art == 'peak' else hoechster
    return {
        'id': row.id,
        'timestamp': row.timestamp.isoformat() if row.timestamp else None,
        'odometer_km': row.odometer_km,
        'soc_percent': row.soc_percent,
        'battery_12v_percent': row.battery_12v_percent,
        'expected_around_km': erwartet,
        # How far off it is. A handful of kilometres is almost always a
        # cached reading being re-served; thousands is another car.
        'delta_km': abs((row.odometer_km or 0) - (erwartet or 0)),
        'kind': art,
        'vin_in_payload': marke,
    }


def audit(vehicle_ids: Optional[List[int]] = None) -> dict:
    """Report per vehicle. ``{'vehicles': [...], 'total': n}``."""
    from models.database import Vehicle
    if vehicle_ids is None:
        fahrzeuge = Vehicle.query.order_by(Vehicle.id.asc()).all()
    else:
        fahrzeuge = [Vehicle.query.get(v) for v in vehicle_ids]
    out, gesamt = [], 0
    for v in fahrzeuge:
        if v is None:
            continue
        treffer = implausible_rows(v.id)
        gesamt += len(treffer)
        out.append({'vehicle_id': v.id, 'name': v.name, 'rows': treffer})
    return {'vehicles': out, 'total': gesamt}


def delete_rows(vehicle_id: int, row_ids: List[int]) -> dict:
    """Delete named rows of ONE vehicle, and only ones the audit flags.

    Both restrictions matter. Scoping to a vehicle means a stale page
    cannot delete another car's history; re-running the audit means a
    list the user has been carrying around since before a new sync
    cannot delete a row that is fine today.
    """
    from models.database import db, VehicleSync
    erlaubt = {r['id'] for r in implausible_rows(vehicle_id)}
    ziel = [i for i in row_ids if i in erlaubt]
    abgelehnt = [i for i in row_ids if i not in erlaubt]
    if ziel:
        (VehicleSync.query
         .filter(VehicleSync.vehicle_id == vehicle_id)
         .filter(VehicleSync.id.in_(ziel))
         .delete(synchronize_session=False))
        db.session.commit()
        logger.warning('sync audit: removed %d implausible row(s) from '
                       'vehicle %s', len(ziel), vehicle_id)
    return {'deleted': len(ziel), 'refused': abgelehnt}
