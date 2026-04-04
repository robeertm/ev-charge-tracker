"""Import charging data from Google Sheet CSV export.

Handles the raw Google Sheets CSV format (File → Download → CSV):
- Comma-separated with quoted German decimal numbers ("1,50")
- Title and summary rows at the top (auto-skipped)
- Extra summary columns to the right (ignored)
"""
import csv
import sys
import os
import subprocess
import re
from datetime import datetime

# ── Auto-setup and activate venv ─────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(SCRIPT_DIR, 'venv')
REQUIREMENTS = os.path.join(SCRIPT_DIR, 'requirements.txt')

if sys.platform == 'win32':
    VENV_PYTHON = os.path.join(VENV_DIR, 'Scripts', 'python.exe')
else:
    VENV_PYTHON = os.path.join(VENV_DIR, 'bin', 'python3')


def _ensure_venv():
    """Create venv and install deps if needed."""
    if not os.path.exists(VENV_PYTHON):
        print("📦 Erstelle virtuelle Umgebung...")
        subprocess.check_call([sys.executable, '-m', 'venv', VENV_DIR])
    print("📥 Installiere Abhängigkeiten...")
    subprocess.check_call([VENV_PYTHON, '-m', 'pip', 'install', '-q', '-r', REQUIREMENTS])


# Check if we're running inside the venv already
_in_venv = os.path.realpath(sys.executable) == os.path.realpath(VENV_PYTHON)

if not _in_venv:
    _ensure_venv()
    print("🔄 Starte mit virtueller Umgebung...")
    os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

# Add project root to path
sys.path.insert(0, SCRIPT_DIR)

from app import create_app
from models.database import db, Charge

# ── Date pattern to detect data rows ─────────────────────
DATE_PATTERN = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')


def parse_german_float(s):
    """Parse German number format: '1.234,56' → 1234.56, '0,29' → 0.29."""
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        # German format: dots as thousands sep, comma as decimal
        return float(s.replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def parse_int_safe(s):
    """Parse integer from string, handling German float format too."""
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        # Try direct int first
        return int(s)
    except ValueError:
        pass
    try:
        # Try via German float
        return int(round(float(s.replace('.', '').replace(',', '.'))))
    except (ValueError, TypeError):
        return None


def is_data_row(row):
    """Check if a CSV row is an actual charge data row (starts with a date)."""
    if not row or not row[0].strip():
        return False
    return bool(DATE_PATTERN.match(row[0].strip()))


def import_csv_data(file_obj, replace=False):
    """Import charges from CSV file object. Returns dict with results.
    Can be called from CLI or web interface."""
    imported = 0
    skipped = 0
    errors = []

    existing = Charge.query.count()
    if replace and existing > 0:
        Charge.query.delete()
        db.session.commit()

    reader = csv.reader(file_obj, delimiter=',')

    for row in reader:
        if not is_data_row(row):
            skipped += 1
            continue

        try:
            date_str = row[0].strip()
            charge_date = datetime.strptime(date_str, '%m/%d/%Y').date()

            charge = Charge(
                date=charge_date,
                eur_per_kwh=parse_german_float(row[1]) if len(row) > 1 else None,
                kwh_loaded=parse_german_float(row[2]) if len(row) > 2 else None,
                total_cost=parse_german_float(row[3]) if len(row) > 3 else None,
                charge_type=row[4].strip().upper() if len(row) > 4 and row[4].strip() else None,
                soc_from=parse_int_safe(row[5]) if len(row) > 5 else None,
                soc_to=parse_int_safe(row[6]) if len(row) > 6 else None,
                soc_charged=parse_int_safe(row[7]) if len(row) > 7 else None,
                loss_kwh=parse_german_float(row[8]) if len(row) > 8 else None,
                loss_pct=parse_german_float(row[9]) if len(row) > 9 else None,
                co2_g_per_kwh=parse_int_safe(row[10]) if len(row) > 10 else None,
                co2_kg=parse_german_float(row[11]) if len(row) > 11 else None,
            )

            if charge.soc_from is not None and charge.soc_to is not None and charge.soc_charged is None:
                charge.soc_charged = charge.soc_to - charge.soc_from

            db.session.add(charge)
            imported += 1

        except Exception as e:
            errors.append(f"Zeile {row[0]}: {e}")
            skipped += 1
            continue

    db.session.commit()

    total_kwh = db.session.query(db.func.sum(Charge.kwh_loaded)).scalar() or 0
    total_cost = db.session.query(db.func.sum(Charge.total_cost)).scalar() or 0
    total_db = Charge.query.count()

    return {
        'imported': imported,
        'skipped': skipped,
        'errors': errors,
        'total_db': total_db,
        'total_kwh': round(total_kwh, 1),
        'total_cost': round(total_cost, 2),
    }


def import_from_csv(filepath):
    """Import charges from Google Sheet CSV export (CLI version)."""
    app = create_app()

    with app.app_context():
        existing = Charge.query.count()
        if existing > 0:
            print(f"\n⚠️  Datenbank enthält bereits {existing} Einträge.")
            resp = input("   Löschen und neu importieren? (j/N): ").strip().lower()
            if resp not in ('j', 'y'):
                print("   Abgebrochen.")
                return
            replace = True
        else:
            replace = False

        import io
        with open(filepath, 'r', encoding='utf-8') as f:
            result = import_csv_data(io.StringIO(f.read()), replace=replace)

        print(f"\n✅ Import abgeschlossen!")
        print(f"   {result['imported']} Ladevorgänge importiert, {result['skipped']} Zeilen übersprungen")
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
        print("  Akzeptiert den direkten Google Sheets CSV-Export")
        print("  (Datei → Herunterladen → Kommagetrennte Werte)")
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"❌ Datei nicht gefunden: {filepath}")
        sys.exit(1)

    import_from_csv(filepath)
