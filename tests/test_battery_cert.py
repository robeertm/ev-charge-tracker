"""Functional tests for services/battery_cert_service.py.

Spins up a throwaway in-memory SQLite app, seeds one vehicle with a
realistic charge/sync history and checks:

  * the coulomb-counting SoH matches a hand-computed value,
  * narrow-window charges are ignored,
  * outliers are rejected,
  * the certificate PDF actually renders to bytes,
  * sparse data (no wide windows, no BMS SoH) degrades gracefully.

Run with:  python3 tests/test_battery_cert.py
Exit code is non-zero if any check fails.
"""
import os
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
from models.database import db, Vehicle, Charge, VehicleSync, ObdReading  # noqa: E402


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _seed(nominal_kwh=64.0):
    """One vehicle, mostly wide-window charges that imply ~58 kWh usable
    (≈90 % SoH on a 64 kWh pack), plus noise the filters must drop."""
    v = Vehicle(name='Test Niro', brand='Kia', model='Niro EV',
                battery_kwh=nominal_kwh, battery_soh_baseline=100,
                is_archived=False, acquired_at=date(2021, 6, 1),
                api_vin='KNACX81ABC1234567')
    db.session.add(v)
    db.session.commit()

    base = date(2024, 1, 1)
    # 8 wide-window charges: 20 -> 80 (60 pts), storing 34.8 kWh net each →
    # implied capacity 34.8 / 0.60 = 58.0 kWh.
    for i in range(8):
        c = Charge(vehicle_id=v.id, date=base + timedelta(days=i * 10),
                   charge_type='DC', soc_from=20, soc_to=80,
                   kwh_loaded=36.0, loss_kwh=1.2, odometer=40000 + i * 500)
        db.session.add(c)
    # Narrow window (must be ignored): 50 -> 60, tiny.
    db.session.add(Charge(vehicle_id=v.id, date=base + timedelta(days=100),
                          charge_type='AC', soc_from=50, soc_to=60,
                          kwh_loaded=6.5, loss_kwh=0.4, odometer=45000))
    # Outlier (must be rejected): implies 200 kWh capacity.
    db.session.add(Charge(vehicle_id=v.id, date=base + timedelta(days=110),
                          charge_type='DC', soc_from=10, soc_to=60,
                          kwh_loaded=100.0, loss_kwh=0.0, odometer=45500))
    # A couple of BMS SoH syncs.
    db.session.add(VehicleSync(vehicle_id=v.id, timestamp=datetime(2024, 1, 1, 8),
                               soc_percent=80, odometer_km=40000,
                               battery_soh_percent=96.0))
    db.session.add(VehicleSync(vehicle_id=v.id, timestamp=datetime(2024, 6, 1, 8),
                               soc_percent=60, odometer_km=44000,
                               battery_soh_percent=94.5))
    db.session.commit()
    return v


def main():
    from services.i18n import set_language
    set_language('de')
    import services.battery_cert_service as bc

    app = _make_app()
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(('PASS' if cond else 'FAIL'), name)
        if not cond:
            fails += 1

    with app.app_context():
        db.create_all()
        v = _seed()

        data = bc.compute_battery_health(v.id)
        check('returns dict', isinstance(data, dict))

        meas = data['measured']
        check('measured present', meas is not None)
        # 36.0 - 1.2 = 34.8 net; /0.60 = 58.0 kWh; /64 = 90.6 %
        check('measured capacity ~58 kWh', meas and abs(meas['capacity_kwh'] - 58.0) < 0.6)
        check('measured SoH ~90.6 %', meas and abs(meas['soh_pct'] - 90.6) < 0.6)
        # Only the 8 wide DC charges qualify — narrow + outlier dropped.
        check('narrow + outlier filtered (8 samples)', meas and meas['sample_count'] == 8)

        check('bms SoH scaled', abs(data['bms']['current'] - 94.5) < 0.01)
        # measured has >=3 samples → it is the certified headline.
        check('certified source is measured', data['certified_source'] == 'measured')
        check('grade is A (>=90)', data['grade']['letter'] == 'A')

        check('equiv cycles computed', data['behaviour']['equiv_full_cycles'] is not None)
        # Nearly all throughput is DC (one small AC charge in the mix).
        check('dc share ~98%', data['behaviour']['dc_share_pct'] > 95.0)
        check('age computed', data['age_years'] is not None and data['age_years'] > 2)

    # ── Erstzulassung drives calendar age, not acquisition ───────────
    app_er = _make_app()
    with app_er.app_context():
        db.create_all()
        # Bought used in 2024 but first registered in 2020 → battery is old
        # even though ownership is young.
        v = Vehicle(name='Used Kona', brand='Hyundai', battery_kwh=64.0,
                    battery_kwh_gross=67.3, is_archived=False,
                    first_registered_at=date(2020, 3, 1),
                    acquired_at=date(2024, 1, 1),
                    retired_at=date(2025, 1, 1))
        db.session.add(v)
        db.session.commit()
        d = bc.compute_battery_health(v.id)
        # calendar age 2020-03 -> 2025-01 ≈ 4.8 yr; ownership only ~1.0 yr
        check('age from Erstzulassung (~4.8y)', abs(d['age_years'] - 4.8) < 0.2)
        check('ownership separate (~1.0y)', abs(d['ownership_years'] - 1.0) < 0.1)
        check('gross carried', abs(d['vehicle']['gross_kwh'] - 67.3) < 0.01)
        pdf = bc.generate_certificate(v.id)
        check('used-car pdf renders', isinstance(pdf, (bytes, bytearray)) and len(pdf) > 1000)

    # ── OBD reading feeds the certificate ────────────────────────────
    app_obd = _make_app()
    with app_obd.app_context():
        db.create_all()
        v = Vehicle(name='OBD Niro', brand='Kia', battery_kwh=64.0,
                    is_archived=False, acquired_at=date(2021, 1, 1))
        db.session.add(v)
        db.session.commit()
        db.session.add(ObdReading(
            vehicle_id=v.id, soh_pct=95.5, cell_count=98,
            cell_min_v=3.98, cell_max_v=4.01, cell_avg_v=4.0,
            cell_delta_mv=30.0, temp_min_c=18, temp_max_c=22, temp_avg_c=20,
            pack_voltage_v=360.0, aux_battery_v=13.6))
        db.session.commit()
        d = bc.compute_battery_health(v.id)
        check('obd present in cert data', d['obd'] is not None and abs(d['obd']['soh_pct'] - 95.5) < 0.01)
        # No wide charges → measured None → OBD SoH becomes the headline.
        check('certified source obd', d['certified_source'] == 'obd')
        check('certified soh from obd', abs(d['certified_soh'] - 95.5) < 0.01)
        pdf = bc.generate_certificate(v.id)
        check('obd pdf renders', isinstance(pdf, (bytes, bytearray)) and len(pdf) > 1000)

        pdf = bc.generate_certificate(v.id)
        check('pdf bytes produced', isinstance(pdf, (bytes, bytearray)) and len(pdf) > 1000)
        check('pdf has %PDF header', bytes(pdf[:4]) == b'%PDF')

        # grade label resolves (not empty / not the raw key)
        gl = data['grade']['label_key']
        from services.i18n import t
        check('grade label localised', t(gl) not in ('', gl))

    # ── Sparse-data path: only narrow windows, no BMS SoH ────────────
    app2 = _make_app()
    with app2.app_context():
        db.create_all()
        v = Vehicle(name='Bare', brand='MG', battery_kwh=50.0, is_archived=True,
                    acquired_at=date(2023, 1, 1))
        db.session.add(v)
        db.session.commit()
        db.session.add(Charge(vehicle_id=v.id, date=date(2024, 1, 1),
                              charge_type='AC', soc_from=60, soc_to=70,
                              kwh_loaded=5.5, odometer=10000))
        db.session.commit()

        data = bc.compute_battery_health(v.id)
        check('sparse: no measured SoH', data['measured'] is None)
        check('sparse: certified SoH None', data['certified_soh'] is None)
        check('sparse: grade unknown', data['grade']['letter'] == '?')
        pdf = bc.generate_certificate(v.id)
        check('sparse: pdf still renders', isinstance(pdf, (bytes, bytearray)) and len(pdf) > 1000)

    print('\n%d check(s) failed' % fails if fails else '\nAll checks passed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
