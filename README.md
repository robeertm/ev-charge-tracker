# EV Charge Tracker

> **Self-hosted dashboard for tracking your electric vehicle charges** — costs, kWh, CO2, recuperation, charging losses, live vehicle status, a self-generated **battery-health certificate**, and in-browser **OBD/ELM327** cell readout. Multi-vehicle / fleet support, connects to 15 EV brands via API. Available in 6 languages.

[![Latest Release](https://img.shields.io/github/v/release/robeertm/ev-charge-tracker)](https://github.com/robeertm/ev-charge-tracker/releases)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Self-Hosted](https://img.shields.io/badge/self--hosted-yes-green)

Built for EV owners who want **full control over their charging data** — runs locally on your laptop, NAS, or Raspberry Pi. No cloud, no tracking, no subscription. Your data stays on your machine.

> ### ⚠️ Please read before installing
>
> **This project is written with AI assistance.** Code, documentation and this
> README are largely AI-generated and reviewed by a human, not the other way
> round. Treat it as a hobby project, not as audited software.
>
> **It is provided "as is", with no warranty and no liability of any kind** —
> for errors, for wrong numbers, for lost or corrupted data, for damage to your
> vehicle or its battery, or for anything that follows from using it. **You use
> it at your own risk.**
>
> Three things deserve saying plainly, because they touch real hardware and real
> decisions:
>
> - **Remote control moves a real car.** The app can start and stop charging and
>   climate control and, for some brands, lock and unlock the vehicle. It is off
>   by default and must be enabled per vehicle. Enable it only if you accept
>   that consequence.
> - **The battery certificate is a self-assessment, not an appraisal.** It is
>   computed from this app's own recorded data and does not replace an
>   accredited battery test. Do not use it as the basis of a sale or a warranty
>   claim without an independent measurement.
> - **It talks to manufacturer APIs with your account credentials or API keys.**
>   Those interfaces are outside this project's control and may change, throttle
>   or refuse access at any time; some are not officially supported by the
>   manufacturer. Costs, CO2 figures and consumption values are estimates
>   derived from your own inputs and may be wrong.
>
> Not affiliated with, endorsed by, or supported by any vehicle manufacturer.
> Product and brand names belong to their respective owners.

---

## Screenshots

Every image below is one real screen of the running app, filled by the demo
seeder (`tools/seed_demo.py`) — two years of one fictional car, 395 charges,
41 720 km. No real installation and no real person's data appears here.

Desktop shots are 1920 × 1080, phone shots 390 × 844 (2×). Both are exactly
one screenful, so what you see is what you get when you open the page.

### Dashboard
![Dashboard](docs/screenshots/dashboard.png)
*Range estimate, highlights, cost and CO2 headline figures, consumption against
outside temperature, and the monthly cost chart across the whole history.*

### Dark mode
![Dark mode](docs/screenshots/dashboard-dark.png)
*One toggle, remembered across tabs and pages.*

### Live vehicle status
![Vehicle](docs/screenshots/vehicle.png)
![Vehicle, dark](docs/screenshots/vehicle-dark.png)
*Battery, range, odometer, 12 V, state of health — pulled from the car over the
vehicle API, cached or refreshed on demand.*

### Driving log
![Driving log](docs/screenshots/trips.png)
*Built from GPS pings: trips, distances, stops and the map of where the car has been.*

### New charge
![New charge](docs/screenshots/input.png)
*AC / DC / **PV** in one row. State of charge, odometer and grid CO2 are filled in
for you; start/stop asks the car directly.*

### History
![History](docs/screenshots/history.png)
*Filter by year and type, edit inline, export to CSV.*

### Report
![Report](docs/screenshots/report.png)
*Any period as a printable summary — cost, energy, CO2, price per kilometre.*

### Maintenance log
![Maintenance](docs/screenshots/maintenance.png)
*Inspections, tyres, parts — with cost and a reminder by date or by odometer.*

### OBD
![OBD](docs/screenshots/obd.png)
*Read the battery management system over a dongle: cell voltages, pack values,
state of health — and a battery certificate as a PDF.*

### Live log viewer
![Logs](docs/screenshots/logs.png)
*Auto-refreshing log window with level filter, search and CSV download.*

### Settings
![Settings](docs/screenshots/settings.png)
*Vehicle API, sync window, GHG quota, ENTSO-E, HTTPS, PV — all from the UI, no
config file to edit.*

### On a phone

The layout is not a shrunken desktop: cards stack, the table becomes a list,
and the charts keep their labels.

| Dashboard | Dark mode | New charge |
| --- | --- | --- |
| ![Dashboard on a phone](docs/screenshots/mobile/dashboard.png) | ![Dark mode on a phone](docs/screenshots/mobile/dashboard-dark.png) | ![New charge on a phone](docs/screenshots/mobile/input.png) |

| History | Driving log | Vehicle |
| --- | --- | --- |
| ![History on a phone](docs/screenshots/mobile/history.png) | ![Driving log on a phone](docs/screenshots/mobile/trips.png) | ![Vehicle on a phone](docs/screenshots/mobile/vehicle.png) |

| Report | Maintenance | Settings |
| --- | --- | --- |
| ![Report on a phone](docs/screenshots/mobile/report.png) | ![Maintenance on a phone](docs/screenshots/mobile/maintenance.png) | ![Settings on a phone](docs/screenshots/mobile/settings.png) |

| Vehicle, dark | OBD | Logs |
| --- | --- | --- |
| ![Vehicle dark on a phone](docs/screenshots/mobile/vehicle-dark.png) | ![OBD on a phone](docs/screenshots/mobile/obd.png) | ![Logs on a phone](docs/screenshots/mobile/logs.png) |

<details>
<summary>Try it with the same data</summary>

```bash
docker exec ev-charge-tracker python tools/seed_demo.py
```

Only works on an installation that has no charges yet — demo numbers must
never mix with a real history.
</details>

---

## Why this app?

| Problem | Solution |
|---|---|
| Apps from your carmaker only show last 30-90 days | **Lifetime tracking** in your own database |
| No privacy / data sold to third parties | **100% local** — SQLite file on your machine |
| Cant compare AC vs DC vs PV cost & CO2 | Built-in **AC/DC/PV split** with separate tariffs |
| GHG quota payouts not tracked | **THG quota** card deducts payouts from total cost |
| Manual logging is tedious | **Vehicle API** auto-fills SoC, odometer, charging status |
| ENTSO-E grid CO2 not integrated | **Hourly CO2 intensity** auto-fetched, missing values backfilled |

---

## Features

### Multi-vehicle / fleet (v3.0+)
- **Track any number of cars in one install** — every charge, sync, parking event, trip and maintenance record is anchored to one specific vehicle for life. Sold a car? Archive it; its lifetime kWh / km / cost stays in fleet aggregates
- **Navbar fleet picker** — flip between "whole fleet" view and a single vehicle from any page; dashboards, history, trips, maintenance, report and the new-charge form all respect the selection
- **Per-vehicle hardware** — battery kWh, SoH baseline, battery production CO₂, max-AC kW, recuperation rate, fossil-CO₂/km, color, icon
- **Per-vehicle API credentials** — every car has its own brand / user / password / pin / region / VIN; the daily API quota counter (200/day for Kia/Hyundai), smart-sync window, force-refresh queue and SDK trip-info reconcile are all per-vehicle so a 5-car fleet doesn't share a single quota
- **Per-vehicle THG-Quote** — German EV emissions certificate payouts are bound to the right car; the year-end reminder warns only when a non-archived vehicle is missing the previous year's payout
- **Setup wizard step 3** — first-install flow lets you register multiple vehicles in one shot before the app opens

### Tracking
- **Mobile-friendly input form** — quickly log charges from your phone, with Cancel button, native operator `<select>` (datalist-free so iOS Safari works), and optional GPS-captured station location
- **"My Location" uses the car's last GPS, not the phone** — pulls from the most recent `VehicleSync` row with coordinates so the station position reflects where the charge actually happened, even if you're back home when logging it. Phone GPS remains a secondary option
- **Operator price auto-fill** — configure per-operator `€/kWh` prices in Settings; picking an operator on the charge form auto-fills the price field (only while you haven't typed anything, so manual overrides are never lost)
- **Start/Stop charge tracking** — force-refresh from vehicle, auto-fill date/time/SoC/odometer, auto-stop when charge limit reached
- **Live vehicle status widget** on dashboard — SoC, range, odometer, doors, tires, climate, SoH, location
- **Vehicle history** — every sync persists SoC, range, odometer, 12V, SoH, recuperation, 30-day consumption, GPS. Stored only when a tracked value changes (compact, audit-friendly history)
- **Raw data viewer** (`/vehicle/raw`) — pretty-prints the full API dump for every sync, per brand, for debugging unusual SoH/range values
- **History** with filtering, inline km editing, CSV export
- **Full edit form** — every stored charge field (location, operator, coordinates, map picker) editable after the fact via `/edit/<id>`

### Driving log / Fahrtenbuch
- **Auto-detected parking events** — every vehicle sync hooks into a parking-event log; >100 m means "moved", new event opened, previous closed with arrival/departure odometer + SoC
- **Home / Work / Favorites** — pick locations on a Leaflet/OpenStreetMap card in Settings; events are auto-classified (home / work / favorite / other) within a 200 m radius. Favorites are inline-editable (rename, reposition via map, delete) with per-row action buttons
- **Trips page** at `/trips` — KPI cards (count, km, drive time, commute km), marker-cluster map, full table with from/to/km/duration/avg-speed/SoC
- **Full trip editor** — click the pencil on any trip row to open a two-column modal (Start / End) with every `ParkingEvent` field: label, favorite name, address, arrival/departure times, odometer and SoC at arrival/departure, coordinates. A shared Leaflet map below has draggable markers (blue = start, red = end) plus a "pick on map" button per side. Derived values (trip km, SoC used, recuperation) recompute automatically from the stored fields
- **7-day safety gate** on trip edits — entries older than 7 days require an explicit confirmation checkbox; server-enforced via 409 response, so hand-crafted requests can't bypass it
- **CSV + GPX export** — `/api/trips/export.csv` for the tax advisor, `/api/trips/export.gpx` for Google Earth / Komoot / OsmAnd
- **Smart sync mode** — runs cached by default but auto-upgrades to a force-refresh when GPS is older than 6 h and the car is not charging, so the Fahrtenbuch stays current without burning the daily API quota
- **Backfill** — replays existing vehicle syncs through the parking hook to retroactively rebuild the driving log

### Maintenance log / Wartungs-Logbuch
- **`/maintenance` page** — track inspections, tires, brakes, wipers, 12V battery, cabin filter, MOT/TUEV with date, odometer, cost and notes
- **Smart reminders** — entries can have a `next_due_km` and/or `next_due_date`; due-soon / overdue banner with sensible defaults per item type (e.g. inspection = 12 months / 30 000 km)

### Analytics
- **Dashboard** with KPI cards, Chart.js visualizations, and 7 vehicle-history mini time-series (SoC, range, odometer, 12V, SoH, recuperation, consumption)
- **Click-to-fullscreen on every plot** — each mini-chart is its own framed card with a fullscreen icon; click opens a Bootstrap `modal-fullscreen` with a larger version (thicker line, more axis ticks, grid, data-point circles). Time-range selector in the card header: 24h / 7d / 30d / 90d / 1y / all. Chosen range persists per-user in `AppConfig`
- **Last-known GPS** — the dashboard location card walks the vehicle-history series backwards to find the most recent GPS-bearing sync, so the map still renders under Kia/Hyundai cached mode where the latest sync typically has no coordinates
- **Range calculator** card — uses live SoC + battery capacity + 30-day consumption + outdoor temperature (Open-Meteo at home location), with a temperature penalty curve
- **Weather correlation** chart — bar (kWh/month) + line (avg outdoor degC) showing exactly why winter is more expensive
- **Highlights / fun facts** — cheapest/most expensive charge, biggest single charge, longest trip, fastest trip, longest park
- **PDF Report** — multi-page report with 10 charts, KPI overview, monthly/yearly/AC-DC-PV tables, vehicle-history time-series, Fahrtenbuch (last 80 trips with home<->work km for the German Pendlerpauschale), Wartungs-Logbuch, highlights page
- **CO2 break-even chart** — cumulative savings vs. battery production CO2 (well-to-wheel)
- **Recuperation stats** — total energy recovered, extra km, recuperation charge cycles
- **Cost & consumption per 100km** — net of GHG quota payouts
- **THG quota reminder** — banner Jan 1 - Mar 31 if no quota is logged for the previous year

### Battery health certificate & OBD readout
- **Self-generated battery-health certificate** — a signed-style PDF you can hand a buyer when you sell or archive a car, runnable on demand at any time. Instead of a lab discharge it reconstructs the same measurement from your logged charges: every session with a wide-enough SoC window is a partial capacity sample (`net_kWh / (SoC_window/100)` extrapolated to a full pack), and the **median over many sessions** cancels the noise. Only windows ≥ 25 % SoC count and per-session outliers (outside 40–115 % of nominal) are rejected
- **A–F grade + benchmark** — certified SoH (measured when ≥ 3 samples, otherwise the baseline-scaled BMS SoH), an age/mileage benchmark against the ~2.3 %/yr fleet mean, warranty-floor headroom (70 %), charging-stress factors (DC share, equivalent full cycles, deepest discharge, avg window) and an honest data-basis/limitations note. Sparse cars still get a certificate that marks SoH "not determinable"
- **SoH is always net-referenced** — State of Health is computed against the **usable net capacity**, never gross; net (`battery_kwh`) drives kWh/cost/CO₂/loss/SoH, while gross is carried informationally and printed on the certificate (buyers expect both numbers)
- **In-browser OBD/ELM327 read** (`/obd`) — connect a cheap ELM327 dongle straight from the browser via **Web Serial (USB)** or **Web Bluetooth (BLE)**, no app or terminal: plug in → button → values. Reads BMS SoH, per-cell voltages (min/max/avg + spread in mV with a traffic-light), pack voltage/current, temperatures and the 12 V battery. Raw frames are stored so a PID offset can be re-decoded later. (Chrome/Edge on desktop + Android; iOS can't do Web Serial/BLE)
- **Adapter picker** — a curated dropdown of compatible dongles (OBDLink SX/EX/CX, Vgate vLinker & iCar Pro, Veepeak OBDCheck, generic ELM327 USB/BLE). Picking one presets the USB baud rate, points you at the right connect button, and is remembered as your preferred adapter. Classic-Bluetooth / WiFi adapters are listed but flagged as unreachable from a browser
- **Robust Bluetooth discovery** — the BLE reader probes the common ELM327 service UUIDs and, if none match, enumerates every service the dongle exposes to find a usable one, so almost any BLE ELM327 works. It retries the first (often-rejected) connect, and once an adapter is granted it reconnects directly on later visits — no device chooser each time (a "choose a different device" link is there when you need it)
- **OBD feeds the certificate** — the latest OBD reading adds a "cell data" section (cell spread + temperatures, exactly what a charge-only certificate can't show) and its BMS SoH counts as a prioritised SoH source
- **Certificate inline on the OBD page** — the real PDF is embedded live under the OBD readout (not an HTML rebuild, so preview and export never drift), with a grade badge and an "export as PDF" button

### Integrations
- **15 vehicle brands** via API (see table below) — auto-fetch SoC, odometer, charging status
- **Brand feature matrix** in Settings — 10-item green/yellow/red grid per brand (SoC, GPS, 12V, SoH, recuperation, 30-day consumption, doors, climate, tires, live status). No more "wait, why isn't my car showing X" surprises.
- **ENTSO-E integration** — fetch hourly CO2 grid intensity for Germany, auto-backfill missing values
- **Open-Meteo** — daily mean temperatures for the range calculator and weather correlation, with DB cache (no key, no rate limits)
- **Nominatim reverse geocoding** — for street addresses on parking events and charge locations, with permanent DB cache and ToS-compliant rate limiter
- **CSV import with live preview** — upload your Google Sheet or exported CSV in Settings, get a dry-run preview showing detected delimiter, column mapping (per-column dropdown to correct misdetections), per-row action badges (`new` / `update` / `duplicate` / `empty` / `error`), and an error list with line numbers. Only when you hit "Import" does the data actually land in the DB. Automatic dedup by (date, hour, kWh) with 0.1 kWh tolerance
- **OBD/ELM327 via the browser** — Web Serial / Web Bluetooth transport with a data-driven PID table per vehicle family: `kia_hyundai_ext` (7E4, Kona/e-Niro/Soul), `xpeng`, `jaguar_ipace`, `mg_saic` (ZS EV/MG5) + `mg_mulan` (MG4), `byd_atto` (Atto 3/Dolphin, little-endian), `nissan_leaf` (LBC, KWP service 21), `renault_zoe_ph1` + `renault_zoe_ph2` (ZE50, 29-bit), `vw_meb` (ID.3/4/5, Enyaq, Born, Q4 — 29-bit), plus a generic `generic_ev` fallback on standard PID 015B. ISO-TP frame reassembly, service 0x21/0x22 + 11-bit/29-bit addressing, raw frames persisted for re-decoding. (Tesla omitted — no standard UDS battery PIDs; needs raw-CAN sniffing)
- **PV charging support** — third charge type with auto-calculated CO2 from PV system specs
- **Operator price directory** — Settings has an editable table of charging operators (19 built-ins + your customs) with per-operator `€/kWh`. Stored as JSON in `AppConfig` and consulted by the charge-entry form to auto-fill the price

### Security / HTTPS
- **Self-signed certificate** auto-generation via `cryptography` (or `openssl` CLI fallback). SAN entries cover `localhost`, `127.0.0.1`, and the LAN IP, so the same cert works on desktop AND smartphone
- **Three modes** in Settings: `off` (HTTP), `auto` (self-signed), `custom` (paths to your own Let's Encrypt cert)
- **Cert metadata viewer** + downloadable `.crt` to install on your phone via Profile (kills browser warnings permanently)
- HTTPS is required for the Geolocation API on smartphones — the auto mode gets you there in two clicks
- **Auto-hide** of the HTTPS card in Settings when the request comes from a Tailscale peer (100.64.0.0/10), since the VPN already provides transport encryption — less clutter for VM deployments

### Web-UI login (optional password gate)
- **Opt-in** front gate with username/password, shown before any route when enabled in Settings → Zugangsschutz
- **Werkzeug password hashing** (bcrypt-compatible), credentials in `AppConfig`
- **Flask signed session cookie** with a per-install random 32-byte secret that's generated on first boot and persisted — sessions survive restarts and updates
- **Use case**: when the Tailscale share link is known to other devices but you still want a password in front of your charge data

### First-run Setup Wizard (VM deployments)
- **Browser-based wizard** that appears on first access to a freshly provisioned VM (triggered by a `/srv/ev-data/.setup_pending` marker)
- **Web login + at least one vehicle** — the two mandatory steps; the web credentials replace the old SSH-password step and gate every subsequent request
- **LUKS passphrase change is optional** — shown only when the data volume actually sits on an encrypted `/dev/mapper/evdata` mapping (`sudo cryptsetup luksChangeKey` under the hood). New, unencrypted installs skip it entirely; existing encrypted installs keep it
- **Resume-safe**: progress is tracked in a state file so a mid-wizard reload doesn't reset the user
- **Result**: an end user can take ownership of a VM without touching a terminal — no SSH, no `cryptsetup`, no manpages

### Self-hosting / updates
- **In-app updater** — "Update available" button in Settings actually rolls out the new release on your machine (download zip, stage, detached helper swaps files, pip install, restart). No `git pull`, no terminal.
- **systemd-aware**: under systemd the file swap is done inline in the running process and the supervisor restarts the service; outside systemd the legacy detached-helper flow is used
- **Automatic rollback on a broken update** — before every swap, a backup of the files that would be overwritten is written to `updates/backup_pre_v<OLD>/` along with a `UPDATE_PENDING.json` marker. On each boot, a pre-flight state machine checks the marker: three failed boots in a row (or a port-7654 bind timeout within 60 seconds of launch) trigger an automatic restore of the previous version plus a `LAST_ROLLBACK.json` note for the UI. Works under any supervisor that restarts on crash. `data/`, `venv/`, `.git/`, `logs/`, `updates/` are never touched by the backup/restore.
- **Dashboard update banner** — a visible banner appears on the dashboard when a newer release is available; clicking jumps directly to `Settings → #updaterCard`. If the last update auto-rolled back, a second banner explains which versions were involved and offers a one-click dismiss (`DELETE /api/update/last-rollback`). Update-check response is cached in `sessionStorage` for 30 minutes so page-hopping doesn't hit the GitHub API on every view.
- **Restart button** in Settings for applying HTTPS changes or new certs
- **API rate limiter** — tracks daily API calls (Kia EU: 190/200 limit), counter on dashboard

### VM deployment (multi-user rollout)
- **Templated VM image** for DS1621+ / Synology VMM and other hypervisors: Debian base with Xvfb + x11vnc + noVNC for the Kia token capture flow, XRDP for occasional maintenance, UFW restricted to the `tailscale0` interface, systemd unit with `Restart=always` and `ConditionPathExists=/srv/ev-data/app/venv/bin/python` so it skips cleanly while the LUKS volume is locked
- **`ev-provision` script** on each clone — auto-detects the data disk, formats LUKS with a temporary passphrase, registers with Tailscale via a pre-auth key, sets up sudoers entries for the wizard, enables UFW, prints handover info
- **`ev-unlock` helper** — one command after VM boot, opens the LUKS volume, mounts it, starts the app
- **End-to-end flow**: admin clones the template → runs `ev-provision` once → shares the VM via Tailscale device sharing → user runs through the browser wizard → done

### UX
- **Dark/Light mode** — toggle in navbar, synced across all tabs via localStorage
- **6 languages** — German, English, French, Spanish, Italian, Dutch, all kept at **full parity** (every user-facing string translated in all six, not just de/en)

---

## Install with Docker (recommended on Linux)

One command. It installs Docker Compose's config, generates a private secret
key, pulls the ready-built image and starts the app — nothing is compiled on
your machine.

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/docker-install.sh | bash
```

Then open `http://localhost:7654` — or `http://<your-server-ip>:7654` from
another machine — and the setup wizard takes it from there.

Images are published for **amd64 and arm64**, so this works on a normal server
as well as on a Raspberry Pi or an ARM NAS.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
mkdir ev-charge-tracker && cd ev-charge-tracker
curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/docker-compose.yml -o docker-compose.yml
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > .env
docker compose up -d
```

`SECRET_KEY` signs the session cookie. Without your own value the app falls
back to a key that is published in this repository, which would let anyone
forge a session on an install that is reachable from outside.
</details>

**Everyday commands**

```bash
cd ~/ev-charge-tracker-docker
docker compose pull && docker compose up -d   # update to the latest version
docker compose logs -f                        # watch the log
docker compose down                           # stop (your data stays)
```

Charge history, settings and exports live in the named volume
`ev-tracker-data`. They survive updates, restarts and `docker compose down` —
only `docker compose down -v` deletes them.

**Options** — put them in `.env` next to the compose file:

| Variable | Default | What it does |
| --- | --- | --- |
| `EV_PORT` | `7654` | Port on the host |
| `TZ` | `Europe/Berlin` | Time zone used for timestamps |
| `ENTSOE_API_KEY` | empty | Enables the real CO2 intensity of your grid |
| `SECRET_KEY` | — | Required. Signs the session cookie |

> The app speaks plain HTTP. Put it behind a reverse proxy, a VPN or Tailscale
> before exposing it to the internet.

---

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

---

## Vehicle API — Supported Brands

Connect your car to automatically fetch SoC, odometer, and charging status.

**Docker: nothing to install.** Every connector below is baked into the image, so all
brands are ready the moment the container starts, and `docker compose pull` brings
newer connectors with the new image. (This is also why it has to be the image: a
connector installed from the running app would live in the container's writable
layer, which the next pull discards.)

**Native install:** `deploy/install.sh` installs the same list, one package at a time,
and skips anything the local Python cannot satisfy — the Kia/Hyundai SDK needs Python
3.12, so on Raspberry Pi OS bookworm (3.11) that one line is skipped and every other
brand still works. Anything skipped can be installed later from Settings, no terminal
needed.

| Brand | Package | Auth |
|-------|---------|------|
| **Kia** | `hyundai-kia-connect-api` | Username / Password ¹ |
| **Hyundai** | `hyundai-kia-connect-api` | Username / Password ¹ |
| **Volkswagen** | `carconnectivity` + connector | Username / Password |
| **Škoda** | none — official MyŠkoda Public API | API key (MyŠkoda app) + VIN ² |
| **Škoda (old access)** | `carconnectivity` + connector | Username / Password ³ |
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
| **XPENG** | *(none — built-in)* | Enode Client ID / Secret |

After installing, configure credentials in Settings > Vehicle API. Optional background sync polls your vehicle at a configurable interval (1-12h).

**¹ Kia/Hyundai:** signing in with username and password works — but only on
**Python 3.12 or newer**. The sign-in needs `hyundai-kia-connect-api` ≥ 4.26.5,
and every release of that SDK from 4.23.1 onward declares `Requires-Python
>=3.12`. The Docker image ships 3.12, so a container install signs in like
every other brand here.

A **native** install on an older interpreter cannot install that SDK at all —
Raspberry Pi OS bookworm still ships Python 3.11, where `pip install
'hyundai-kia-connect-api>=4.26.5'` simply finds no matching distribution.
There the app falls back to fetching a refresh token once through a browser
("Fetch Token" in Settings, valid about a year). It detects which of the two
cases it is in and offers only the one that can actually work, instead of
pointing at an upgrade that cannot happen.

**Škoda note:** Škoda is retiring the unofficial app API in October 2026. The app now
speaks the official [MyŠkoda Public API](https://public.api.connect.skoda-auto.cz/docs):
create an API key in the MyŠkoda app (under *API keys*), bound to the vehicles you pick,
and enter it with the VIN. Settings shows a one-form changeover for cars still on the old
access, and keeps everything already recorded. Two limits worth knowing: the official API
allows **20 requests per hour per vehicle** (the app budgets them and keeps a few back for
your own manual refresh), and it has **no trips or charging-history endpoints** — trips are
derived from parking events, as they are for other brands.

**Remote control:** for Škoda (official API) and Kia/Hyundai the app can start and stop
charging and climate control, set a charge limit, and — Kia/Hyundai only — lock and unlock.
It is **off by default and enabled per vehicle** in Settings; unlocking asks for a separate
confirmation. Commands are reported as *sent*, because these APIs queue the request and the
car acts afterwards.

**XPENG note:** XPENG has no public brand SDK, so it connects through the [Enode](https://enode.com) aggregator (the same route the community Home Assistant integrations use). Create an Enode app (Client ID + Secret), link your XPENG account once via Enode's hosted flow, then enter the keys under Settings > Vehicle API — brand *XPENG*, Client ID as username, Client Secret as password. Needs no extra package (rides on `requests`). Provides SoC, range, odometer, charging status and location.

---

## Import from Google Sheet

**Via Web UI (recommended):**
1. Open your Google Sheet > File > Download > CSV
2. In the app: Settings > Database > CSV Import > Upload

**Via CLI:**
```bash
python import_gsheet.py downloaded_file.csv
```

The importer handles German number format (comma as decimal separator) and various date formats. Missing CO2 values are automatically fetched from ENTSO-E in the background after import.

---

## ENTSO-E Setup

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu/)
2. Request an API token via email
3. Enter the token in Settings within the app
4. Optionally select the charging hour for hour-specific CO2 data

---

## Vehicle Settings

Configure in Settings > Vehicle:

| Setting | Default | Description |
|---------|---------|-------------|
| First registration (Erstzulassung) | -- | Calendar date the battery actually ages on — drives age + degradation benchmark on the certificate |
| Battery capacity (net) | 64 kWh | Usable net size — basis for cycle, loss, cost, CO2 and SoH |
| Battery capacity (gross) | -- | Optional gross/buffer size — informational, printed on the certificate |
| Max AC power | -- | Max AC charging power |
| Battery production CO2 | 100 kg/kWh | For break-even calculation (MY2021) |
| ICE CO2 WTW | 164 g/km | Well-to-wheel comparison (DE average) |
| Recuperation | 0.086 kWh/km | Energy recovered per km |

### PV System (Settings > PV)

| Setting | Default | Description |
|---------|---------|-------------|
| System size | -- | kWp of your PV system |
| Annual yield | 950 kWh/kWp | Annual yield per kWp (DE average) |
| Lifetime | 25 years | Expected system lifetime |
| Manufacturing CO2 | 1000 kg/kWp | Production CO2 incl. transport & installation |
| PV electricity price | 0.00 EUR/kWh | Self-consumption cost |

---

## Languages

Switchable from Settings > Language:

- Deutsch
- English
- Francais
- Espanol
- Italiano
- Nederlands

1257 translated strings per language, kept at full parity across all six (standing order — every user-facing string is translated in all languages, not just German/English). German is the source and remains the fallback if a key is ever missing. New languages can be added by dropping a `<lang>.json` file into `translations/`.

---

## Tech Stack

- **Backend:** Python 3.10+, Flask, SQLAlchemy, SQLite
- **Frontend:** Bootstrap 5.3 (with dark mode), Chart.js
- **PDF:** matplotlib + fpdf2
- **Data:** ENTSO-E Transparency Platform API
- **Vehicle APIs:** hyundai-kia-connect-api, teslapy, renault-api, pypolestar, saic-ismart-client-ng, pySmartHashtag, pyporscheconnectapi, carconnectivity; XPENG via the Enode aggregator (REST over `requests`, no extra package)

---

## Contributing

Pull requests welcome! Areas where help is appreciated:
- More vehicle API connectors
- Additional language translations (just add `translations/<lang>.json`)
- Charts and analytics ideas
- Mobile UX improvements

---

## License

Robert Manuwald 2021-2026 — released under the MIT License (see `LICENSE`).

### Disclaimer

This software is provided **"as is", without warranty of any kind**, express or
implied, including but not limited to the warranties of merchantability,
fitness for a particular purpose and non-infringement. In no event shall the
author be liable for any claim, damages or other liability arising from the
software or its use.

**Large parts of this project — code, tests and documentation — are
AI-generated.** They are reviewed before release, but reviewing is not the same
as auditing: mistakes are possible anywhere, including in the parts that read
from or send commands to a vehicle. Anyone relying on this software for
something that matters should verify it themselves.
