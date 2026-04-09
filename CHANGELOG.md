# Changelog

## v2.1.1 (2026-04-09)

### Fixes
- **Updater** — version comparison now uses semver tuples instead of plain string inequality. A user on a later dev version no longer sees an "update available" pointing at an older release, and `2.10.0` correctly sorts above `2.9.0`.

## v2.1.0 (2026-04-09)

### New Features
- **Vehicle history tracking** — every vehicle sync now persists battery (SoC), range, odometer, 12V battery, calculated SoH, total recuperated kWh, 30-day kWh/100km consumption, and GPS location. New rows are only stored when at least one tracked value has changed (compact, audit-friendly history).
- **Dashboard vehicle history widget** — 7 compact time-series mini-charts (SoC, range, odometer, 12V, SoH, recuperation, consumption) showing the evolution of all tracked metrics.
- **Vehicle location map** — small Leaflet/OpenStreetMap card on the dashboard showing where the car was last seen, with marker and zoom.
- **PDF report extended** — new "Fahrzeug-Historie" section with all 7 time-series charts, summary KPIs (km driven, SoH delta, recuperation delta) and the last known GPS position.

### Database
- New columns on `vehicle_syncs`: `battery_12v_percent`, `battery_soh_percent`, `total_regenerated_kwh`, `consumption_30d_kwh_per_100km`, `location_lat`, `location_lon` (auto-migrated on startup).

## v2.0.0 (2026-04-09)

### New Features
- **Multi-language support** — Deutsch, English, Français, Español, Italiano, Nederlands. Switchable in Settings → Sprache. 286 strings per locale, JSON-based fallback to German.
- **Marketing-ready README** — badges, screenshots section, problem/solution table, "why this app" pitch, GitHub topics for discoverability (electric-vehicle, ev-charging, kia, hyundai, tesla, …).

### Improvements
- Lightweight i18n service (`services/i18n.py`) with `t()` global, per-request language selection, format-string support.

## v1.9.0 (2026-04-09)

### New Features
- **6 additional vehicle brands** via API connectors:
  - **Tesla** (`teslapy`, OAuth refresh-token, miles → km auto-convert)
  - **Renault** & **Dacia** (`renault-api`, async)
  - **Polestar** (`pypolestar`, async)
  - **MG / SAIC** (`saic-ismart-client-ng`)
  - **Smart #1/#3** (`pySmartHashtag`)
  - **Porsche** (`pyporscheconnectapi`)
- Modular connector architecture preserved — Kia/Hyundai integration untouched, no token loss.
- All packages installable from Settings → Vehicle API UI (no terminal needed).

### Improvements
- **Dark / Light mode** toggle in navbar, inline boot script avoids flash, synced across browser tabs via `localStorage` storage event.
- **Local timestamps** — `datetime.utcnow` replaced with `datetime.now` everywhere; "Letzte Sync" no longer shows UTC.
- **Repo cleanup** — `.DS_Store`, `.claude/`, `*.command` added to `.gitignore` and untracked.
- **Dynamic copyright year** — footer no longer hardcoded to 2025.

## v1.8.4 (2026-04-08)

### Fixes
- Reverted experimental client-side OAuth wizard — Selenium-based token fetch (v1.5.4) is back as the only reliable approach for headed environments.

## v1.8.3 (2026-04-08)

### Fixes
- **SoH fallback** — when the EU API does not populate `BatteryManagement.SoH.Ratio` (most non-Kona vehicles), SoH is computed from `total_consumed_kwh / battery_kwh` and shown in the dashboard widget.

## v1.8.2 (2026-04-08)

### Fixes
- **Kia API unit conversion** — `totalPwrCsp` and `regenPwr` empirically use 0.1 kWh units (not Wh as the upstream library docs claim). Recuperation now matches dashboard expectations (~7.072 kWh, not 21.011).

## v1.8.1 (2026-04-08)

### Fixes
- **PDF "Gesamtübersicht" layout** — replaced overlapping manual y-positioning with a clean bordered KPI table.
- **Dashboard auto-refresh** — vehicle widget now actually fetches fresh cached data on page load (was only restoring from localStorage cache).
- **SoH on dashboard** — added new "SoH %" tile to the live vehicle widget.

## v1.8.0 (2026-04-08)

### New Features
- **PDF Report** — new "Report" button in navigation, generates multi-page PDF with:
  - KPI overview (costs, kWh, CO2, savings, consumption, recuperation)
  - 10 colorful charts (monthly costs/kWh/CO2 with averages, cumulative cost/kWh, CO2 break-even, price trend, charge count, AC/DC/PV pie charts, yearly comparison)
  - Detailed tables (AC/DC/PV statistics, yearly overview, monthly breakdown)
  - Auto-generated filename with car model and date

## v1.7.0 (2026-04-08)

### New Features
- **Start/Stop charge tracking** — buttons on input page trigger force-refresh from vehicle, auto-fill date/time/SoC/odometer
- **Live charge timer** — shows elapsed time, estimates kWh from time × AC power
- **Auto-stop** — polls every 10 min during charging, auto-stops when SoC reaches charge limit or car stops charging
- **CO2 from time range** — calculates weighted average CO2 from ENTSO-E for the charge period (start to end hour)
- **API rate limiter** — tracks daily Kia API calls (190/200 limit), counter shown on dashboard, auto-reset at midnight
- **Session persistence** — charge session survives tab switches and page reloads via localStorage

### Improvements
- Charge poll interval: 10 min (was 5 min) to respect Kia EU 200 calls/day limit
- Auto-sync minimum interval: 1 hour (was 30 min)
- Sync service respects daily API limit
- Settings: vehicleCredentials and syncSection render server-side when brand configured

## v1.6.0 (2026-04-08)
- **Cached vs Live refresh** — two buttons on dashboard: "Cached" reads server cache, "Live" wakes the car for fresh data
- **Force refresh fallback** — if Live returns null values (odometer, range, 12V), last known values are preserved
- **Settings sync modes** — "Sync (Cached)" and "Sync (Live)" buttons, auto-sync mode selector (Cached/Live)
- **Input force refresh** — vehicle fetch button in "Neue Ladung" always wakes the car
- **localStorage cache** — vehicle data persists across tab switches, no re-fetch needed
- **Hyundai token support** — token fetch now works for both Kia and Hyundai with brand-specific OAuth URLs

## v1.5.5 (2026-04-07)
- **Full vehicle live dashboard** — all available data from Kia/Hyundai displayed in 3-row widget
- **New data points** — doors/trunk/hood status, tire pressure warnings, 30-day consumption, Schuko charge time, registration date, Google Maps location link
- **Extended API** — `/api/vehicle/status` returns all vehicle data

## v1.5.4 (2026-04-07)
- **One-click Kia/Hyundai token fetch** — opens Chrome with mobile user-agent, user logs in + solves reCAPTCHA, token is auto-captured and saved
- **Working OAuth flow** — uses `peukiaidm-online-sales` client for initial login, then exchanges for CCSP refresh token
- **Clean settings UI** — brand selection, install buttons, delete/reset, manual token entry as fallback

## v1.5.1 (2026-04-07)
- **One-click package install** — install vehicle API packages directly from settings UI (no terminal needed)

## v1.5.0 (2026-04-07)

### New Features
- **Vehicle API integration** — connect your car to auto-fetch SoC, odometer, charging status
- **Supported brands** — Kia (UVO), Hyundai (Bluelink), VW (WeConnect), Skoda (MySkoda), Seat (MyCar), Cupra (MyCupra), Audi (myAudi)
- **Auto-fill on input** — "Von Fahrzeug abrufen" button fills SoC and odometer from vehicle API
- **Background sync service** — periodic vehicle status polling (configurable 1h–12h interval)
- **Vehicle sync history** — all synced data points stored in database
- **Settings UI** — Fahrzeug-API card with brand selection, credentials, connection test, manual sync, auto-sync toggle
- **Modular connector architecture** — plugin-based design, new brands can be added easily
- **Optional dependencies** — vehicle API packages only needed when used (graceful degradation)

## v1.4.4 (2026-04-04)
- **Average lines in all monthly charts** — dashed Ø lines for costs, kWh, and CO2

## v1.4.3 (2026-04-04)
- **Average line in monthly cost chart** — dashed line showing Ø cost per month

## v1.4.0 (2026-04-04)
- **Auto CO2 backfill** — missing CO2 values are automatically fetched from ENTSO-E after CSV import
- **Manual backfill button** — "CO₂ nachladen" in ENTSO-E settings with live progress
- **Background processing** — rate-limit aware with automatic retries

## v1.3.1 (2026-04-04)
- Fix uniform chart heights across all dashboard rows

## v1.3.0 (2026-04-04)

### New Features
- **PV charging** — third charge type "PV (Solar)" alongside AC/DC
- **PV system configuration** — kWp, annual yield, lifetime, production CO2 in settings
- **Auto-calculated PV CO2** — from system specs (e.g. 10kWp → ~42 g/kWh)
- **PV auto-fill** — selecting PV pre-fills CO2 and price fields
- **AC/DC/PV comparison** — dashboard table includes PV column when data exists
- **PV filter** — history filterable by PV charge type
- **Mobile-friendly charts** — responsive sizing, fewer ticks, smaller fonts, shorter legends on small screens

## v1.2.1 (2026-04-04)
- **CSV import via web UI** — upload Google Sheet CSV directly in settings (no CLI needed)
- Refactored import logic into reusable `import_csv_data()` function

## v1.2.0 (2026-04-04)

### New Features
- **Vehicle configuration** — car model, battery capacity, max AC power editable in settings
- **THG quota management** — add/delete yearly CO2 bonus payouts, deducted from total costs
- **Odometer tracking** — km field per charge, inline editing in history view
- **Charging hour** — select hour (00-23) for hour-specific ENTSO-E CO2 data
- **Recuperation tracking** — configurable kWh/km rate, total energy recovered, extra km, recuperation cycles
- **CO2 break-even chart** — cumulative CO2 savings vs. battery production with break-even line
- **Well-to-wheel CO2** — configurable fossil car WTW emissions (default 164 g/km DE average)
- **Auto-calculated charging losses** — from SoC difference and battery capacity when not manually entered
- **New dashboard KPIs** — net costs (after THG), consumption kWh/100km, cost per 100km, charge cycles, recuperation stats
- **CO2 charts** — monthly CO2 emissions bar chart, cumulative CO2 savings line chart
- **Improved dashboard layout** — AC/DC and yearly tables separated, full-width cost chart

### Fixes
- Fix ENTSO-E connection test button (hidden input override)
- Fix GitHub username in settings template and update checker
- Auto-migrate database schema (adds columns without data loss)

## v1.1.0 (2026-04-04)
- Vehicle configuration in settings
- THG quota tracking

## v1.0.2 (2026-04-04)
- Fix GitHub username in update checker and settings link

## v1.0.1 (2026-04-04)
- Fix ENTSO-E connection test button

## v1.0.0 (2026-04-04)
- Initial release
