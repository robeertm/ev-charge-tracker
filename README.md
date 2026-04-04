# EV Charge Tracker

Local web application for tracking electric vehicle charging data for a **Kia Niro EV 64kWh (MY21)**.

## Features

- **Mobile-friendly input form** — quickly log charges from your phone
- **Dashboard** with KPI cards and Chart.js visualizations (monthly costs, cumulative, AC/DC split, CO2 charts)
- **Vehicle configuration** — battery capacity, max AC power, recuperation rate, CO2 production
- **THG quota tracking** — yearly payouts for saved CO2 emissions, deducted from total costs
- **Odometer tracking** — log km per charge, inline editing in history, consumption & cost per 100km
- **CO2 break-even chart** — cumulative savings vs. battery production CO2 (well-to-wheel)
- **Recuperation stats** — total energy recovered, extra km, recuperation charge cycles
- **ENTSO-E integration** — fetch hourly CO2 grid intensity for Germany
- **History** with filtering, inline km editing, CSV export
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
4. Optionally select the charging hour for hour-specific CO2 data

## Vehicle Settings

Configure in Settings → Fahrzeug:

| Setting | Default | Description |
|---------|---------|-------------|
| Akkukapazität | 64 kWh | Battery size for cycle & loss calculation |
| Max. AC-Ladeleistung | — | Max AC charging power |
| CO2 Akkuproduktion | 100 kg/kWh | For break-even calculation (MY2021) |
| Verbrenner CO2 WTW | 164 g/km | Well-to-wheel comparison (DE average) |
| Rekuperation | 0.086 kWh/km | Energy recovered per km |

## Tech Stack

- Python 3.10+, Flask, SQLAlchemy, SQLite
- Bootstrap 5, Chart.js
- ENTSO-E Transparency Platform API

## License

Robert Manuwald 2021-2026
