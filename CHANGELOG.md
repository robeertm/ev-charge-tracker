# Changelog

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
