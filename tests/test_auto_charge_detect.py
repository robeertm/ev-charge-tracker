# -*- coding: utf-8 -*-
"""Two detectors, one car, and the ways a real charge falls between them.

A charge is found either by the primary detector — it owns every window in
which the car reported ``is_charging`` — or by the SoC-rise fallback, for
brands whose cloud only pushes state on key events and therefore almost never
coincides with the charging itself.

Both of the failures below happened, on the same car, on consecutive days, and
neither made a sound: the wallbox meter had both charges to the watt-hour while
the app showed nothing at all.

* **Monday.** Parked at 92 %, drove 23 km home, charged 6.9 kWh, next sync
  94 %. The primary detector owned the window (the cloud was still reporting
  is_charging hours later), measured a 2 % gain against its 3 % threshold and
  dropped the charge. The drive inside its own window was never taken off.
* **Tuesday.** The fallback would have got it — 5 % plus 24 km is 12 % against
  its 8 % threshold — but its walk-back to the SoC valley bails on any charging
  row it meets, and it met Monday's stale echo, which sat one step *beyond* the
  valley and should merely have ended the walk.

So each rule is checked from both sides: it fires on the real case AND stays
quiet when the thing it guards against is genuinely present.
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AKKU = 58.0                    # usable capacity of the car this happened to


@pytest.fixture
def app_ctx():
    d = tempfile.mkdtemp(prefix='evct-detect-')
    os.environ['EV_DATA_DIR'] = d
    os.environ.setdefault('SECRET_KEY', 'detect-test')
    # 🔴 Only the two modules that read EV_DATA_DIR at import time. Dropping
    # ``models`` as well would hand the next test file in the run a second
    # SQLAlchemy registry while it still holds the first one — every mapping
    # it made at collection time then belongs to a database that no longer
    # exists. (test_sync_audit.py does drop them, which is why running it
    # before test_shelly_link.py fails; this file does not add to that.)
    for mod in [m for m in list(sys.modules) if m.startswith(('app', 'config'))]:
        sys.modules.pop(mod, None)
    from app import create_app
    a = create_app()
    with a.app_context():
        from models.database import db, Vehicle
        v = Vehicle.query.order_by(Vehicle.id.asc()).first()
        v.name = 'Testwagen'
        v.battery_kwh = AKKU
        v.is_archived = False
        db.session.commit()
        yield a


def _sync(app_ctx, wann, soc, odo, laedt=False):
    """One cloud answer, as the sync loop would have stored it."""
    from models.database import db, Vehicle, VehicleSync
    v = Vehicle.query.order_by(Vehicle.id.asc()).first()
    s = VehicleSync(vehicle_id=v.id,
                    timestamp=datetime.strptime(wann, '%Y-%m-%d %H:%M'),
                    soc_percent=soc, odometer_km=odo, is_charging=laedt)
    db.session.add(s)
    db.session.commit()
    return s


def _ladungen():
    from models.database import Charge
    return Charge.query.order_by(Charge.id.asc()).all()


def pruefe(name, ist, soll):
    """One checked claim, printed either way.

    Half of these say a rule must NOT fire; a silent suite makes a check that
    was accidentally removed look exactly like one that passed.
    """
    print(("  OK   " if ist == soll else "  FEHL ") + name +
          "   ist=%r soll=%r" % (ist, soll))
    assert ist == soll, "%s: ist=%r soll=%r" % (name, ist, soll)


# ══════════════════════════════════════════════════════════════════════════
# The fallback and the stale echo
# ══════════════════════════════════════════════════════════════════════════

def test_01_a_stale_echo_beyond_the_valley_does_not_block_the_fallback(app_ctx):
    """Tuesday. The echo is from Monday's charge and sits above the valley."""
    import app as A
    _sync(app_ctx, '2026-09-28 19:02', 94, 30023, laedt=True)   # stale echo
    _sync(app_ctx, '2026-09-29 03:02', 94, 30023, laedt=True)   # still echoing
    _sync(app_ctx, '2026-09-29 07:02', 89, 30045)               # the valley
    _sync(app_ctx, '2026-09-29 11:02', 89, 30045)
    _sync(app_ctx, '2026-09-29 15:02', 89, 30045)
    ende = _sync(app_ctx, '2026-09-29 19:02', 94, 30069)        # home, charged, drove

    A._detect_auto_charge_from_soc_rise(ende)

    l = _ladungen()
    pruefe('the charge is found', len(l), 1)
    # 5 % on the dial plus 24 km driven ≈ 12 % actually put in — the start is
    # carried back by the drive, exactly as the detector has always done.
    pruefe('the start is corrected for the drive', l[0].soc_from, 82)
    pruefe('the end is what the car reported', l[0].soc_to, 94)
    pruefe('it is flagged for review, not asserted', bool(l[0].needs_review), True)


def test_02_a_charging_row_inside_the_rise_still_stops_it(app_ctx):
    """The guard's real job: a row the walk-back genuinely crosses.

    Same shape as above, but the charging row lies *within* the rise instead of
    beyond its valley. That window belongs to the primary detector, and running
    both over one session is what produced the 20-hour ghost of v3.0.69.
    """
    import app as A
    _sync(app_ctx, '2026-09-29 07:02', 89, 30045)
    _sync(app_ctx, '2026-09-29 11:02', 89, 30045, laedt=True)   # inside the rise
    _sync(app_ctx, '2026-09-29 15:02', 89, 30045)
    ende = _sync(app_ctx, '2026-09-29 19:02', 94, 30069)

    A._detect_auto_charge_from_soc_rise(ende)
    pruefe('nothing is written', len(_ladungen()), 0)


def test_03_without_the_drive_the_rise_stays_below_the_threshold(app_ctx):
    """And the threshold still means something: 5 % alone is not a charge."""
    import app as A
    _sync(app_ctx, '2026-09-29 07:02', 89, 30045)
    _sync(app_ctx, '2026-09-29 15:02', 89, 30045)
    ende = _sync(app_ctx, '2026-09-29 19:02', 94, 30045)        # not one kilometre

    A._detect_auto_charge_from_soc_rise(ende)
    pruefe('nothing is written', len(_ladungen()), 0)


# ══════════════════════════════════════════════════════════════════════════
# The primary detector and the drive inside its own window
# ══════════════════════════════════════════════════════════════════════════

def test_04_a_drive_before_the_charging_run_is_taken_off_the_start(app_ctx):
    """Monday. 92 % at the office, 23 km home, then the wallbox."""
    import app as A
    _sync(app_ctx, '2026-09-28 07:02', 92, 30000)
    _sync(app_ctx, '2026-09-28 15:02', 92, 30000)               # before the drive
    _sync(app_ctx, '2026-09-28 19:02', 94, 30023, laedt=True)   # home, 23 km on
    ende = _sync(app_ctx, '2026-09-29 07:02', 89, 30045)        # charging run over

    A._detect_auto_charge(ende)

    l = _ladungen()
    pruefe('the charge is found', len(l), 1)
    # 92 % minus 23 km at 18 kWh/100 km on a 58 kWh battery = 85 %.
    pruefe('the start is corrected for the drive', l[0].soc_from, 85)
    pruefe('the end is the peak seen while charging', l[0].soc_to, 94)


def test_05_a_car_that_did_not_move_gets_no_correction(app_ctx):
    """Same two percent, no kilometres — and two percent is still noise.

    Without this the fix would be a threshold cut in disguise: every parked
    car's BMS recalibration would start producing charges.
    """
    import app as A
    _sync(app_ctx, '2026-09-28 07:02', 92, 30000)
    _sync(app_ctx, '2026-09-28 15:02', 92, 30000)
    _sync(app_ctx, '2026-09-28 19:02', 94, 30000, laedt=True)   # stood still
    ende = _sync(app_ctx, '2026-09-29 07:02', 94, 30000)

    A._detect_auto_charge(ende)
    pruefe('nothing is written', len(_ladungen()), 0)


def test_06_a_stale_pre_charge_soc_is_still_not_corrected(app_ctx):
    """The older stale-echo rule keeps precedence over the new correction.

    When the pre-charge row reads HIGHER than the first charging sample it is a
    cached echo, and the detector already falls back to the charging sample.
    Subtracting a drive from that sample would move a start that was never the
    pre-charge value in the first place.
    """
    import app as A
    _sync(app_ctx, '2026-09-28 15:02', 97, 30000)               # stale, too high
    _sync(app_ctx, '2026-09-28 19:02', 37, 30259, laedt=True)   # the truth, +236 km
    _sync(app_ctx, '2026-09-28 22:02', 80, 30259, laedt=True)
    ende = _sync(app_ctx, '2026-09-29 07:02', 87, 30259)

    A._detect_auto_charge(ende)

    l = _ladungen()
    pruefe('the charge is found', len(l), 1)
    pruefe('the start is the first charging sample, untouched', l[0].soc_from, 37)


# ══════════════════════════════════════════════════════════════════════════
# Throwing a reading away
# ══════════════════════════════════════════════════════════════════════════

def test_07_a_reading_a_charge_is_built_on_cannot_be_discarded(app_ctx):
    """The one thing discarding must never do: hollow out an entry."""
    from models.database import db, WallboxCharge
    w = WallboxCharge(device_key='wallbox', source_id='x1', start_ts=1, end_ts=2,
                      energy_kwh=5.0, match_state='matched')
    db.session.add(w)
    db.session.commit()

    c = app_ctx.test_client()
    a = c.post('/api/wallbox/discard', json={'reading_id': w.id}).get_json()
    pruefe('it is refused', a.get('ok'), False)
    pruefe('the reading is still there', WallboxCharge.query.count(), 1)


def test_08_an_unmatched_reading_can_be_discarded(app_ctx):
    from models.database import db, WallboxCharge
    w = WallboxCharge(device_key='wallbox', source_id='x2', start_ts=1, end_ts=2,
                      energy_kwh=5.0, match_state='unmatched')
    db.session.add(w)
    db.session.commit()

    c = app_ctx.test_client()
    a = c.post('/api/wallbox/discard', json={'reading_id': w.id}).get_json()
    pruefe('it is discarded', a.get('ok'), True)
    pruefe('and it is gone', WallboxCharge.query.count(), 0)
