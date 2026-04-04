# EV Charge Tracker

Local web application for tracking electric vehicle charging data for a **Kia Niro EV 64kWh (MY21)**.

## Features

- **Mobile-friendly input form** — quickly log charges from your phone
- **Dashboard** with KPI cards and Chart.js visualizations (monthly costs, cumulative, AC/DC split)
- **ENTSO-E integration** — automatically fetch CO₂ grid intensity for Germany
- **History** with filtering, editing, CSV export
- **Auto-updater** via GitHub releases
- **SQLite database** — all data stays local on your machine

## Quick Start

```bash
# Clone
git clone https://github.com/robeertm/ev-charge-tracker.git
cd ev-charge-tracker

# Install dependencies
pip install -r requirements.txt

# (Optional) Import existing Google Sheet data
python import_gsheet.py path/to/exported.csv

# Run
python app.py
```

Open `http://localhost:7654` in your browser.
From your phone (same network): `http://<your-pc-ip>:7654`

## Import from Google Sheet

1. Open your Google Sheet
2. File → Download → Comma Separated Values (.csv)
3. Run: `python import_gsheet.py downloaded_file.csv`

The importer handles German number format (comma as decimal separator) and various date formats.

## ENTSO-E Setup

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu/)
2. Request an API token via email
3. Enter the token in Settings within the app

## Tech Stack

- Python 3.10+, Flask, SQLAlchemy, SQLite
- Bootstrap 5, Chart.js
- ENTSO-E Transparency Platform API

## License

© Robert Manuwald 2021-2026
