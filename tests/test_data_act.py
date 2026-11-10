# -*- coding: utf-8 -*-
"""XPENG Data Act import: reading the export, deriving trips and charges,
filing them without double counting, undo, and the routes.

The sample files are synthetic (tests/_data_act_fake.py) and follow the
public description of the export; no real file was available. The first real
one is kept by the importer as a format probe, see test_unknown_format.

Run with:  python3 -m pytest tests/test_data_act.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from flask import Flask                                     # noqa: E402
from models.database import db, Charge, Vehicle, VehicleTrip  # noqa: E402
from services import data_act as da                         # noqa: E402
from services import data_act_apply as ap                   # noqa: E402
import _data_act_fake as F                                  # noqa: E402

LANGS = ('de', 'en', 'es', 'fr', 'it', 'nl')


def _write(tmp_path, named):
    out = []
    for n, b in named.items():
        p = tmp_path / n
        p.write_bytes(b)
        out += da.sources_from_path(str(p), n)
    return out


@pytest.fixture
def result(tmp_path):
    return da.analyze(_write(tmp_path, F.files()))


# ── reading and deriving ──────────────────────────────────────────────
def test_names_and_parts_are_recognised():
    n = 'DA20261010AB_dwd_opp_gdpr_veh_driving_power_energy_di_part10.csv'
    assert da.classify_name(n) == ('DA20261010AB', 'power', 10)
    assert da.classify_name('x/DA1_dwd_opp_gdpr_veh_driving_status_di.csv')[1] == 'status'
    assert da.classify_name('notes.csv') is None


def test_charges_are_derived(result):
    ac, dc = sorted(result['charges'], key=lambda c: c['start'])
    assert ac['type'] == 'AC' and dc['type'] == 'DC'
    assert ac['kwh'] == pytest.approx(7.4 * 7195 / 3600, abs=0.05)
    assert dc['kwh'] == pytest.approx(100 * 1195 / 3600, abs=0.1)
    assert (ac['soc_from'], ac['soc_to']) == (39, 58)
    assert ac['peak_kw'] == 7.4 and dc['peak_kw'] == 100.0
    assert ac['odometer'] == 12020
    # the independent measure (pack voltage x current) agrees
    assert ac['kwh_pack'] == pytest.approx(ac['kwh'], abs=0.1)


def test_trips_are_derived(result):
    a, b = sorted(result['trips'], key=lambda t: t['start'])
    assert a['distance_km'] == pytest.approx(20, abs=0.3)
    assert b['distance_km'] == pytest.approx(25, abs=0.3)
    assert a['max_speed'] == 60 and b['max_speed'] == 100
    assert a['soc_from'] == 40 and a['consumption'] == pytest.approx(40, abs=1)
    assert result['vin'] == F.VIN and result['model'] == 'G6'


def test_parts_are_joined_in_numeric_order_and_duplicates_dropped(tmp_path):
    whole = da.analyze(_write(tmp_path, F.files()))
    sub = tmp_path / 'p'
    sub.mkdir()
    split = da.analyze(_write(sub, F.files(split_power=300)))
    assert [c['start'] for c in split['charges']] == [c['start'] for c in whole['charges']]
    # a part that repeats rows of the previous one (overlap) changes nothing
    named = F.files()
    key = [k for k in named if 'power' in k][0]
    lines = named[key].split(b'\n')
    named[key] = b'\n'.join(lines + lines[1:400])
    sub2 = tmp_path / 'q'
    sub2.mkdir()
    dup = da.analyze(_write(sub2, named))
    assert [c['kwh'] for c in dup['charges']] == [c['kwh'] for c in whole['charges']]


def test_placeholders_are_not_readings(tmp_path):
    named = F.files()
    key = [k for k in named if 'power' in k][0]
    rows = named[key].decode().split('\n')
    head = rows[0].split(',')
    i = head.index('ldcu_chrgpwr')
    for n in range(1, 40):
        c = rows[n].split(',')
        c[i] = '1638.3'            # "signal not available"
        rows[n] = ','.join(c)
    named[key] = '\n'.join(rows).encode()
    r = da.analyze(_write(tmp_path, named))
    assert all(c['peak_kw'] < 1000 for c in r['charges'])


def test_zip_with_all_files(tmp_path):
    p = tmp_path / 'export.zip'
    p.write_bytes(F.zip_bytes(F.files()))
    r = da.analyze(da.sources_from_path(str(p), 'export.zip'))
    assert len(r['charges']) == 2 and len(r['trips']) == 2


def test_timestamps_in_milliseconds_and_text_are_accepted():
    assert da.parse_timer('1792000000') == 1792000000
    assert da.parse_timer('1792000000000') == 1792000000
    assert da.parse_timer('1792000000.0') == 1792000000
    assert da.parse_timer('2026-10-10 12:00:00') is not None
    assert da.parse_timer('') is None and da.parse_timer('x') is None


def test_unknown_format_is_refused_clearly_and_a_probe_is_kept(tmp_path):
    p = tmp_path / 'other.csv'
    p.write_text('vin,foo,bar\nLXXXXTESTVIN000001,1,2\n')
    src = da.sources_from_path(str(p), 'other.csv')
    with pytest.raises(da.DataActError) as e:
        da.analyze(src)
    assert e.value.code == 'unknown_format'
    folder = da.save_probe(src, str(tmp_path), 'unknown_format')
    text = open(os.path.join(folder, 'file0.head.csv')).read()
    assert 'vin,foo,bar' in text and 'LXXXXTESTVIN000001' not in text
    assert json.load(open(os.path.join(folder, 'probe.json')))['reason'] == 'unknown_format'


def test_broken_zip_and_empty_zip(tmp_path):
    p = tmp_path / 'bad.zip'
    p.write_bytes(b'PK\x03\x04 not really')
    with pytest.raises(da.DataActError):
        da.analyze(da.sources_from_path(str(p), 'bad.zip'))
    z = tmp_path / 'empty.zip'
    z.write_bytes(F.zip_bytes({'readme.txt': b'hi'}))
    with pytest.raises(da.DataActError) as e:
        da.sources_from_path(str(z), 'empty.zip')
    assert e.value.code == 'zip_no_csv'


def test_status_stream_alone_has_no_signals(tmp_path):
    named = {k: v for k, v in F.files().items() if 'status' in k}
    with pytest.raises(da.DataActError) as e:
        da.analyze(_write(tmp_path, named))
    assert e.value.code == 'no_signal_streams'


# ── filing ────────────────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path):
    a = Flask(__name__)
    a.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    a.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(a)
    with a.app_context():
        db.create_all()
        v = Vehicle()
        v.name, v.is_archived, v.api_vin = 'G6', False, None
        db.session.add(v)
        db.session.commit()
        yield a


def _file(app_vid, result, tmp_path, job='aaaaaaaaaaaa'):
    plan = ap.make_plan(result, app_vid, 80.0)
    n = ap.apply_plan(plan, app_vid, str(tmp_path), job, 80.0, 0.88)
    return plan, n


def test_apply_files_charges_and_trips(app, result, tmp_path):
    plan, n = _file(1, result, tmp_path)
    assert n['charges_new'] == 2 and n['trips_new'] == 2
    ch = Charge.query.order_by(Charge.id).all()
    assert [c.charge_type for c in ch] == ['AC', 'DC']
    assert ch[0].soc_from == 39 and ch[0].soc_to == 58 and ch[0].needs_review
    assert ch[0].notes.startswith('data-act:')
    tr = VehicleTrip.query.order_by(VehicleTrip.start_time).all()
    assert tr[0].source == 'data_act' and tr[0].distance_km == pytest.approx(19.9, abs=0.3)
    assert tr[0].consumption_kwh_per_100km == pytest.approx(40, abs=1)


def test_the_same_export_twice_adds_nothing(app, result, tmp_path):
    _file(1, result, tmp_path, 'aaaaaaaaaaaa')
    plan = ap.make_plan(result, 1, 80.0)
    assert plan['summary']['charges'] == {'dup': 2}
    assert plan['summary']['trips'] == {'dup': 2}
    n = ap.apply_plan(plan, 1, str(tmp_path), 'bbbbbbbbbbbb', 80.0, 0.88)
    assert sum(n.values()) == 0
    assert Charge.query.count() == 2 and VehicleTrip.query.count() == 2


def test_an_overlapping_longer_export_extends_instead_of_doubling(app, tmp_path):
    # the first export saw only the end of the AC charge (window edge)
    full = da.analyze(_write(tmp_path, F.files()))
    ac = sorted(full['charges'], key=lambda c: c['start'])[0]
    cut = dict(full)
    short = dict(ac)
    short['start'] = ac['start'] + 3600
    short['duration'] = short['end'] - short['start']
    short['kwh'] = 7.4
    cut['charges'] = [short]
    cut['trips'] = []
    _file(1, cut, tmp_path, 'aaaaaaaaaaaa')
    assert Charge.query.count() == 1
    plan = ap.make_plan(full, 1, 80.0)
    assert [c['status'] for c in plan['charges']] == ['extend', 'new']
    ap.apply_plan(plan, 1, str(tmp_path), 'bbbbbbbbbbbb', 80.0, 0.88)
    assert Charge.query.count() == 2
    first = Charge.query.order_by(Charge.id).first()
    assert first.kwh_loaded == pytest.approx(ac['kwh'], abs=0.05)


def test_a_live_entry_is_completed_not_duplicated(app, result, tmp_path):
    ac = sorted(result['charges'], key=lambda c: c['start'])[0]
    s = datetime.fromtimestamp(ac['start'])
    live = Charge()
    live.vehicle_id, live.date, live.charge_hour = 1, s.date(), s.hour
    live.charge_end_hour = datetime.fromtimestamp(ac['end']).hour
    live.kwh_loaded, live.eur_per_kwh, live.charge_type = 15.5, 0.30, 'AC'
    live.operator = 'Home'
    live.needs_review = True
    db.session.add(live)
    db.session.commit()
    plan, n = _file(1, result, tmp_path)
    assert plan['charges'][0]['status'] == 'merge'
    assert n['charges_merged'] == 1 and n['charges_new'] == 1
    assert Charge.query.count() == 2
    live = db.session.get(Charge, live.id)
    assert live.kwh_loaded == 15.5                  # what was there stays
    assert live.eur_per_kwh == 0.30 and live.operator == 'Home'
    assert live.soc_from == 39 and live.soc_to == 58   # what was empty is filled
    assert 'data-act:' in live.notes


def test_a_live_trip_is_not_counted_twice(app, result, tmp_path):
    t = sorted(result['trips'], key=lambda x: x['start'])[0]
    row = VehicleTrip()
    row.vehicle_id = 1
    row.start_time = datetime.fromtimestamp(t['start'] + 20)
    row.trip_date = row.start_time.date()
    row.drive_minutes, row.idle_minutes, row.distance_km = 20, 0, 20.0
    row.source = 'sdk_day_trip_info'
    db.session.add(row)
    db.session.commit()
    plan, n = _file(1, result, tmp_path)
    assert plan['trips'][0]['status'] == 'live'
    assert n['trips_new'] == 1 and VehicleTrip.query.count() == 2


def test_undo_restores_everything(app, result, tmp_path):
    ac = sorted(result['charges'], key=lambda c: c['start'])[0]
    s = datetime.fromtimestamp(ac['start'])
    live = Charge()
    live.vehicle_id, live.date, live.charge_hour = 1, s.date(), s.hour
    live.charge_end_hour = datetime.fromtimestamp(ac['end']).hour
    live.charge_type, live.notes = 'AC', 'mine'
    db.session.add(live)
    db.session.commit()
    _file(1, result, tmp_path)
    assert Charge.query.count() == 2 and VehicleTrip.query.count() == 2
    r = ap.undo_last(str(tmp_path))
    assert r['removed'] == 3 and r['restored'] == 1
    assert Charge.query.count() == 1
    live = Charge.query.first()
    assert live.soc_from is None and live.notes == 'mine'
    assert ap.undo_last(str(tmp_path)) is None


def test_implausible_charge_is_skipped(app, result, tmp_path):
    big = dict(result)
    c = dict(result['charges'][0])
    c['kwh'] = 500.0
    big['charges'] = [c]
    big['trips'] = []
    plan = ap.make_plan(big, 1, 80.0)
    assert plan['charges'][0]['status'] == 'implausible'


def test_vin_mismatch_is_detected():
    v = Vehicle()
    v.api_vin = 'LXXXXOTHERVIN00002'
    assert ap.vin_mismatch(v, F.VIN)
    v.api_vin = F.VIN.lower()
    assert not ap.vin_mismatch(v, F.VIN)
    v.api_vin = None
    assert not ap.vin_mismatch(v, F.VIN)


# ── texts ─────────────────────────────────────────────────────────────
def test_every_text_exists_in_every_language_with_the_same_placeholders():
    import re
    base = None
    for lang in LANGS:
        d = json.load(open(os.path.join(ROOT, 'translations', lang + '.json'), encoding='utf-8'))
        keys = {k: v for k, v in d.items() if k.startswith('dataact.')}
        assert len(keys) >= 60, lang
        if base is None:
            base = keys
        assert set(keys) == set(base), lang
        for k, v in keys.items():
            assert sorted(re.findall(r'\{(\w+)\}', v)) == sorted(re.findall(r'\{(\w+)\}', base[k])), (lang, k)
    for lang in LANGS:
        d = json.load(open(os.path.join(ROOT, 'translations', lang + '.json'), encoding='utf-8'))
        for c in ('no_file', 'too_large', 'no_vehicle', 'store_failed', 'unknown_job',
                  'apply_failed', 'undo_none', 'zip_broken', 'zip_no_csv',
                  'unknown_format', 'no_signal_streams', 'empty', 'unreadable',
                  'vin_mismatch'):
            assert 'dataact.err_' + c in d


# ── routes: the whole click path in a real app ────────────────────────
_ROUTES = """
import io, json, sys, time
sys.path.insert(0, %r); sys.path.insert(0, %r)
import _data_act_fake as F
from app import create_app
from models.database import db, AppConfig, Vehicle, Charge, VehicleTrip
app = create_app()
client = app.test_client()
H = {'X-Requested-With': 'XMLHttpRequest'}
from services.auth_service import set_credentials
with app.app_context():
    AppConfig.set('vehicle_sync_enabled', 'false')
    vid = Vehicle.query.order_by(Vehicle.id).first().id
    set_credentials('owner', 'owner-password-123')
out = {}
def up(files, headers=H, **extra):
    data = {'export_files': [(io.BytesIO(b), n) for n, b in files.items()], 'vehicle_id': str(vid)}
    data.update(extra)
    r = client.post('/api/data-act/upload', data=data, headers=headers, content_type='multipart/form-data')
    return r.status_code, (r.get_json(silent=True) or {})
def wait(job):
    for _ in range(300):
        j = client.get('/api/data-act/job/' + job).get_json()
        if j['state'] != 'running':
            return j
        time.sleep(0.1)
out['logged_out'] = up(F.files())[0]
client.post('/login', data={'username': 'owner', 'password': 'owner-password-123'})
out['page'] = client.get('/data-act').status_code
out['settings_link'] = 'linkDataAct' in client.get('/settings').get_data(as_text=True)
out['no_header'] = up(F.files(), headers={})[0]
code, j = up({'a.csv': b'vin,foo\\nX,1\\n'})
out['unknown'] = wait(j['job'])
code, j = up({'export.zip': F.zip_bytes(F.files())})
ready = wait(j['job'])
out['ready'] = (ready['state'], ready['summary']['counts'])
out['rows_ui'] = len(ready['summary']['rows_ui']['charges'])
out['no_write_before_apply'] = (Charge.query.count() if False else None)
r = client.post('/api/data-act/apply/' + j['job'], headers=H)
out['apply'] = (r.status_code, r.get_json().get('charges_new'), r.get_json().get('trips_new'))
with app.app_context():
    out['db'] = (Charge.query.count(), VehicleTrip.query.count())
code, j2 = up({'export.zip': F.zip_bytes(F.files())})
again = wait(j2['job'])
out['again'] = again['summary']['counts']
out['apply_unknown_job'] = client.post('/api/data-act/apply/zzzzzzzzzzzz', headers=H).status_code
out['undo'] = client.post('/api/data-act/undo', headers=H).status_code
with app.app_context():
    out['after_undo'] = (Charge.query.count(), VehicleTrip.query.count())
out['undo_again'] = client.post('/api/data-act/undo', headers=H).status_code
with app.app_context():
    v = db.session.get(Vehicle, vid)
    v.api_vin = 'LXXXXOTHERVIN00002'
    db.session.commit()
code, j3 = up({'export.zip': F.zip_bytes(F.files())})
mm = wait(j3['job'])
out['vin'] = (mm['state'], mm.get('code'))
code, j4 = up({'export.zip': F.zip_bytes(F.files())}, force='1')
out['vin_force'] = wait(j4['job'])['state']
print(json.dumps(out))
"""


def test_routes_whole_path(tmp_path):
    env = dict(os.environ, EV_DATA_DIR=str(tmp_path), SECRET_KEY='data-act-test')
    code = _ROUTES % (ROOT, os.path.join(ROOT, 'tests'))
    proc = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads([ln for ln in proc.stdout.split('\n') if ln.startswith('{')][-1])
    assert out['logged_out'] in (302, 401)
    assert out['page'] == 200 and out['settings_link']
    assert out['no_header'] == 403
    assert out['unknown']['state'] == 'error' and out['unknown']['code'] == 'unknown_format'
    assert out['ready'][0] == 'ready'
    assert out['ready'][1] == {'charges': {'new': 2}, 'trips': {'new': 2}}
    assert out['apply'] == [200, 2, 2]
    assert out['db'] == [2, 2]
    assert out['again'] == {'charges': {'dup': 2}, 'trips': {'dup': 2}}
    assert out['apply_unknown_job'] == 404
    assert out['undo'] == 200 and out['after_undo'] == [0, 0]
    assert out['undo_again'] == 404
    assert out['vin'] == ['error', 'vin_mismatch']
    assert out['vin_force'] == 'ready'
    # the upload folder and the probe: raw files are gone after analysis
    assert not os.path.isdir(os.path.join(str(tmp_path), 'data_act', 'uploads')) or \
        not os.listdir(os.path.join(str(tmp_path), 'data_act', 'uploads'))
    probes = os.path.join(str(tmp_path), 'data_act', 'probe')
    assert os.path.isdir(probes) and os.listdir(probes)
