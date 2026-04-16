"""Import charging data from a CSV file.

Tolerant of different CSV formats:
- Auto-detects delimiter (comma, semicolon, or tab) from the first non-empty line.
- Auto-detects date format (mm/dd/yyyy, dd.mm.yyyy, yyyy-mm-dd, dd/mm/yyyy).
- Header-based column mapping with fuzzy/case-insensitive matching so the
  columns of our own export (German headers with semicolons) and Google
  Sheet exports (no headers, comma-separated) both work. Falls back to
  position-based mapping when no header row can be detected.

Four import modes protect existing data:
- ``skip`` (default): rows whose ``(date, charge_hour, kwh_loaded)`` already
  exist in the DB are skipped — **manual entries are never overwritten**.
- ``update``: skip-dedup behavior, but if a matching row has a NULL field
  that the CSV provides, patch in the CSV value.
- ``append``: insert every CSV row unconditionally (for users who want
  duplicates, e.g. two separate charges at the same time).
- ``replace``: the legacy nuclear option — DELETE FROM charges, then
  INSERT. Requires double-confirmation in the UI. Always writes a DB
  backup into ``DATA_DIR/backups/`` first, so the user can undo.
"""
import csv
import io
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, date
from difflib import SequenceMatcher

# ── Auto-setup and activate venv (CLI path only) ──────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(SCRIPT_DIR, 'venv')
REQUIREMENTS = os.path.join(SCRIPT_DIR, 'requirements.txt')

if sys.platform == 'win32':
    VENV_PYTHON = os.path.join(VENV_DIR, 'Scripts', 'python.exe')
else:
    VENV_PYTHON = os.path.join(VENV_DIR, 'bin', 'python3')


def _ensure_venv():
    if not os.path.exists(VENV_PYTHON):
        print("📦 Erstelle virtuelle Umgebung...")
        subprocess.check_call([sys.executable, '-m', 'venv', VENV_DIR])
    print("📥 Installiere Abhängigkeiten...")
    subprocess.check_call([VENV_PYTHON, '-m', 'pip', 'install', '-q', '-r', REQUIREMENTS])


_in_venv = os.path.realpath(sys.executable) == os.path.realpath(VENV_PYTHON)

if not _in_venv and __name__ == '__main__':
    _ensure_venv()
    print("🔄 Starte mit virtueller Umgebung...")
    if sys.platform == 'win32':
        result = subprocess.run([VENV_PYTHON] + sys.argv)
        sys.exit(result.returncode)
    else:
        os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

sys.path.insert(0, SCRIPT_DIR)

from app import create_app
from models.database import db, Charge


# ── Date parsing ──────────────────────────────────────────
_DATE_PATTERNS = (
    ('%Y-%m-%d',   re.compile(r'^\d{4}-\d{1,2}-\d{1,2}$')),
    ('%d.%m.%Y',   re.compile(r'^\d{1,2}\.\d{1,2}\.\d{4}$')),
    ('%m/%d/%Y',   re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')),  # Google Sheet US
    ('%d/%m/%Y',   re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')),  # last-resort EU
)


def _parse_date(s):
    """Parse a date string in any of the known formats. Returns a date
    object or None if nothing matches."""
    if not s:
        return None
    s = s.strip()
    for fmt, pattern in _DATE_PATTERNS:
        if pattern.match(s):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return None


def _is_date(s):
    return _parse_date(s) is not None


# ── Number parsing ────────────────────────────────────────
def parse_german_float(s):
    """Parse '1.234,56' → 1234.56, '0,29' → 0.29, '1.5' → 1.5."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    # Heuristic: if both '.' and ',' present and ',' comes last → German
    if '.' in s and ',' in s and s.rfind(',') > s.rfind('.'):
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s and '.' not in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def parse_int_safe(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    f = parse_german_float(s)
    if f is None:
        return None
    try:
        return int(round(f))
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_hour(s):
    """Parse '14', '14:00', '14:30' → 14."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if ':' in s:
        s = s.split(':', 1)[0]
    v = parse_int_safe(s)
    if v is None or v < 0 or v > 23:
        return None
    return v


# ── Header-based column mapping ───────────────────────────
# Each logical field maps to an ordered tuple of accepted header aliases.
# Matching is case- and whitespace-insensitive, with a fuzzy fallback for
# minor typos (ratio ≥ 0.82 via SequenceMatcher).
FIELD_ALIASES = {
    'date':          ('datum', 'date', 'tag', 'day'),
    'charge_hour':   ('uhrzeit', 'zeit', 'hour', 'stunde', 'time', 'start_time',
                      'startzeit'),
    'odometer':      ('km', 'km-stand', 'km_stand', 'odometer', 'mileage',
                      'kilometer', 'kmstand'),
    'eur_per_kwh':   ('eur/kwh', '€/kwh', 'eur_per_kwh', 'preis_kwh', 'price_kwh',
                      'preis', 'price'),
    'kwh_loaded':    ('kwh', 'kwh geladen', 'kwh_geladen', 'energy', 'geladen',
                      'energie', 'kwh_loaded'),
    'total_cost':    ('kosten', 'cost', 'total', 'summe', 'gesamt', 'preis_gesamt'),
    'charge_type':   ('typ', 'type', 'art', 'stromart', 'charge_type', 'ladeart'),
    'soc_from':      ('von%', 'von %', 'von', 'from', 'from%', 'soc_from',
                      'start_soc', 'start%', 'start'),
    'soc_to':        ('bis%', 'bis %', 'bis', 'to', 'to%', 'soc_to', 'end_soc',
                      'end%', 'end', 'ziel'),
    'soc_charged':   ('geladen%', 'geladen %', 'geladen_%', 'charged_%',
                      'charged%', 'diff%', 'soc_charged'),
    'loss_kwh':      ('verlust_kwh', 'loss_kwh', 'verluste_kwh', 'verlust',
                      'losses_kwh'),
    'loss_pct':      ('verlust%', 'verlust_%', 'verlust %', 'loss_pct',
                      'loss_%', 'verluste_%'),
    'co2_g_per_kwh': ('co2_g/kwh', 'co2 g/kwh', 'co2_gkwh', 'co2g', 'co2_intensity',
                      'co2_g_per_kwh'),
    'co2_kg':        ('co2_kg', 'co2 kg', 'co2kg', 'co2'),
    'notes':         ('notizen', 'notes', 'comment', 'bemerkung', 'kommentar'),
    'operator':      ('anbieter', 'provider', 'operator', 'cpo', 'betreiber',
                      'network'),
    'location_name': ('ort', 'standort', 'location', 'location_name', 'station'),
    'location_lat':  ('lat', 'latitude', 'breitengrad', 'location_lat'),
    'location_lon':  ('lon', 'lng', 'long', 'longitude', 'laengengrad',
                      'location_lon'),
}


def _normalize_header(s):
    """Lowercase, strip surrounding whitespace, collapse inner whitespace."""
    if s is None:
        return ''
    s = str(s).strip().lower()
    # Remove BOM
    if s.startswith('\ufeff'):
        s = s[1:]
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s)
    return s


def _match_header(header, aliases):
    """Return True if ``header`` matches any of ``aliases`` after
    normalization. Falls back to fuzzy ratio ≥ 0.82."""
    h = _normalize_header(header)
    if not h:
        return False
    for alias in aliases:
        a = _normalize_header(alias)
        if h == a:
            return True
        # Strip trailing punctuation variants for loose equality
        if h.rstrip('%.:').strip() == a.rstrip('%.:').strip():
            return True
    # Fuzzy fallback for typos
    for alias in aliases:
        a = _normalize_header(alias)
        if SequenceMatcher(None, h, a).ratio() >= 0.82:
            return True
    return False


def _build_column_map(header_row):
    """Return {logical_field: column_index} for a detected header row.
    Unmapped fields are simply absent from the returned dict."""
    col_map = {}
    for col_idx, cell in enumerate(header_row):
        for field, aliases in FIELD_ALIASES.items():
            if field in col_map:
                continue
            if _match_header(cell, aliases):
                col_map[field] = col_idx
                break
    return col_map


def _legacy_column_map():
    """Fallback positional mapping matching the old Google Sheet layout.
    Matches the pre-header-detection behavior so existing users don't get
    silent breakage if their export has no headers."""
    return {
        'date':          0,
        'eur_per_kwh':   1,
        'kwh_loaded':    2,
        'total_cost':    3,
        'charge_type':   4,
        'soc_from':      5,
        'soc_to':        6,
        'soc_charged':   7,
        'loss_kwh':      8,
        'loss_pct':      9,
        'co2_g_per_kwh': 10,
        'co2_kg':        11,
    }


# ── Delimiter detection ───────────────────────────────────
_DELIMITERS = (';', ',', '\t', '|')


def _detect_delimiter(sample):
    """Pick the delimiter that yields the most columns on the first few
    lines of ``sample``. Defaults to ',' when everything ties (preserves
    the old behavior for legacy Google Sheet exports)."""
    best = ','
    best_cols = 0
    for delim in _DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delim))
        except csv.Error:
            continue
        # Take the median column count on non-empty rows in the first 5
        counts = [len(r) for r in rows[:5] if any(c.strip() for c in r)]
        if not counts:
            continue
        median = sorted(counts)[len(counts) // 2]
        if median > best_cols:
            best_cols = median
            best = delim
    return best


# ── Dedup helpers ─────────────────────────────────────────
def _dedup_key(charge_date, charge_hour, kwh_loaded):
    """Key used to identify duplicate charges across import/DB.
    kWh is rounded to 1 decimal so minor precision differences don't
    create false negatives (CSV exports lose precision vs in-memory
    floats all the time)."""
    k_kwh = round(kwh_loaded, 1) if kwh_loaded is not None else None
    return (charge_date, charge_hour if charge_hour is not None else -1, k_kwh)


def _existing_keys():
    """Build a dict of ``_dedup_key → Charge`` for every existing row so
    duplicate detection during import is O(1) per CSV row."""
    out = {}
    for c in Charge.query.all():
        out[_dedup_key(c.date, c.charge_hour, c.kwh_loaded)] = c
    return out


# ── Backup before destructive imports ─────────────────────
def _backup_db_before_replace():
    """Copy the live SQLite DB into DATA_DIR/backups/ before a replace
    import. Keeps the last 5 automatic backups."""
    try:
        from config import DATA_DIR
        src = os.path.join(DATA_DIR, 'ev_tracker.db')
        if not os.path.exists(src):
            return None
        backup_dir = os.path.join(DATA_DIR, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dst = os.path.join(backup_dir, f'pre_import_{stamp}.db')
        shutil.copy2(src, dst)
        # Keep only the 5 newest pre_import_*.db files
        files = sorted(
            (f for f in os.listdir(backup_dir) if f.startswith('pre_import_')),
            reverse=True,
        )
        for f in files[5:]:
            try:
                os.remove(os.path.join(backup_dir, f))
            except OSError:
                pass
        return dst
    except Exception:
        return None


# ── The import itself ─────────────────────────────────────
VALID_MODES = ('skip', 'update', 'append', 'replace')


def import_csv_data(file_obj, mode='skip', replace=False):
    """Import charges from a CSV file object. Returns a dict with stats.

    ``mode`` is one of ``skip`` (default), ``update``, ``append``,
    ``replace``. For backwards compatibility ``replace=True`` is still
    accepted and maps to ``mode='replace'``.
    """
    if replace:
        mode = 'replace'
    if mode not in VALID_MODES:
        mode = 'skip'

    # Read the whole stream (we need two passes: detect delimiter + parse)
    data = file_obj.read()
    if isinstance(data, bytes):
        data = data.decode('utf-8', errors='replace')

    delim = _detect_delimiter(data[:4096])
    reader = csv.reader(io.StringIO(data), delimiter=delim)
    rows = [r for r in reader if any((c or '').strip() for c in r)]

    if not rows:
        return {
            'imported': 0, 'updated': 0, 'skipped_dup': 0, 'skipped': 0,
            'errors': [], 'total_db': Charge.query.count(),
            'total_kwh': 0.0, 'total_cost': 0.0, 'mode': mode,
            'delimiter': delim, 'backup': None,
        }

    # Detect header row: first row whose first cell is NOT a date.
    first_cell = (rows[0][0] or '').strip()
    if _is_date(first_cell):
        # No header → legacy Google-Sheet layout
        col_map = _legacy_column_map()
        data_rows = rows
        detected_header = False
    else:
        col_map = _build_column_map(rows[0])
        # A valid header must at least map 'date' to some column.
        if 'date' in col_map:
            data_rows = rows[1:]
            detected_header = True
        else:
            # Couldn't recognize — fall back to legacy positional and
            # treat first row as data (might still start with a date
            # after the initial validation failed due to e.g. summary
            # rows).
            col_map = _legacy_column_map()
            data_rows = rows
            detected_header = False

    backup_path = None
    if mode == 'replace' and Charge.query.count() > 0:
        backup_path = _backup_db_before_replace()
        Charge.query.delete()
        db.session.commit()

    existing = {} if mode in ('replace', 'append') else _existing_keys()

    imported = 0
    updated = 0
    skipped_dup = 0
    skipped = 0
    errors = []

    def _get(row, field):
        idx = col_map.get(field)
        if idx is None or idx >= len(row):
            return None
        val = (row[idx] or '').strip()
        return val or None

    for row_idx, row in enumerate(data_rows, start=2 if detected_header else 1):
        date_raw = _get(row, 'date')
        if not date_raw:
            skipped += 1
            continue
        charge_date = _parse_date(date_raw)
        if charge_date is None:
            skipped += 1
            continue

        try:
            ct_raw = _get(row, 'charge_type')
            charge_type = ct_raw.upper() if ct_raw else None
            if charge_type and charge_type not in ('AC', 'DC', 'PV'):
                # Accept variants like 'ac (11kw)' → 'AC'
                if charge_type.startswith('AC'):
                    charge_type = 'AC'
                elif charge_type.startswith('DC'):
                    charge_type = 'DC'
                elif charge_type.startswith('PV'):
                    charge_type = 'PV'
                else:
                    charge_type = None

            kwh_loaded = parse_german_float(_get(row, 'kwh_loaded'))
            charge_hour = _parse_hour(_get(row, 'charge_hour'))

            # Build the candidate row
            fields = {
                'date':          charge_date,
                'charge_hour':   charge_hour,
                'odometer':      parse_int_safe(_get(row, 'odometer')),
                'eur_per_kwh':   parse_german_float(_get(row, 'eur_per_kwh')),
                'kwh_loaded':    kwh_loaded,
                'total_cost':    parse_german_float(_get(row, 'total_cost')),
                'charge_type':   charge_type,
                'soc_from':      parse_int_safe(_get(row, 'soc_from')),
                'soc_to':        parse_int_safe(_get(row, 'soc_to')),
                'soc_charged':   parse_int_safe(_get(row, 'soc_charged')),
                'loss_kwh':      parse_german_float(_get(row, 'loss_kwh')),
                'loss_pct':      parse_german_float(_get(row, 'loss_pct')),
                'co2_g_per_kwh': parse_int_safe(_get(row, 'co2_g_per_kwh')),
                'co2_kg':        parse_german_float(_get(row, 'co2_kg')),
                'notes':         _get(row, 'notes'),
                'operator':      _get(row, 'operator'),
                'location_name': _get(row, 'location_name'),
                'location_lat':  parse_german_float(_get(row, 'location_lat')),
                'location_lon':  parse_german_float(_get(row, 'location_lon')),
            }

            key = _dedup_key(charge_date, charge_hour, kwh_loaded)
            existing_charge = existing.get(key) if mode in ('skip', 'update') else None

            if existing_charge is not None and mode == 'skip':
                skipped_dup += 1
                continue

            if existing_charge is not None and mode == 'update':
                # Only overwrite existing NULL fields — manual entries
                # keep whatever the user filled in.
                patched = False
                for k, v in fields.items():
                    if v is None:
                        continue
                    if getattr(existing_charge, k, None) in (None, ''):
                        setattr(existing_charge, k, v)
                        patched = True
                if patched:
                    updated += 1
                else:
                    skipped_dup += 1
                continue

            # Insert fresh row
            charge = Charge(**{k: v for k, v in fields.items() if v is not None})
            if (charge.soc_from is not None and charge.soc_to is not None
                    and charge.soc_charged is None):
                charge.soc_charged = charge.soc_to - charge.soc_from
            db.session.add(charge)
            existing[key] = charge  # prevent duplicate-within-file inserts
            imported += 1

        except Exception as e:
            errors.append(f"Zeile {row_idx}: {e}")
            skipped += 1
            continue

    db.session.commit()

    total_kwh = db.session.query(db.func.sum(Charge.kwh_loaded)).scalar() or 0
    total_cost = db.session.query(db.func.sum(Charge.total_cost)).scalar() or 0
    total_db = Charge.query.count()

    return {
        'imported':    imported,
        'updated':     updated,
        'skipped_dup': skipped_dup,
        'skipped':     skipped,
        'errors':      errors,
        'total_db':    total_db,
        'total_kwh':   round(total_kwh, 1),
        'total_cost':  round(total_cost, 2),
        'mode':        mode,
        'delimiter':   delim,
        'header_detected': detected_header,
        'backup':      backup_path,
    }


def import_from_csv(filepath):
    """CLI entry point: import a CSV file from disk."""
    app = create_app()

    with app.app_context():
        existing = Charge.query.count()
        mode = 'skip'
        if existing > 0:
            print(f"\n⚠️  Datenbank enthält bereits {existing} Einträge.")
            print("   Modus wählen:")
            print("     [s] Skip (Duplikate überspringen, manuelle Einträge bleiben) — Standard")
            print("     [u] Update (fehlende Felder bestehender Einträge ergänzen)")
            print("     [a] Append (alles anhängen, auch Duplikate)")
            print("     [r] Replace (ALLES löschen + neu importieren — Backup wird erstellt)")
            resp = input("   Auswahl [s]: ").strip().lower()
            mode = {'s': 'skip', 'u': 'update', 'a': 'append',
                    'r': 'replace'}.get(resp, 'skip')
            if mode == 'replace':
                confirm = input("   BIST DU SICHER? Tippe 'LOESCHEN' um zu bestätigen: ").strip()
                if confirm != 'LOESCHEN':
                    print("   Abgebrochen.")
                    return

        with open(filepath, 'r', encoding='utf-8') as f:
            result = import_csv_data(io.StringIO(f.read()), mode=mode)

        print(f"\n✅ Import abgeschlossen (Modus: {result['mode']})")
        print(f"   Delimiter: '{result['delimiter']}', Header erkannt: {result.get('header_detected', False)}")
        print(f"   {result['imported']} neu importiert")
        if result['updated']:
            print(f"   {result['updated']} bestehende Einträge ergänzt")
        if result['skipped_dup']:
            print(f"   {result['skipped_dup']} Duplikate übersprungen (manuelle Daten geschützt)")
        if result['skipped']:
            print(f"   {result['skipped']} sonstige Zeilen übersprungen")
        if result.get('backup'):
            print(f"   Vorheriger DB-Stand gesichert: {result['backup']}")
        print(f"   Gesamt in DB: {result['total_db']} Einträge")
        print(f"   Summe kWh:    {result['total_kwh']:,.1f}")
        print(f"   Summe Kosten: €{result['total_cost']:,.2f}")

        if result['errors']:
            print(f"\n⚠️  {len(result['errors'])} Fehler:")
            for e in result['errors'][:10]:
                print(f"  {e}")
            if len(result['errors']) > 10:
                print(f"   ... und {len(result['errors']) - 10} weitere")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Verwendung: python import_gsheet.py <csv_datei>")
        print("")
        print("  Akzeptiert CSV-Exporte mit und ohne Header, Komma- oder")
        print("  Semikolon-getrennt, deutsche oder ISO-Datumsformate.")
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"❌ Datei nicht gefunden: {filepath}")
        sys.exit(1)

    import_from_csv(filepath)
