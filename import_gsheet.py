"""Import charging data from Google Sheet CSV export."""
import csv
import io
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from models.database import db, Charge


def parse_german_float(s):
    """Parse German number format (comma as decimal sep)."""
    if not s or s.strip() == '':
        return None
    try:
        return float(s.replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def parse_german_int(s):
    """Parse integer, handling German format."""
    if not s or s.strip() == '':
        return None
    try:
        return int(float(s.replace('.', '').replace(',', '.')))
    except (ValueError, TypeError):
        return None


def import_from_csv(filepath):
    """Import charges from a semicolon-separated CSV file (German format)."""
    app = create_app()

    with app.app_context():
        existing = Charge.query.count()
        if existing > 0:
            print(f"Database already has {existing} entries.")
            resp = input("Delete existing and re-import? (y/N): ").strip().lower()
            if resp != 'y':
                print("Aborted.")
                return
            Charge.query.delete()
            db.session.commit()
            print("Existing entries deleted.")

        imported = 0
        skipped = 0

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=';')
            header = next(reader)  # Skip header
            print(f"Header: {header}")

            for row in reader:
                if len(row) < 5 or not row[0].strip():
                    skipped += 1
                    continue

                try:
                    # Parse date (M/D/YYYY format from Google Sheets)
                    date_str = row[0].strip()
                    for fmt in ('%m/%d/%Y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y'):
                        try:
                            charge_date = datetime.strptime(date_str, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        print(f"  Skipping row, can't parse date: {date_str}")
                        skipped += 1
                        continue

                    charge = Charge(
                        date=charge_date,
                        eur_per_kwh=parse_german_float(row[1]) if len(row) > 1 else None,
                        kwh_loaded=parse_german_float(row[2]) if len(row) > 2 else None,
                        total_cost=parse_german_float(row[3]) if len(row) > 3 else None,
                        charge_type=row[4].strip().upper() if len(row) > 4 and row[4].strip() else None,
                        soc_from=parse_german_int(row[5]) if len(row) > 5 else None,
                        soc_to=parse_german_int(row[6]) if len(row) > 6 else None,
                        soc_charged=parse_german_int(row[7]) if len(row) > 7 else None,
                        loss_kwh=parse_german_float(row[8]) if len(row) > 8 else None,
                        loss_pct=parse_german_float(row[9]) if len(row) > 9 else None,
                        co2_g_per_kwh=parse_german_int(row[10]) if len(row) > 10 else None,
                        co2_kg=parse_german_float(row[11]) if len(row) > 11 else None,
                    )

                    # Calculate missing derived fields
                    if charge.soc_from is not None and charge.soc_to is not None and charge.soc_charged is None:
                        charge.soc_charged = charge.soc_to - charge.soc_from

                    db.session.add(charge)
                    imported += 1

                except Exception as e:
                    print(f"  Error on row: {row[:5]}... -> {e}")
                    skipped += 1
                    continue

            db.session.commit()

        print(f"\nImport complete: {imported} entries imported, {skipped} skipped.")
        print(f"Total entries in database: {Charge.query.count()}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python import_gsheet.py <csv_file>")
        print("  CSV must be semicolon-separated with German number format.")
        print("  Columns: Datum;EUR/kWh;kWh;Preis;Stromart;Von%;Bis%;Geladen%;Verlust_kWh;Verlust%;CO2_g;CO2_kg")
        sys.exit(1)

    import_from_csv(sys.argv[1])
