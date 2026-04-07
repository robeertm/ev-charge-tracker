# EV Charge Tracker

Local web application for tracking electric vehicle charging data. Works with any EV — configure your vehicle in settings.

## Features

- **Mobile-friendly input form** — quickly log charges from your phone
- **Dashboard** with KPI cards and Chart.js visualizations (monthly costs, cumulative, AC/DC/PV split, CO2 charts)
- **PV charging support** — third charge type with auto-calculated CO2 from PV system specs
- **Vehicle configuration** — battery capacity, max AC power, recuperation rate, CO2 production
- **THG quota tracking** — yearly payouts for saved CO2 emissions, deducted from total costs
- **Odometer tracking** — log km per charge, inline editing in history, consumption & cost per 100km
- **CO2 break-even chart** — cumulative savings vs. battery production CO2 (well-to-wheel)
- **Recuperation stats** — total energy recovered, extra km, recuperation charge cycles
- **ENTSO-E integration** — fetch hourly CO2 grid intensity for Germany
- **Auto CO2 backfill** — automatically fetches missing CO2 data from ENTSO-E (rate-limit aware)
- **CSV import via web UI** — upload Google Sheet CSV directly in settings
- **History** with filtering, inline km editing, CSV export
- **Vehicle API** — connect your Kia, Hyundai, VW, Skoda, Seat, Cupra, or Audi to auto-fetch SoC & odometer
- **Auto-updater** via GitHub releases
- **SQLite database** — all data stays local on your machine

## Quick Start

```bash
# Clone
git clone https://github.com/robeertm/ev-charge-tracker.git
cd ev-charge-tracker

# Quick start (recommended)
# macOS:   double-click start.command
# Linux:   ./start.sh
# Windows: double-click start.bat

# Or manually:
pip install -r requirements.txt
python app.py
```

Open `http://localhost:7654` in your browser.
From your phone (same network): `http://<your-pc-ip>:7654`

## Import from Google Sheet

**Via Web UI (recommended):**
1. Open your Google Sheet → File → Download → CSV
2. In the app: Settings → Datenbank → CSV Import → Upload

**Via CLI:**
```bash
python import_gsheet.py downloaded_file.csv
```

The importer handles German number format (comma as decimal separator) and various date formats. Missing CO2 values are automatically fetched from ENTSO-E in the background after import.

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

### PV System (Settings → PV-Anlage)

| Setting | Default | Description |
|---------|---------|-------------|
| Anlagengröße | — | kWp of your PV system |
| Jahresertrag | 950 kWh/kWp | Annual yield per kWp (DE average) |
| Lebensdauer | 25 years | Expected system lifetime |
| Herstellungs-CO₂ | 1000 kg/kWp | Production CO2 incl. transport & installation |
| PV-Strompreis | €0.00/kWh | Self-consumption cost |

### Vehicle API (Settings → Fahrzeug-API)

Connect your car to automatically fetch SoC, odometer, and charging status. Install the package for your brand:

| Brand | Install Command |
|-------|----------------|
| Kia | `pip install hyundai-kia-connect-api` |
| Hyundai | `pip install hyundai-kia-connect-api` |
| VW | `pip install carconnectivity carconnectivity-connector-volkswagen` |
| Skoda | `pip install carconnectivity carconnectivity-connector-skoda` |
| Seat / Cupra | `pip install carconnectivity carconnectivity-connector-seatcupra` |

After installing, configure credentials in Settings → Fahrzeug-API. Optional background sync polls your vehicle at a configurable interval (1–12h).

## Tech Stack

- Python 3.10+, Flask, SQLAlchemy, SQLite
- Bootstrap 5, Chart.js
- ENTSO-E Transparency Platform API
- Optional: hyundai-kia-connect-api, CarConnectivity (vehicle APIs)

## License

Robert Manuwald 2021-2026
