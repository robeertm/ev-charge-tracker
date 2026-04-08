# EV Charge Tracker

Local web application for tracking electric vehicle charging data. Works with any EV — configure your vehicle in settings.

## Features

- **Mobile-friendly input form** — quickly log charges from your phone
- **Start/Stop charge tracking** — force-refresh from vehicle, auto-fill date/time/SoC/odometer, auto-stop when charge limit reached
- **Dashboard** with KPI cards, Chart.js visualizations, and live vehicle status widget (SoC, range, odometer, doors, tires, climate, SoH, location)
- **Dark/Light mode** — toggle in navbar, synced across all tabs via localStorage
- **PDF Report** — multi-page report with 10 charts, KPI overview, and detailed tables (monthly, yearly, AC/DC/PV)
- **PV charging support** — third charge type with auto-calculated CO2 from PV system specs
- **Vehicle configuration** — battery capacity, max AC power, recuperation rate, CO2 production
- **THG quota tracking** — yearly payouts for saved CO2 emissions, deducted from total costs
- **Odometer tracking** — log km per charge, inline editing in history, consumption & cost per 100km
- **CO2 break-even chart** — cumulative savings vs. battery production CO2 (well-to-wheel)
- **Recuperation stats** — total energy recovered, extra km, recuperation charge cycles
- **ENTSO-E integration** — fetch hourly CO2 grid intensity for Germany, auto-backfill missing values
- **CSV import via web UI** — upload Google Sheet CSV directly in settings
- **History** with filtering, inline km editing, CSV export
- **Vehicle API** — connect your car to auto-fetch SoC, odometer, charging status (13 brands supported)
- **API rate limiter** — tracks daily API calls (Kia EU: 190/200 limit)
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
1. Open your Google Sheet > File > Download > CSV
2. In the app: Settings > Datenbank > CSV Import > Upload

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

## Vehicle API

Connect your car to automatically fetch SoC, odometer, and charging status. All packages installable directly from Settings UI (no terminal needed).

| Brand | Package | Auth |
|-------|---------|------|
| **Kia** | `hyundai-kia-connect-api` | Refresh-Token (OAuth via Selenium) |
| **Hyundai** | `hyundai-kia-connect-api` | Refresh-Token (OAuth via Selenium) |
| **Volkswagen** | `carconnectivity` + connector | Username / Password |
| **Skoda** | `carconnectivity` + connector | Username / Password |
| **Seat** | `carconnectivity` + connector | Username / Password |
| **Cupra** | `carconnectivity` + connector | Username / Password |
| **Audi** | `carconnectivity` + connector | Username / Password |
| **Tesla** | `teslapy` | OAuth Refresh-Token |
| **Renault** | `renault-api` | Username / Password |
| **Dacia** | `renault-api` | Username / Password |
| **Polestar** | `pypolestar` | Username / Password |
| **MG (SAIC)** | `saic-ismart-client-ng` | Username / Password |
| **Smart #1/#3** | `pySmartHashtag` | Username / Password |
| **Porsche** | `pyporscheconnectapi` | Username / Password |

After installing, configure credentials in Settings > Fahrzeug-API. Optional background sync polls your vehicle at a configurable interval (1-12h).

**Kia/Hyundai note:** Password login is blocked by reCAPTCHA. Use the "Token holen" button in settings — opens Chrome with mobile user-agent for the OAuth flow. Token is valid for ~1 year.

## Vehicle Settings

Configure in Settings > Fahrzeug:

| Setting | Default | Description |
|---------|---------|-------------|
| Akkukapazitat | 64 kWh | Battery size for cycle & loss calculation |
| Max. AC-Ladeleistung | -- | Max AC charging power |
| CO2 Akkuproduktion | 100 kg/kWh | For break-even calculation (MY2021) |
| Verbrenner CO2 WTW | 164 g/km | Well-to-wheel comparison (DE average) |
| Rekuperation | 0.086 kWh/km | Energy recovered per km |

### PV System (Settings > PV-Anlage)

| Setting | Default | Description |
|---------|---------|-------------|
| Anlagengroesse | -- | kWp of your PV system |
| Jahresertrag | 950 kWh/kWp | Annual yield per kWp (DE average) |
| Lebensdauer | 25 years | Expected system lifetime |
| Herstellungs-CO2 | 1000 kg/kWp | Production CO2 incl. transport & installation |
| PV-Strompreis | 0.00 EUR/kWh | Self-consumption cost |

## Tech Stack

- Python 3.10+, Flask, SQLAlchemy, SQLite
- Bootstrap 5.3 (with dark mode), Chart.js
- matplotlib + fpdf2 (PDF reports)
- ENTSO-E Transparency Platform API
- Optional: vehicle API connectors (see table above)

## License

Robert Manuwald 2021-2026
