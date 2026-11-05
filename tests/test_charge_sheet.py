# -*- coding: utf-8 -*-
"""Charges out to a spreadsheet and back in again.

What a user does: export, open the file in a spreadsheet program, fix a
price, add a forgotten charge, mark a duplicate for deletion, save, import.
What must hold:

* an untouched file changes nothing — not one field, not after a
  spreadsheet re-saved it with ``;``, a BOM, decimal commas and dotted dates;
* every edited field is reported as old → new before anything is written;
* rows that are wrong are refused with their line and the reason, and the
  rest still goes in;
* a charge missing from the file is never deleted; only ``delete = yes`` is;
* undo puts every touched row back exactly as it was;
* a text cell that a spreadsheet would run as a formula is defused.

Service tests run on an in-memory database; the route tests start the real
app in a subprocess on a throwaway data folder (same pattern as
test_readonly_api.py).

Run with:  python3 -m pytest tests/test_charge_sheet.py
"""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                                     # noqa: E402
from sqlalchemy.orm import class_mapper                     # noqa: E402
from models.database import db, Charge, Vehicle              # noqa: E402
from services import charge_sheet as S                      # noqa: E402

TODAY = date(2026, 11, 5)
LANGS = ('de', 'en', 'es', 'fr', 'it', 'nl')


# ── helpers ───────────────────────────────────────────────────────────
def _make(model, **values):
    """Build a model row attribute by attribute (the declarative keyword
    constructor is invisible to type checkers)."""
    obj = model()
    for key, value in values.items():
        setattr(obj, key, value)
    return obj


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    a.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(a)
    with a.app_context():
        db.create_all()
        car = _make(Vehicle, name='Daily car', is_archived=False)
        other = _make(Vehicle, name='Second car', is_archived=False)
        db.session.add_all([car, other])
        db.session.flush()
        rows = [
            _make(Charge, vehicle_id=car.id, date=date(2026, 10, 1), charge_hour=22,
                   charge_end_hour=3, odometer=40100, charge_type='AC',
                   kwh_loaded=21.4567, eur_per_kwh=0.3149, soc_from=40,
                   soc_to=75, loss_kwh=2.34567, co2_g_per_kwh=380,
                   operator='Home', location_name='Garage',
                   location_lat=51.1234567, location_lon=-0.1234567,
                   notes='=HYPERLINK("http://example.invalid")\nsecond line'),
            _make(Charge, vehicle_id=car.id, date=date(2026, 10, 5), charge_hour=12,
                   charge_type='DC', kwh_loaded=30.0, eur_per_kwh=0.59,
                   start_fee_eur=1.5, blocking_fee_eur=0.25, soc_from=20,
                   soc_to=80, operator='+Fast & Co', notes='Ümlaut ß café'),
            _make(Charge, vehicle_id=other.id, date=date(2026, 10, 7),
                   charge_type='PV', kwh_loaded=9.9, total_cost=0.0,
                   notes='@mention -dash'),
            _make(Charge, vehicle_id=car.id, date=date(2026, 10, 9)),   # nearly empty
        ]
        for c in rows:
            c.calculate_fields(64.0, 0.9)
        db.session.add_all(rows)
        db.session.commit()
    yield a


def _all_rows():
    cols = [col.name for col in class_mapper(Charge).columns]
    return [{name: getattr(c, name) for name in cols}
            for c in Charge.query.order_by(Charge.id).all()]


def _plan(raw):
    return S.build_plan(raw, battery_kwh_fn=lambda vid: 64.0,
                        efficiency_fn=lambda vid: 0.9, today=TODAY)


def _csv_rows(raw):
    text = raw.decode('utf-8-sig')
    return list(csv.reader(io.StringIO(text), delimiter=';'))


def _to_csv(rows, delimiter=';', bom=True, encoding='utf-8'):
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator='\r\n').writerows(rows)
    data = out.getvalue().encode(encoding)
    return (b'\xef\xbb\xbf' + data) if bom and encoding == 'utf-8' else data


def _by_line(plan, line):
    return next(r for r in plan['rows'] if r['line'] == line)


def _export(comma=True):
    return S.export_csv(S.query_charges(), comma)


# ── round trip ────────────────────────────────────────────────────────
@pytest.mark.parametrize('comma', [True, False])
def test_an_untouched_csv_changes_nothing(app, comma):
    with app.app_context():
        plan = _plan(_export(comma))
        s = plan['summary']
        assert (s['new'], s['changed'], s['delete'], s['invalid']) == (0, 0, 0, 0), plan['rows']
        assert s['unchanged'] == 4


def test_an_untouched_xlsx_changes_nothing(app):
    pytest.importorskip('openpyxl')
    with app.app_context():
        raw = S.export_xlsx(S.query_charges())
        assert raw[:4] == b'PK\x03\x04'
        plan = _plan(raw)
        s = plan['summary']
        assert (s['new'], s['changed'], s['delete'], s['invalid']) == (0, 0, 0, 0), plan['rows']
        assert plan['format'] == 'xlsx'


def test_export_is_spreadsheet_ready(app):
    with app.app_context():
        raw = _export(True)
    assert raw.startswith(b'\xef\xbb\xbf'), 'UTF-8 BOM so a spreadsheet picks UTF-8'
    rows = _csv_rows(raw)
    assert rows[0] == list(S.COLUMN_NAMES)
    first = dict(zip(rows[0], rows[1]))
    assert first['id'].isdigit()
    assert first['kwh_loaded'] == '21,457'        # decimal comma, export precision
    assert first['location_lon'] == '-0,123457'   # numbers are never masked
    assert first['date'] == '2026-10-01'
    assert first['vehicle'] == 'Daily car'
    with app.app_context():
        point = _csv_rows(_export(False))
    assert dict(zip(point[0], point[1]))['kwh_loaded'] == '21.457'


def test_export_filters_by_vehicle_and_period(app):
    with app.app_context():
        car = Vehicle.query.filter_by(name='Daily car').one()
        got = S.query_charges(car.id, date(2026, 10, 2), date(2026, 10, 31))
        assert [c.date for c in got] == [date(2026, 10, 5), date(2026, 10, 9)]


def test_a_spreadsheet_resave_with_semicolon_bom_comma_and_dotted_dates_is_read(app):
    """What a spreadsheet program in a German locale writes back: dotted
    dates, decimal commas, and either UTF-8 with BOM or the Windows code
    page. Nothing may count as changed."""
    with app.app_context():
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        di = hdr.index('date')
        for r in rows[1:]:
            y, m, d = r[di].split('-')
            r[di] = f'{d}.{m}.{y}'
        for raw in (_to_csv(rows, bom=True), _to_csv(rows, bom=False, encoding='cp1252')):
            plan = _plan(raw)
            s = plan['summary']
            assert plan['delimiter'] == ';'
            assert (s['new'], s['changed'], s['invalid']) == (0, 0, 0), plan['rows']
        # Point decimals, comma separator, US-style dates that resolve
        # against the stored value — also no change.
        rows = _csv_rows(_export(False))
        for r in rows[1:]:
            y, m, d = r[di].split('-')
            r[di] = f'{int(m)}/{int(d)}/{y}'
        plan = _plan(_to_csv(rows, delimiter=',', bom=False))
        s = plan['summary']
        assert plan['delimiter'] == ','
        assert (s['new'], s['changed'], s['invalid']) == (0, 0, 0), plan['rows']


# ── changes ───────────────────────────────────────────────────────────
def test_edited_fields_are_reported_old_to_new_and_derived_values_follow(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        r = rows[2]                                    # the DC charge
        r[hdr.index('kwh_loaded')] = '40,5'
        r[hdr.index('notes')] = 'fixed price'
        r[hdr.index('total_cost')] = '999'             # recalculated anyway
        plan = _plan(_to_csv(rows))
        row = _by_line(plan, 3)
        assert row['status'] == 'changed'
        ch = {c['field']: (c['old'], c['new']) for c in row['changes']}
        assert ch['kwh_loaded'] == (30.0, 40.5)
        assert ch['notes'] == ('Ümlaut ß café', 'fixed price')
        # 40.5 × 0.59 + 1.50 + 0.25, rounded exactly as the edit form does
        expected = round(40.5 * 0.59 + 1.5 + 0.25, 2)
        assert ch['total_cost'][1] == pytest.approx(expected)
        assert plan['summary']['changed'] == 1
        assert plan['summary']['unchanged'] == 3

        cid = row['id']
        S.apply_plan(plan, tempfile.mkdtemp())
        c = db.session.get(Charge, cid)
        assert c is not None
        assert c.kwh_loaded == 40.5 and c.notes == 'fixed price'
        assert c.total_cost == pytest.approx(expected)


def test_a_new_date_clears_the_grid_intensity_so_it_is_fetched_again(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        rows[1][hdr.index('date')] = '2026-10-02'
        plan = _plan(_to_csv(rows))
        row = _by_line(plan, 2)
        ch = {c['field']: c['new'] for c in row['changes']}
        assert ch['date'] == date(2026, 10, 2)
        assert ch['co2_g_per_kwh'] is None and ch['co2_kg'] is None
        assert row['refetch_co2'] is True


def test_a_missing_column_leaves_the_field_alone(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        ni = rows[0].index('notes')
        rows = [r[:ni] + r[ni + 1:] for r in rows]
        plan = _plan(_to_csv(rows))
        assert plan['summary']['changed'] == 0


def test_new_rows_are_added_and_only_marked_rows_are_deleted(app):
    with app.app_context():
        before = Charge.query.count()
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        keep = [hdr, rows[1], rows[2]]                 # rows 3 and 4 left out
        keep[2][hdr.index('delete')] = 'ja'            # delete the DC charge
        new = [''] * len(hdr)
        new[hdr.index('date')] = '2026-10-20'
        new[hdr.index('kwh_loaded')] = '12,5'
        new[hdr.index('eur_per_kwh')] = '0,30'
        new[hdr.index('charge_type')] = 'ac'
        keep.append(new)
        plan = _plan(_to_csv(keep))
        s = plan['summary']
        assert (s['new'], s['delete'], s['unchanged'], s['invalid']) == (1, 1, 1, 0), plan['rows']
        res = S.apply_plan(plan, tempfile.mkdtemp())
        assert (res['inserted'], res['deleted']) == (1, 1)
        # Left-out rows are still there: 4 − 1 deleted + 1 new.
        assert Charge.query.count() == before
        added = Charge.query.filter_by(date=date(2026, 10, 20)).one()
        assert added.charge_type == 'AC' and added.total_cost == pytest.approx(3.75)
        assert added.vehicle_id is not None


# ── refused rows ──────────────────────────────────────────────────────
def test_invalid_rows_are_refused_with_line_and_reason(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        H = hdr.index

        def blank(**kw):
            r = [''] * len(hdr)
            for k, v in kw.items():
                r[H(k)] = v
            return r

        bad = [hdr]
        r = list(rows[1]); r[H('kwh_loaded')] = '-3'; bad.append(r)           # 2
        r = list(rows[2]); r[H('soc_from')] = '90'; bad.append(r)             # 3
        r = list(rows[3]); r[H('date')] = '31.02.2026'; bad.append(r)         # 4
        r = list(rows[4]); r[H('charge_type')] = 'XX'; bad.append(r)          # 5
        bad.append(blank(id='99999', date='2026-10-01', kwh_loaded='1'))      # 6
        bad.append(blank(date='2026-10-21'))                                  # 7 no kWh
        bad.append(blank(date='2027-05-01', kwh_loaded='5'))                  # 8 future
        dup = list(rows[1]); dup[H('id')] = ''; bad.append(dup)               # 9 dup of existing
        bad.append(blank(date='2026-10-22', kwh_loaded='abc'))                # 10
        bad.append(blank(date='2026-10-22', kwh_loaded='5', soc_to='120'))    # 11
        bad.append(blank(date='2026-10-23', kwh_loaded='5', delete='maybe'))  # 12
        bad.append(blank(date='2026-10-24', kwh_loaded='5', vehicle_id='77')) # 13
        ok = blank(date='2026-10-25', kwh_loaded='7'); bad.append(ok)         # 14 valid
        plan = _plan(_to_csv(bad))
        expect = {2: 'negative', 3: 'soc_order', 4: 'invalid_date',
                  5: 'invalid_type', 6: 'unknown_id', 7: 'required',
                  8: 'future_date', 9: 'duplicate_charge',
                  10: 'invalid_number', 11: 'out_of_range',
                  12: 'invalid_delete', 13: 'unknown_vehicle'}
        for line, code in expect.items():
            row = _by_line(plan, line)
            assert row['status'] == 'invalid', (line, row)
            assert code in [c for _f, c in row['errors']], (line, row['errors'])
        assert _by_line(plan, 14)['status'] == 'new'

        before = _all_rows()
        S.apply_plan(plan, tempfile.mkdtemp())
        after = _all_rows()
        assert after[:len(before)] == before, 'refused rows must not touch anything'
        assert len(after) == len(before) + 1


def test_the_same_id_twice_is_refused_both_times(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        rows.append(list(rows[1]))
        plan = _plan(_to_csv(rows))
        lines = [r['line'] for r in plan['rows']
                 if ('id', 'duplicate_id') in r['errors']]
        assert lines == [2, len(rows)]


def test_a_file_without_a_date_header_is_refused_as_a_whole(app):
    with app.app_context():
        with pytest.raises(S.SheetError) as e:
            _plan(b'foo;bar\n1;2\n')
        assert e.value.code == 'no_header'
        with pytest.raises(S.SheetError):
            _plan(b'')


# ── backup and undo ───────────────────────────────────────────────────
def test_undo_restores_the_state_before_the_import(app):
    with app.app_context():
        before = _all_rows()
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        rows[1][hdr.index('notes')] = 'changed'
        rows[1][hdr.index('kwh_loaded')] = '22'
        rows[3][hdr.index('delete')] = 'yes'
        new = [''] * len(hdr)
        new[hdr.index('date')] = '2026-10-30'
        new[hdr.index('kwh_loaded')] = '5'
        rows.append(new)
        backup_dir = tempfile.mkdtemp()
        plan = _plan(_to_csv(rows))
        res = S.apply_plan(plan, backup_dir)
        assert (res['inserted'], res['updated'], res['deleted']) == (1, 1, 1)
        assert os.path.isfile(res['path'])
        journal = json.load(open(res['path'], encoding='utf-8'))
        assert journal['state'] == 'applied' and len(journal['snapshot']) == 4
        assert S.last_import(backup_dir) is not None

        assert S.undo_last(backup_dir) is not None
        db.session.expire_all()
        assert _all_rows() == before
        # Undone once is undone: there is nothing left to undo.
        assert S.last_import(backup_dir) is None
        assert S.undo_last(backup_dir) is None


def test_a_failed_apply_writes_nothing_and_leaves_no_journal(app, monkeypatch):
    with app.app_context():
        before = _all_rows()
        rows = _csv_rows(_export(True))
        rows[1][rows[0].index('notes')] = 'x'
        plan = _plan(_to_csv(rows))
        backup_dir = tempfile.mkdtemp()
        monkeypatch.setattr(db.session, 'commit',
                            lambda: (_ for _ in ()).throw(RuntimeError('disk full')))
        with pytest.raises(RuntimeError):
            S.apply_plan(plan, backup_dir)
        monkeypatch.undo()
        db.session.rollback()
        assert _all_rows() == before
        assert os.listdir(backup_dir) == []


# ── formula injection ─────────────────────────────────────────────────
def test_text_that_would_run_as_a_formula_is_masked_and_comes_back_intact(app):
    with app.app_context():
        rows = _csv_rows(_export(True))
        hdr = rows[0]
        cells = {(r[hdr.index('id')], col): r[hdr.index(col)]
                 for r in rows[1:] for col in ('notes', 'operator')}
        texts = [v for v in cells.values() if v]
        assert any(v.startswith("'=") for v in texts)
        assert any(v.startswith("'+") for v in texts)
        assert any(v.startswith("'@") for v in texts)
        for v in texts:
            assert not v.startswith(('=', '+', '-', '@')), v
        # and nothing changes on the way back
        assert _plan(_export(True))['summary']['changed'] == 0
    assert S.mask_formula('-1') == "'-1"
    assert S.unmask_formula("'=1+1") == '=1+1'
    assert S.unmask_formula("'plain") == "'plain"


def test_xlsx_never_stores_a_formula(app):
    openpyxl = pytest.importorskip('openpyxl')
    with app.app_context():
        raw = S.export_xlsx(S.query_charges())
    ws = openpyxl.load_workbook(io.BytesIO(raw)).active
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            assert cell.data_type != 'f', cell.coordinate
            if isinstance(cell.value, str):
                assert not cell.value.startswith(('=', '+', '-', '@')), cell.value


# ── languages and the old importer ────────────────────────────────────
def test_every_reason_and_message_exists_in_all_languages():
    import re
    src = open(os.path.join(ROOT, 'services', 'charge_sheet.py'), encoding='utf-8').read()
    codes = set(re.findall(r"""(?:SheetError|_CellError)\('([a-z_]+)'""", src))
    codes |= set(re.findall(r"""errs\.append\(\([^,]+,\s*'([a-z_]+)'\)\)""", src))
    codes |= set(re.findall(r"""entry\['errors'\]\.append\(\([^,]+,\s*'([a-z_]+)'\)\)""", src))
    file_codes = set(re.findall(r"""SheetError\('([a-z_]+)'""", src))
    row_codes = codes - file_codes
    assert {'required', 'negative', 'soc_order', 'duplicate_id'} <= row_codes
    app_src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    file_codes |= set(re.findall(r"""t\('sheet\.err_file_([a-z_]+)'""", app_src))
    for lang in LANGS:
        d = json.load(open(os.path.join(ROOT, 'translations', f'{lang}.json'), encoding='utf-8'))
        missing = [c for c in row_codes if f'sheet.err_{c}' not in d]
        missing += [c for c in file_codes if f'sheet.err_file_{c}' not in d]
        missing += [f for f in ('id', 'vehicle_id', 'charge_end_hour', 'start_fee_eur',
                                'blocking_fee_eur', 'delete')
                    if f'sheet.field_{f}' not in d]
        assert not missing, (lang, missing)


def test_the_old_header_import_still_reads_the_new_export():
    """The import for files from elsewhere matches headers by alias; the
    new column names must land on the right fields there too."""
    from import_gsheet import _build_column_map
    cmap = _build_column_map(list(S.COLUMN_NAMES))
    idx = {name: i for i, name in enumerate(S.COLUMN_NAMES)}
    for field in ('date', 'charge_hour', 'kwh_loaded', 'eur_per_kwh',
                  'total_cost', 'charge_type', 'soc_from', 'soc_to', 'loss_kwh',
                  'co2_g_per_kwh', 'operator', 'location_name', 'location_lat',
                  'location_lon', 'notes'):
        assert cmap.get(field) == idx[field], field
    assert cmap.get('odometer') == idx['odometer_km']


# ── routes: rights, guard, size, the whole click path ─────────────────
_PREAMBLE = """
import io, json, sys
sys.path.insert(0, %r)
from app import create_app
from models.database import db, AppConfig, Vehicle, Charge
from datetime import date
app = create_app()
client = app.test_client()
H = {'X-Requested-With': 'XMLHttpRequest'}
def up(path, raw, headers=H, **extra):
    data = {'sheet_file': (io.BytesIO(raw), 'charges.csv')}
    data.update(extra)
    r = client.post(path, data=data, headers=headers, content_type='multipart/form-data')
    return r.status_code, (r.get_json(silent=True) or {})
""" % ROOT

_FLOW = """
from services.auth_service import set_credentials
with app.app_context():
    AppConfig.set('vehicle_sync_enabled', 'false')
    v = Vehicle.query.order_by(Vehicle.id).first()
    db.session.add(Charge(vehicle_id=v.id, date=date(2026, 10, 1), charge_hour=8,
                          kwh_loaded=20.0, eur_per_kwh=0.3, charge_type='AC'))
    db.session.commit()
    set_credentials('owner', 'owner-password-123')
out = {}
raw = client.get('/api/charges/export.csv').status_code
out['export_logged_out'] = raw
out['preview_logged_out'] = up('/api/charges/import/preview', b'id;date\\n')[0]
client.post('/login', data={'username': 'owner', 'password': 'owner-password-123'})
r = client.get('/api/charges/export.csv')
out['export_type'] = r.headers.get('Content-Type')
csv_raw = r.get_data()
out['bom'] = csv_raw[:3] == b'\\xef\\xbb\\xbf'
out['legacy_same'] = client.get('/api/export/csv').get_data() == csv_raw
out['xlsx'] = client.get('/api/charges/export.xlsx').status_code
out['no_header'] = up('/api/charges/import/preview', csv_raw, headers={})[0]
code, p = up('/api/charges/import/preview', csv_raw)
out['roundtrip'] = (code, p.get('summary'))
edited = csv_raw.decode('utf-8-sig').replace(';20;', ';25;')
edited = edited.replace('\\r\\n', '\\r\\n', 1) + ';;;2026-10-02;;;;AC;5;;;;;;;;;;;;;;;;;\\r\\n'
code, p = up('/api/charges/import/preview', edited.encode('utf-8'))
out['preview'] = (code, p.get('summary'))
out['stale'] = up('/api/charges/import/apply', edited.encode('utf-8'), fingerprint='nope')[0]
code, a = up('/api/charges/import/apply', edited.encode('utf-8'), fingerprint=p['fingerprint'])
out['apply'] = (code, a.get('inserted'), a.get('updated'))
with app.app_context():
    out['after'] = sorted((c.date.isoformat(), c.kwh_loaded) for c in Charge.query.all())
page = client.get('/settings').get_data(as_text=True)
out['undo_button'] = 'btnSheetUndo' in page
r = client.post('/api/charges/import/undo', headers=H)
out['undo'] = r.status_code
with app.app_context():
    out['undone'] = sorted((c.date.isoformat(), c.kwh_loaded) for c in Charge.query.all())
out['undo_again'] = client.post('/api/charges/import/undo', headers=H).status_code
out['too_large'] = up('/api/charges/import/preview', b'date\\n' + b'x' * (6 * 1024 * 1024))[0]
print(json.dumps(out))
"""


def test_routes_check_rights_guard_size_and_undo():
    d = tempfile.mkdtemp(prefix='evct-sheet-')
    env = dict(os.environ, EV_DATA_DIR=d, SECRET_KEY='sheet-test')
    proc = subprocess.run([sys.executable, '-c', _PREAMBLE + _FLOW], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads([ln for ln in proc.stdout.split('\n') if ln.startswith('{')][-1])
    assert out['export_logged_out'] == 302           # to the login page
    assert out['preview_logged_out'] == 401
    assert out['export_type'].startswith('text/csv') and out['bom']
    assert out['legacy_same']
    assert out['xlsx'] in (200, 501)
    assert out['no_header'] == 403                    # cross-site form post refused
    code, s = out['roundtrip']
    assert code == 200 and s['changed'] == 0 and s['unchanged'] == 1
    code, s = out['preview']
    assert code == 200 and (s['changed'], s['new'], s['invalid']) == (1, 1, 0), s
    assert out['stale'] == 409
    assert out['apply'] == [200, 1, 1]
    assert out['after'] == [['2026-10-01', 25.0], ['2026-10-02', 5.0]]
    assert out['undo_button']
    assert out['undo'] == 200
    assert out['undone'] == [['2026-10-01', 20.0]]
    assert out['undo_again'] == 404
    assert out['too_large'] == 413
    backups = os.listdir(os.path.join(d, 'backups'))
    assert any(n.startswith('pre_import_') for n in backups), backups
    assert any(n.startswith('charges-import-') for n in backups), backups
