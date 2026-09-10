# -*- coding: utf-8 -*-
"""Can we find the rows the picker bug wrote into the wrong car?

Until v3.0.125 the dashboard asked the first vehicle and filed the answer
under the picked one, so one car's odometer landed in the other car's
history. The cause is fixed; the rows written before it are still there
and nothing could even show them.

The check is the vehicle's own history judging itself: mileage only ever
rises, so a reading below one that came earlier is impossible.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def app_ctx():
    d = tempfile.mkdtemp(prefix='evct-audit-')
    os.environ['EV_DATA_DIR'] = d
    os.environ.setdefault('SECRET_KEY', 'audit-test')
    for mod in [m for m in list(sys.modules) if m.startswith(('app', 'config', 'models'))]:
        sys.modules.pop(mod, None)
    from app import create_app
    a = create_app()
    with a.app_context():
        yield a


def _fleet(anzahl=2):
    from models.database import db, Vehicle
    v1 = Vehicle.query.order_by(Vehicle.id.asc()).first()
    v1.name = 'Kona'
    v1.is_archived = False
    autos = [v1]
    for i in range(1, anzahl):
        v = Vehicle(name=f'Auto{i + 1}', is_archived=False)
        db.session.add(v)
        autos.append(v)
    db.session.commit()
    return autos


def _sync(vehicle, km, minuten, soc=50, raw='{}'):
    from models.database import db, VehicleSync
    r = VehicleSync(vehicle_id=vehicle.id,
                    timestamp=datetime(2026, 9, 1) + timedelta(minutes=minuten),
                    odometer_km=km, soc_percent=soc, raw_json=raw)
    db.session.add(r)
    db.session.commit()
    return r


def test_a_clean_history_reports_nothing(app_ctx):
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    for i, km in enumerate((12340, 12345, 12360, 12400)):
        _sync(enyaq, km, i * 10)
    assert implausible_rows(enyaq.id) == []


def test_a_foreign_row_is_found_and_named(app_ctx):
    """The customer's shape: the Kona's 37 750 km inside the Enyaq."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 12340, 0)
    fremd = _sync(enyaq, 37750, 10, raw='{"stub": "KONA"}')
    _sync(enyaq, 12345, 20)
    _sync(enyaq, 12360, 30)

    treffer = implausible_rows(enyaq.id)
    assert [t['id'] for t in treffer] == [fremd.id], treffer
    assert treffer[0]['odometer_km'] == 37750
    # The row it should have been near, so the listing can say so.
    assert abs(treffer[0]['expected_around_km'] - 12345) <= 1


def test_the_intruder_is_named_not_the_innocent_row_after_it(app_ctx):
    """Reporting only "mileage went backwards" would flag the genuine row
    that follows and leave the foreign one in place."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 12340, 0)
    fremd = _sync(enyaq, 37750, 10)
    danach = _sync(enyaq, 12345, 20)
    ids = [t['id'] for t in implausible_rows(enyaq.id)]
    assert fremd.id in ids
    assert danach.id not in ids


def test_a_real_odometer_rollback_is_still_reported(app_ctx):
    """A history that simply drops and stays down is also impossible —
    reported, and left for a human to judge."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 40000, 0)
    _sync(enyaq, 40010, 10)
    _sync(enyaq, 12000, 20)
    _sync(enyaq, 12010, 30)
    assert implausible_rows(enyaq.id), 'a backwards jump must not pass silently'


def test_rounding_between_brands_is_not_a_finding(app_ctx):
    """Some brands report metres, some whole kilometres."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    for i, km in enumerate((12345, 12344, 12345, 12346)):
        _sync(enyaq, km, i * 10)
    assert implausible_rows(enyaq.id) == []


def test_deleting_only_touches_rows_the_audit_still_flags(app_ctx):
    from models.database import VehicleSync
    from services.sync_audit import delete_rows, implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 12340, 0)
    fremd = _sync(enyaq, 37750, 10)
    gut = _sync(enyaq, 12345, 20)

    fremd_id, gut_id = fremd.id, gut.id
    ergebnis = delete_rows(enyaq.id, [fremd_id, gut_id])
    assert ergebnis['deleted'] == 1
    assert ergebnis['refused'] == [gut_id]
    # Frisch abfragen statt über die alten Objekte: ein Massenlöschen
    # lässt die Sitzung mit veralteten Kopien zurück.
    uebrig = {r.id for r in VehicleSync.query.filter_by(vehicle_id=enyaq.id)}
    assert fremd_id not in uebrig
    assert gut_id in uebrig
    assert implausible_rows(enyaq.id) == []


def test_deleting_cannot_reach_another_vehicles_rows(app_ctx):
    """A stale page must not be able to delete a different car's history."""
    from models.database import VehicleSync
    from services.sync_audit import delete_rows
    kona, enyaq = _fleet()
    _sync(kona, 37740, 0)
    fremd_beim_kona = _sync(kona, 99999, 10)
    _sync(kona, 37750, 20)

    fremd_id = fremd_beim_kona.id
    ergebnis = delete_rows(enyaq.id, [fremd_id])
    assert ergebnis['deleted'] == 0
    assert VehicleSync.query.filter_by(id=fremd_id).count() == 1


def test_the_audit_reports_every_vehicle(app_ctx):
    from services.sync_audit import audit
    kona, enyaq = _fleet()
    _sync(kona, 100, 0)
    _sync(kona, 5000, 10)
    _sync(kona, 110, 20)
    _sync(enyaq, 12340, 0)
    _sync(enyaq, 12350, 10)

    bericht = audit()
    namen = {v['name']: len(v['rows']) for v in bericht['vehicles']}
    assert namen['Kona'] == 1
    assert namen['Auto2'] == 0
    assert bericht['total'] == 1


def test_two_cars_with_similar_mileage_are_honestly_not_detected(app_ctx):
    """The limit of the method, written down as a test so nobody later
    mistakes the check for a completeness guarantee."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 12340, 0)
    _sync(enyaq, 12342, 10)     # könnte vom anderen Auto sein — steigt aber
    _sync(enyaq, 12345, 20)
    assert implausible_rows(enyaq.id) == []


def test_a_finding_says_how_far_off_it_is(app_ctx):
    """Some brands re-serve a cached reading after a fresher one, which
    looks exactly like a foreign row. The size of the step is what tells
    them apart, so it has to be in the finding — the check reports a
    number a person can judge, not a verdict the data cannot support."""
    from services.sync_audit import implausible_rows
    kona, enyaq = _fleet()
    _sync(enyaq, 12340, 0)
    _sync(enyaq, 37750, 10)      # anderes Auto
    _sync(enyaq, 12345, 20)
    _sync(enyaq, 12342, 30)      # zwischengespeicherter Rückschritt (3 km)
    _sync(enyaq, 12350, 40)

    treffer = implausible_rows(enyaq.id)
    assert len(treffer) == 2
    # Größte Abweichung zuerst — das offensichtliche zuerst entscheiden.
    assert treffer[0]['delta_km'] > treffer[1]['delta_km']
    assert treffer[0]['odometer_km'] == 37750
    assert treffer[1]['delta_km'] <= 5, 'the echo must read as a small step'
