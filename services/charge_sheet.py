# -*- coding: utf-8 -*-
"""Spreadsheet round trip for charges: export, edit elsewhere, import back.

The export carries the stable database ID of every charge. The import
matches rows by that ID and never writes anything before the user has seen
a plan of what would happen:

* ``new``        – row without an ID, becomes a new charge
* ``changed``    – row with an ID whose values differ (field: old → new)
* ``unchanged``  – row with an ID whose values are what the database holds
* ``delete``     – row with an ID and a yes in the ``delete`` column
* ``invalid``    – row that cannot be taken over (line number + reason)

Rules that keep an edited file from doing damage:

* A charge that is missing from the file is **not** deleted. Only an explicit
  yes in the ``delete`` column deletes.
* A column that is missing from the file leaves that field alone; it does not
  clear it.
* Values are compared at the precision the export wrote them, so exporting
  and importing an untouched file changes nothing at all.
* ``soc_charged``, ``loss_pct`` and ``co2_kg`` are calculated by the app and
  are ignored on import. ``total_cost`` is taken over, and recalculated the
  same way the edit form does whenever price and kWh are set.
* Before a plan is applied, the whole database file is copied (the existing
  pre-import backup) and every charge is written to a JSON journal. The
  journal is what "undo last import" restores from.

Text cells that start with ``= + - @`` (or a tab / carriage return) are
prefixed with an apostrophe on export, so a spreadsheet program does not run
them as a formula. The import strips exactly that prefix again.

Nothing in here talks to Flask directly; routes in ``app.py`` pass in what
they need (battery capacity, charge efficiency, translator, paths).
"""
from __future__ import annotations

import csv
import glob
import hashlib
import io
import json
import os
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import class_mapper

from models.database import db, Charge, Vehicle

FORMAT_VERSION = 1

#: Upload limit for an edited file. A year of daily charges is ~60 KB as CSV.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
#: Hard cap on data rows, so a huge (or hostile) file cannot tie up a worker.
MAX_ROWS = 50000
#: Journals kept for "undo last import".
KEEP_JOURNALS = 10

# (column, kind, role)
#   kind: 'int' | 'date' | 'text' | 'type' | 'flag' | <int> decimals
#   role: 'key' | 'input' | 'info' | 'derived' | 'action'
COLUMNS = (
    ('id',               'int',  'key'),
    ('vehicle_id',       'int',  'input'),
    ('vehicle',          'text', 'info'),
    ('date',             'date', 'input'),
    ('charge_hour',      'int',  'input'),
    ('charge_end_hour',  'int',  'input'),
    ('odometer_km',      'int',  'input'),
    ('charge_type',      'type', 'input'),
    ('kwh_loaded',       3,      'input'),
    ('eur_per_kwh',      4,      'input'),
    ('start_fee_eur',    2,      'input'),
    ('blocking_fee_eur', 2,      'input'),
    ('total_cost',       2,      'input'),
    ('soc_from',         'int',  'input'),
    ('soc_to',           'int',  'input'),
    ('soc_charged',      'int',  'derived'),
    ('loss_kwh',         3,      'input'),
    ('loss_pct',         2,      'derived'),
    ('co2_g_per_kwh',    'int',  'input'),
    ('co2_kg',           2,      'derived'),
    ('operator',         'text', 'input'),
    ('location_name',    'text', 'input'),
    ('location_lat',     6,      'input'),
    ('location_lon',     6,      'input'),
    ('notes',            'text', 'input'),
    ('delete',           'flag', 'action'),
)
COLUMN_NAMES = tuple(c[0] for c in COLUMNS)
_KIND = {c[0]: c[1] for c in COLUMNS}
_ROLE = {c[0]: c[2] for c in COLUMNS}
#: Column name → Charge attribute where the two differ.
_ATTR: dict[str, str] = {'odometer_km': 'odometer'}
INPUT_COLUMNS = tuple(c[0] for c in COLUMNS if c[2] == 'input')
DERIVED_COLUMNS = ('total_cost', 'soc_charged', 'loss_pct', 'co2_kg')
#: Inputs the app's calculation reads. Editing only a note or a place name
#: leaves the calculated values alone.
_CALC_INPUTS = {'vehicle_id', 'date', 'charge_hour', 'charge_type',
                'kwh_loaded', 'eur_per_kwh', 'start_fee_eur',
                'blocking_fee_eur', 'total_cost', 'soc_from', 'soc_to',
                'loss_kwh', 'co2_g_per_kwh'}
#: Text length limits, from the database columns.
_MAX_LEN = {'operator': 64, 'location_name': 200, 'notes': 5000}

#: Inclusive value ranges checked on new and changed rows.
_RANGES = {
    'charge_hour': (0, 23), 'charge_end_hour': (0, 23),
    'soc_from': (0, 100), 'soc_to': (0, 100),
    'location_lat': (-90, 90), 'location_lon': (-180, 180),
}
#: Must not be negative.
_NON_NEGATIVE = ('odometer_km', 'kwh_loaded', 'eur_per_kwh', 'start_fee_eur',
                 'blocking_fee_eur', 'total_cost', 'loss_kwh', 'co2_g_per_kwh')

_FORMULA_START = ('=', '+', '-', '@', '\t', '\r')

# Accepted in the ``delete`` column, in all six app languages.
_YES = {'yes', 'y', 'ja', 'j', 'x', '1', 'true', 'wahr', 'oui', 'vrai', 'si',
        'sí', 'vero', 'verdadero', 'delete', 'löschen', 'loeschen',
        'supprimer', 'eliminar', 'elimina', 'cancella', 'verwijderen',
        'waar'}
_NO = {'', 'no', 'n', 'nein', '0', 'false', 'falsch', 'non', 'faux', 'falso',
       'nee', 'onwaar', '-'}

#: Languages that write a decimal comma.
_COMMA_LANGS = {'de', 'fr', 'es', 'it', 'nl'}


class SheetError(Exception):
    """The file as a whole cannot be read. ``code`` is an i18n suffix."""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def attr_of(column: str) -> str:
    return _ATTR.get(column) or column


def decimal_comma_for(lang):
    return (lang or '').split('-')[0].lower() in _COMMA_LANGS


def xlsx_available():
    try:
        import openpyxl  # noqa: F401
        return True
    except Exception:
        return False


# ── export ────────────────────────────────────────────────────────────
def query_charges(vehicle_id=None, date_from=None, date_to=None):
    q = Charge.query
    if vehicle_id is not None:
        q = q.filter(Charge.vehicle_id == vehicle_id)
    if date_from is not None:
        q = q.filter(Charge.date >= date_from)
    if date_to is not None:
        q = q.filter(Charge.date <= date_to)
    return q.order_by(Charge.date.asc(), Charge.charge_hour.asc(),
                      Charge.id.asc()).all()


def mask_formula(text):
    """Prefix a text cell that a spreadsheet would run as a formula."""
    if text and text.startswith(_FORMULA_START):
        return "'" + text
    return text


def unmask_formula(text):
    """Undo :func:`mask_formula` — only for the exact prefix it adds."""
    if text and len(text) > 1 and text[0] == "'" and text[1] in _FORMULA_START:
        return text[1:]
    return text


def _vehicle_names():
    try:
        return {v.id: (v.name or '') for v in Vehicle.query.all()}
    except Exception:
        return {}


def _typed_value(charge, column, names):
    """The value of ``column`` for ``charge``, rounded like the export."""
    if column == 'vehicle':
        return names.get(charge.vehicle_id) or None
    if column == 'delete':
        return None
    v = getattr(charge, attr_of(column), None)
    kind = _KIND[column]
    if v is None:
        return None
    if isinstance(kind, int):
        return round(float(v), kind)
    if kind == 'int':
        return int(v)
    return v


def _format_number(value, decimals, comma):
    if value is None:
        return ''
    if decimals == 0:
        s = str(int(round(value)))
    else:
        s = f'{value:.{decimals}f}'.rstrip('0').rstrip('.')
        if s in ('-0', ''):
            s = '0'
    return s.replace('.', ',') if comma else s


def display_value(column: str, value: Any, comma: bool) -> str:
    """A value as the preview shows it: dates as ISO, numbers at export
    precision with the language's decimal mark, empty as ''."""
    if value is None:
        return ''
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    kind = _KIND.get(column)
    if isinstance(kind, int) and isinstance(value, (int, float)):
        return _format_number(float(value), kind, comma)
    return str(value)


def export_rows(charges) -> list[list[Any]]:
    """Header plus one list of typed cells per charge (None = empty)."""
    names = _vehicle_names()
    rows: list[list[Any]] = [list(COLUMN_NAMES)]
    for c in charges:
        rows.append([_typed_value(c, col, names) for col in COLUMN_NAMES])
    return rows


def export_csv(charges, comma):
    """UTF-8 with BOM, ``;`` separated — opens straight in a spreadsheet
    program. Decimal mark: comma when ``comma`` is true, else a point."""
    out = io.StringIO()
    out.write('﻿')
    w = csv.writer(out, delimiter=';', lineterminator='\r\n')
    rows = export_rows(charges)
    w.writerow(rows[0])
    for row in rows[1:]:
        cells = []
        for col, v in zip(COLUMN_NAMES, row):
            kind = _KIND[col]
            if v is None:
                cells.append('')
            elif isinstance(kind, int):
                cells.append(_format_number(v, kind, comma))
            elif kind == 'int':
                cells.append(str(v))
            elif kind == 'date' and isinstance(v, date):
                cells.append(v.isoformat())
            else:
                cells.append(mask_formula(str(v)))
        w.writerow(cells)
    return out.getvalue().encode('utf-8')


def export_xlsx(charges):
    """One sheet, typed cells (numbers stay numbers, dates stay dates),
    frozen header, filter on. Needs openpyxl."""
    from openpyxl import Workbook
    from openpyxl.cell.cell import Cell
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    if ws is None:  # a fresh workbook always has one sheet
        ws = wb.create_sheet()
    ws.title = 'charges'
    rows = export_rows(charges)
    ws.append(rows[0])
    for row in rows[1:]:
        ws.append([None] * len(COLUMN_NAMES))
        r = ws.max_row
        for i, (col, v) in enumerate(zip(COLUMN_NAMES, row), start=1):
            if v is None:
                continue
            cell = ws.cell(row=r, column=i)
            if not isinstance(cell, Cell):  # merged cells take no value
                continue
            kind = _KIND[col]
            if kind in ('text', 'type'):
                # data_type 's' keeps a leading '=' a string; the mask also
                # protects whoever saves this sheet as CSV later.
                cell.value = mask_formula(str(v))
                cell.data_type = 's'
            elif kind == 'date':
                cell.value = v
                cell.number_format = 'yyyy-mm-dd'
            else:
                cell.value = v
                if isinstance(kind, int):
                    cell.number_format = '0.' + '0' * kind if kind else '0'
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(len(COLUMN_NAMES))}{ws.max_row}'
    widths = {'notes': 40, 'location_name': 28, 'operator': 22, 'vehicle': 18,
              'date': 12}
    for i, col in enumerate(COLUMN_NAMES, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(
            col, max(10, len(col) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── reading a file ────────────────────────────────────────────────────
def _decode(raw):
    # Spreadsheet programs write UTF-8 with a BOM, or the Windows code
    # page. Try UTF-8 first; it fails loudly on cp1252 umlauts.
    try:
        return raw.decode('utf-8-sig'), 'utf-8'
    except UnicodeDecodeError:
        return raw.decode('cp1252', errors='replace'), 'cp1252'


def _detect_delimiter(first_line):
    best, best_n = ';', 0
    for d in (';', ',', '\t'):
        n = len(next(csv.reader([first_line], delimiter=d), []))
        if n > best_n:
            best, best_n = d, n
    return best


def read_table(raw):
    """Return ``(rows, meta)`` — rows are lists of cells (str or typed
    values from XLSX). Raises :class:`SheetError` when unreadable."""
    if not raw:
        raise SheetError('empty')
    if raw[:4] == b'PK\x03\x04':
        if not xlsx_available():
            raise SheetError('xlsx_unsupported')
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = []
            for row in ws.iter_rows(max_col=len(COLUMN_NAMES) + 20,
                                    values_only=True):
                rows.append(list(row))
                if len(rows) > MAX_ROWS + 1:
                    raise SheetError('too_many_rows', max=MAX_ROWS)
            wb.close()
        except SheetError:
            raise
        except Exception:
            raise SheetError('unreadable')
        return rows, {'format': 'xlsx', 'delimiter': None, 'encoding': None}
    text, enc = _decode(raw)
    if '\x00' in text:
        raise SheetError('unreadable')
    first = text.split('\n', 1)[0].rstrip('\r')
    delim = _detect_delimiter(first)
    try:
        rows = []
        for row in csv.reader(io.StringIO(text), delimiter=delim):
            rows.append(row)
            if len(rows) > MAX_ROWS + 1:
                raise SheetError('too_many_rows', max=MAX_ROWS)
    except csv.Error:
        raise SheetError('unreadable')
    return rows, {'format': 'csv', 'delimiter': delim, 'encoding': enc}


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


# ── parsing single cells ──────────────────────────────────────────────
class _CellError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_number(v):
    """Accepts 1234.5, 1234,5, 1.234,5, 1,234.5 and numbers from XLSX."""
    if _blank(v):
        return None
    if isinstance(v, bool):
        raise _CellError('invalid_number')
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(' ', '').replace(' ', '').replace("'", '')
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        if s.count(',') > 1:
            raise _CellError('invalid_number')
        s = s.replace(',', '.')
    if not re.fullmatch(r'[+-]?(\d+(\.\d*)?|\.\d+)', s):
        raise _CellError('invalid_number')
    return float(s)


def parse_int(v):
    f = parse_number(v)
    if f is None:
        return None
    if abs(f - round(f)) > 1e-6:
        raise _CellError('invalid_integer')
    return int(round(f))


def parse_hour(v):
    if isinstance(v, (datetime, time)):
        return v.hour
    if not _blank(v) and isinstance(v, str) and ':' in v:
        v = v.split(':', 1)[0]
    return parse_int(v)


def parse_date(v, current=None):
    """ISO (2026-11-05), dotted (05.11.2026) and slashed dates. A slashed
    date that reads both ways (03/04/2026) is resolved by the value the
    database already has, else refused as ambiguous."""
    if _blank(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        raise _CellError('invalid_date')
    s = str(v).strip()
    m = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?', s)
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})', s)
    if m:
        return _mk_date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})', s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        options = []
        for d_, m_ in ((a, b), (b, a)):
            try:
                options.append(date(y, m_, d_))
            except ValueError:
                pass
        options = list(dict.fromkeys(options))
        if len(options) == 1:
            return options[0]
        if current is not None and current in options:
            return current
        if options:
            raise _CellError('ambiguous_date')
    raise _CellError('invalid_date')


def _year(s):
    y = int(s)
    return 2000 + y if y < 100 else y


def _mk_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        raise _CellError('invalid_date')


def parse_text(v):
    if _blank(v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return unmask_formula(str(v).strip())


def parse_type(v):
    s = parse_text(v)
    if s is None:
        return None
    s = s.upper()
    if s not in ('AC', 'DC', 'PV'):
        raise _CellError('invalid_type')
    return s


def parse_flag(v):
    if _blank(v):
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        raise _CellError('invalid_delete')
    s = str(v).strip().lower()
    if s in _YES:
        return True
    if s in _NO:
        return False
    raise _CellError('invalid_delete')


def parse_cell(column, v, current=None):
    kind = _KIND[column]
    if kind == 'date':
        return parse_date(v, current)
    if column in ('charge_hour', 'charge_end_hour'):
        return parse_hour(v)
    if kind == 'int':
        return parse_int(v)
    if isinstance(kind, int):
        f = parse_number(v)
        return f
    if kind == 'type':
        return parse_type(v)
    if kind == 'flag':
        return parse_flag(v)
    return parse_text(v)


def same_value(column, a, b):
    """Equal at the precision the export writes."""
    kind = _KIND[column]
    if kind == 'text':
        # Trailing blanks and line-ending style are not an edit.
        a = (a or '').replace('\r\n', '\n').strip() or None
        b = (b or '').replace('\r\n', '\n').strip() or None
    if a is None or b is None:
        return a is None and b is None
    if isinstance(kind, int):
        return abs(round(float(a), kind) - round(float(b), kind)) < 10 ** -(kind + 1)
    return a == b


# ── the plan ──────────────────────────────────────────────────────────
def _charge_state(c) -> dict[str, Any]:
    """Input + derived values of a charge, keyed by column name."""
    return {col: getattr(c, attr_of(col), None)
            for col in INPUT_COLUMNS + ('soc_charged', 'loss_pct', 'co2_kg')}


def _dedup_key(vehicle_id, d, hour, kwh):
    return (vehicle_id, d, hour if hour is not None else -1,
            round(kwh, 1) if kwh is not None else None)


def _validate(state, is_new, vehicle_ids, today):
    """Errors as ``[(column, code)]`` for the state a row would end up in."""
    errs = []
    if state.get('date') is None:
        errs.append(('date', 'required'))
    else:
        if state['date'] > today + timedelta(days=1):
            errs.append(('date', 'future_date'))
        if state['date'].year < 2000:
            errs.append(('date', 'out_of_range'))
    if is_new and state.get('kwh_loaded') is None:
        errs.append(('kwh_loaded', 'required'))
    vid = state.get('vehicle_id')
    if vid is None or vid not in vehicle_ids:
        errs.append(('vehicle_id', 'unknown_vehicle'))
    for col in _NON_NEGATIVE:
        v = state.get(col)
        if v is not None and v < 0:
            errs.append((col, 'negative'))
    for col, (lo, hi) in _RANGES.items():
        v = state.get(col)
        if v is not None and not (lo <= v <= hi):
            errs.append((col, 'out_of_range'))
    sf, st = state.get('soc_from'), state.get('soc_to')
    if sf is not None and st is not None and sf > st:
        errs.append(('soc_to', 'soc_order'))
    for col, n in _MAX_LEN.items():
        v = state.get(col)
        if v is not None and len(v) > n:
            errs.append((col, 'too_long'))
    return errs


def _derive(state, battery_kwh_fn, efficiency_fn, prev=None):
    """Run the app's own calculation on a detached copy and return the
    resulting state. ``prev`` is the database state of an existing row, for
    the same charge-type rule the edit form applies."""
    tmp = Charge()
    for col in INPUT_COLUMNS:
        setattr(tmp, attr_of(col), state.get(col))
    if prev is not None:
        tmp.soc_charged = prev.get('soc_charged')
        tmp.loss_pct = prev.get('loss_pct')
        tmp.co2_kg = prev.get('co2_kg')
        # Same rule as the edit form: switching AC/PV <-> DC voids a loss
        # derived for the old type, unless the loss itself was edited.
        if (prev.get('charge_type') != state.get('charge_type')
                and same_value('loss_kwh', prev.get('loss_kwh'), state.get('loss_kwh'))):
            tmp.loss_kwh = None
            tmp.loss_pct = None
    vid = state.get('vehicle_id')
    try:
        bk = battery_kwh_fn(vid) if battery_kwh_fn else None
    except Exception:
        bk = None
    try:
        eff = efficiency_fn(vid) if efficiency_fn else None
    except Exception:
        eff = None
    tmp.calculate_fields(bk, eff)
    out = dict(state)
    for col in ('total_cost', 'soc_charged', 'loss_kwh', 'loss_pct', 'co2_kg'):
        out[col] = getattr(tmp, attr_of(col), None)
    if out.get('co2_g_per_kwh') is None:
        # No intensity, no CO2 mass: a stale one would belong to the old row.
        out['co2_kg'] = None
    return out


def _fingerprint(rows):
    h = hashlib.sha256()
    for r in rows:
        if r['status'] in ('new', 'changed', 'delete'):
            h.update(json.dumps([r['line'], r['status'], r.get('id'),
                                 [[c['field'], c['new']] for c in r.get('changes', [])]],
                                default=str, sort_keys=True).encode())
    return h.hexdigest()[:32]


def build_plan(raw, default_vehicle_id=None, battery_kwh_fn=None,
               efficiency_fn=None, today=None):
    """Read an edited file and decide, per row, what an import would do.
    Touches nothing. Raises :class:`SheetError` for an unreadable file."""
    today = today or date.today()
    table, meta = read_table(raw)
    # Skip leading blank lines.
    while table and all(_blank(c) for c in table[0]):
        table.pop(0)
    if not table:
        raise SheetError('empty')
    header = [(str(h).replace('﻿', '').strip().lower() if h is not None else '')
              for h in table[0]]
    col_index = {}
    ignored = []
    for i, h in enumerate(header):
        if h in _KIND and h not in col_index:
            col_index[h] = i
        elif h:
            ignored.append(h)
    if 'date' not in col_index:
        raise SheetError('no_header')

    charges = {c.id: c for c in Charge.query.all()}
    vehicle_ids = {v.id for v in Vehicle.query.all()}
    if default_vehicle_id not in vehicle_ids:
        default_vehicle_id = min(vehicle_ids) if vehicle_ids else None

    existing_keys = {}
    for c in charges.values():
        existing_keys.setdefault(_dedup_key(c.vehicle_id, c.date, c.charge_hour,
                                            c.kwh_loaded), c.id)

    def cell(row, col):
        i = col_index.get(col)
        if i is None or i >= len(row):
            return None
        return row[i]

    # IDs that appear more than once — every occurrence is refused, because
    # nothing says which of them is the intended one.
    id_lines = {}
    for n, row in enumerate(table[1:], start=2):
        if all(_blank(c) for c in row):
            continue
        try:
            rid = parse_int(cell(row, 'id'))
        except _CellError:
            continue
        if rid is not None:
            id_lines.setdefault(rid, []).append(n)

    rows = []
    new_keys = {}
    for n, row in enumerate(table[1:], start=2):
        if all(_blank(c) for c in row):
            continue
        entry = {'line': n, 'status': None, 'id': None, 'errors': [],
                 'changes': []}
        rows.append(entry)
        try:
            rid = parse_int(cell(row, 'id'))
        except _CellError:
            entry['status'] = 'invalid'
            entry['errors'].append(('id', 'invalid_id'))
            continue
        entry['id'] = rid
        try:
            wants_delete = parse_flag(cell(row, 'delete')) if 'delete' in col_index else False
        except _CellError as e:
            entry['status'] = 'invalid'
            entry['errors'].append(('delete', e.code))
            continue
        if rid is not None:
            if len(id_lines.get(rid, [])) > 1:
                entry['status'] = 'invalid'
                entry['errors'].append(('id', 'duplicate_id'))
                continue
            if rid not in charges:
                entry['status'] = 'invalid'
                entry['errors'].append(('id', 'unknown_id'))
                continue
        current = charges.get(rid) if rid is not None else None
        if current is not None:
            entry['date'] = current.date.isoformat() if current.date else None
        if wants_delete:
            if current is None:
                entry['status'] = 'invalid'
                entry['errors'].append(('delete', 'delete_without_id'))
                continue
            entry['status'] = 'delete'
            continue

        # Database state of an existing row; empty for a new one.
        prev: dict[str, Any] = _charge_state(current) if current is not None else {}
        parsed: dict[str, Any] = {}
        for col in INPUT_COLUMNS:
            if col not in col_index:
                continue
            try:
                parsed[col] = parse_cell(col, cell(row, col), prev.get(col))
            except _CellError as e:
                entry['errors'].append((col, e.code))
        if entry['errors']:
            entry['status'] = 'invalid'
            continue

        if current is not None:
            if 'vehicle_id' in parsed and parsed['vehicle_id'] is None:
                parsed['vehicle_id'] = prev.get('vehicle_id')  # blank = keep
            changed = [col for col, v in parsed.items()
                       if not same_value(col, v, prev.get(col))]
            if not changed:
                entry['status'] = 'unchanged'
                continue
            state: dict[str, Any] = dict(prev)
            for col in changed:
                state[col] = parsed[col]
            if state.get('vehicle_id') is None:
                state['vehicle_id'] = prev.get('vehicle_id')
            # A new date or start hour makes the stored grid intensity belong
            # to another moment: clear it so the CO2 backfill fetches it anew.
            if (('date' in changed or 'charge_hour' in changed)
                    and 'co2_g_per_kwh' not in changed):
                state['co2_g_per_kwh'] = None
            errs = _validate(state, False, vehicle_ids, today)
            if errs:
                entry['status'] = 'invalid'
                entry['errors'] = errs
                continue
            if _CALC_INPUTS.intersection(changed) or state.get('co2_g_per_kwh') != prev.get('co2_g_per_kwh'):
                final = _derive(state, battery_kwh_fn, efficiency_fn, prev)
            else:
                final = state
            entry['status'] = 'changed'
            entry['values'] = {col: final.get(col) for col in final}
            entry['changes'] = [
                {'field': col, 'old': prev.get(col), 'new': final.get(col)}
                for col in INPUT_COLUMNS + ('soc_charged', 'loss_pct', 'co2_kg')
                if not same_value(col, final.get(col), prev.get(col))]
            if not entry['changes']:
                entry['status'] = 'unchanged'
            entry['refetch_co2'] = state.get('co2_g_per_kwh') is None
            continue

        # New charge.
        state = {col: parsed.get(col) for col in INPUT_COLUMNS}
        if state.get('vehicle_id') is None:
            state['vehicle_id'] = default_vehicle_id
        errs = _validate(state, True, vehicle_ids, today)
        if not errs:
            key = _dedup_key(state['vehicle_id'], state['date'],
                             state.get('charge_hour'), state.get('kwh_loaded'))
            if key in existing_keys:
                errs.append(('date', 'duplicate_charge'))
                entry['duplicate_of'] = existing_keys[key]
            elif key in new_keys:
                errs.append(('date', 'duplicate_in_file'))
                entry['duplicate_of_line'] = new_keys[key]
            else:
                new_keys[key] = n
        if errs:
            entry['status'] = 'invalid'
            entry['errors'] = errs
            continue
        final = _derive(state, battery_kwh_fn, efficiency_fn)
        entry['status'] = 'new'
        entry['date'] = final['date'].isoformat()
        entry['values'] = final
        entry['changes'] = [{'field': col, 'old': None, 'new': final.get(col)}
                            for col in INPUT_COLUMNS + ('soc_charged', 'loss_pct', 'co2_kg')
                            if final.get(col) is not None]
        entry['refetch_co2'] = final.get('co2_g_per_kwh') is None

    summary = {'total': len(rows)}
    for s in ('new', 'changed', 'unchanged', 'delete', 'invalid'):
        summary[s] = sum(1 for r in rows if r['status'] == s)
    return {
        'format': meta['format'],
        'delimiter': meta['delimiter'],
        'encoding': meta['encoding'],
        'columns_used': [c for c in COLUMN_NAMES if c in col_index],
        'columns_ignored': ignored,
        'has_id_column': 'id' in col_index,
        'summary': summary,
        'rows': rows,
        'fingerprint': _fingerprint(rows),
    }


# ── journal (backup + undo) ───────────────────────────────────────────
def _col_value(col, v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _charge_columns():
    """The table columns of ``charges`` (via the mapper, which type
    checkers can see, rather than the dynamic ``__table__``)."""
    return list(class_mapper(Charge).columns)


def _row_from_json(data: dict[str, Any]) -> dict[str, Any]:
    """Turn a journal row back into column values with the right types."""
    out: dict[str, Any] = {}
    for col in _charge_columns():
        if col.name not in data:
            continue
        v = data[col.name]
        if v is not None:
            pt = col.type.python_type
            if pt is datetime:
                v = datetime.fromisoformat(v)
            elif pt is date:
                v = date.fromisoformat(v)
            elif pt is bool:
                v = bool(v)
        out[col.name] = v
    return out


def snapshot_charges():
    cols = [c.name for c in _charge_columns()]
    return [{name: _col_value(name, getattr(c, name)) for name in cols}
            for c in Charge.query.order_by(Charge.id).all()]


def _journal_paths(backup_dir):
    return sorted(glob.glob(os.path.join(backup_dir, 'charges-import-*.json')),
                  reverse=True)


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _prune(backup_dir):
    for p in _journal_paths(backup_dir)[KEEP_JOURNALS:]:
        try:
            os.remove(p)
        except OSError:
            pass


def last_import(backup_dir):
    """The newest import that can still be undone, or None."""
    for p in _journal_paths(backup_dir):
        try:
            with open(p, encoding='utf-8') as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        if j.get('state') == 'applied':
            j['path'] = p
            return j
    return None


def apply_plan(plan, backup_dir, filename=None, release_fn=None):
    """Write a plan from :func:`build_plan`. The caller must have rebuilt the
    plan from the same file right before, so it reflects the database now.

    Returns the journal entry (counts, ids, path)."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    path = os.path.join(backup_dir, f'charges-import-{stamp}.json')
    journal = {
        'format': FORMAT_VERSION,
        'state': 'pending',
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'file': (filename or '')[:200],
        'snapshot': snapshot_charges(),
        'inserted_ids': [], 'updated_ids': [], 'deleted_ids': [],
    }
    # The snapshot is on disk before the first row changes.
    _write_json(path, journal)
    refetch = False
    try:
        for r in plan['rows']:
            st = r['status']
            if st == 'changed':
                c = db.session.get(Charge, r['id'])
                if c is None:  # gone since the plan was built: nothing to change
                    continue
                for col, v in r['values'].items():
                    setattr(c, attr_of(col), v)
                changed_fields = {ch['field'] for ch in r['changes']}
                if 'co2_g_per_kwh' in changed_fields:
                    c.co2_estimated = False
                    c.co2_attempts = 0
                journal['updated_ids'].append(c.id)
                refetch = refetch or r.get('refetch_co2', False)
            elif st == 'new':
                c = Charge(**{attr_of(col): v for col, v in r['values'].items()
                              if v is not None})
                db.session.add(c)
                db.session.flush()
                journal['inserted_ids'].append(c.id)
                refetch = refetch or r.get('refetch_co2', False)
            elif st == 'delete':
                c = db.session.get(Charge, r['id'])
                if c is None:
                    continue
                if release_fn is not None:
                    release_fn(c)
                db.session.delete(c)
                journal['deleted_ids'].append(r['id'])
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    journal['state'] = 'applied'
    journal['applied_at'] = datetime.now().isoformat(timespec='seconds')
    _write_json(path, journal)
    _prune(backup_dir)
    return {'path': path, 'refetch_co2': refetch,
            'inserted': len(journal['inserted_ids']),
            'updated': len(journal['updated_ids']),
            'deleted': len(journal['deleted_ids'])}


def undo_last(backup_dir, release_fn=None):
    """Put every charge the newest applied import touched back the way it
    was before that import. Returns counts, or None if nothing to undo."""
    j = last_import(backup_dir)
    if j is None:
        return None
    before = {row['id']: row for row in j.get('snapshot', [])}
    removed = restored = recreated = 0
    try:
        for cid in j.get('inserted_ids', []):
            c = db.session.get(Charge, cid)
            if c is not None:
                if release_fn is not None:
                    release_fn(c)
                db.session.delete(c)
                removed += 1
        db.session.flush()
        for cid in j.get('updated_ids', []):
            data = before.get(cid)
            if data is None:
                continue
            values = _row_from_json(data)
            c = db.session.get(Charge, cid)
            if c is None:
                # Deleted by hand since the import: bring back the original.
                values.pop('wallbox_charge_id', None)
                db.session.add(Charge(**values))
                recreated += 1
                continue
            for name, v in values.items():
                if name in ('id', 'wallbox_charge_id'):
                    continue
                setattr(c, name, v)
            restored += 1
        for cid in j.get('deleted_ids', []):
            data = before.get(cid)
            if data is None:
                continue
            values = _row_from_json(data)
            # The meter reading was released when the row was deleted and
            # may belong to another charge by now: do not re-link it.
            values.pop('wallbox_charge_id', None)
            if db.session.get(Charge, cid) is not None:
                values.pop('id', None)  # id reused meanwhile → new id
            db.session.add(Charge(**values))
            recreated += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    path = j.pop('path')
    j['state'] = 'undone'
    j['undone_at'] = datetime.now().isoformat(timespec='seconds')
    _write_json(path, j)
    return {'removed': removed, 'restored': restored, 'recreated': recreated}
