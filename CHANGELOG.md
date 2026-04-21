# Changelog

## v2.28.27 (2026-04-21)

### PE same-place updates: trust only freshly-timestamped GPS

Observed on ev-robert (Kia): the first sync after the overnight smart-window re-opens sometimes carries `gps_ts=None` with the **origin's GPS** (home coords) while the car has already started the morning commute. Because the coords match the open Home PE, the same-place path happily overwrote `soc_departed` with the mid-drive SoC reading (50 % → 45 %, where 50 % was the true pre-drive value and 45 % the in-progress drive value). The Fahrtenbuch then showed the trip as "left Home at 45 %", which is just the Work-arrival SoC bleeding backward.

The odometer is unchanged across these echo syncs (origin's cached odo), so they can't be distinguished by coord alone — but the missing `gps_ts` is the tell. v2.28.27 gates the same-place update on `location_last_updated_at` being present AND within the 30-min staleness window. If not: the function returns the open PE unchanged, skipping the `soc_departed` / `odometer_departed` / `last_seen_at` writes. Missing-ts syncs are still written to the VehicleSync table (so regen / SoC lookups can find them), just not allowed to rewrite PE state.

## v2.28.26 (2026-04-21)

### PE state machine: reject GPS moves when odometer hasn't advanced (Hyundai cache-echo fix)

Observed on ev-dirk (Hyundai Bluelink): cloud occasionally serves a stale GPS reading with a deceptively fresh timestamp (4 min old, below the 30-min staleness gate) that points at an earlier location (e.g. the morning's Work coord after the car has long since been Home). The PE state machine would interpret this as a real move — closing the current Home PE and opening a phantom Work PE — even though the car never physically moved. Once reconcile ran, the phantom PE paired with the next morning's genuine Home sync to render as a bizarre multi-hour `work → home` "trip".

Ground truth is the odometer. Every legitimate move must advance it. `update_parking_from_sync` now requires `sync.odometer_km` to differ from the open PE's last-known odometer (`odometer_departed or odometer_arrived`) by ≥ 1 km before accepting the GPS-based move; otherwise it ignores the sync and keeps the current PE intact. No impact on Kia — its cache echoes haven't shown this pattern; the guard is just a belt-and-suspenders check.

## v2.28.25 (2026-04-21)

### Widen reconcile arrival-tolerance from 20 min to 4 h (fixes Hyundai 14-hour phantom trips)

When Hyundai Bluelink stops returning GPS for a sleeping car, our PE state machine correctly ignores those syncs — but the car then gets detected at B only on the next GPS-bearing poll, which can be many hours after the physical arrival. Result: a PE pair whose `arrived_at` was 2 h+ later than the SDK-reported trip end. The old 20-min reconcile tolerance couldn't match that, so the trip double-rendered as:

- A polled PE pair with grossly wrong timestamps (e.g. `17:03 yesterday → 10:39 today`, 14 h for 9 km).
- A duplicate SDK-only row with correct times but no location.

Tolerance is now 240 min. The physical conflict check (`sdk.start_time ∈ (prev.arrived_at, curr.arrived_at]`) plus the km-matching guard keep cross-pairing from happening; the greedy allocator scores by closest time-delta so the right SDK trip still wins when multiple fall in the window.

## v2.28.24 (2026-04-21)

### Skoda (and all VAG brands): fix `'Drives' object is not subscriptable`

`carconnectivity >= 0.11` changed `vehicle.drives` from a subscriptable list to a `Drives` container object whose payload lives in `.drives: Dict[str, GenericDrive]`. Our connector still did `vehicle.drives[0]`, which raised `'Drives' object is not subscriptable` and turned every Skoda/VW/Seat/Cupra/Audi status call into a settings-page error banner.

`connector_vag.get_status()` now pulls `vehicle.drives.drives`, prefers the drive whose `type.value` ends in `ELECTRIC` (matters for hybrids), and falls back to the first drive otherwise.

### Silence CarConnectivity MQTT push-channel noise

The Skoda/VW/Seat/Cupra/Audi MQTT connectors log `Could not connect (Not authorized)` at ERROR level every ~2 s when the MQTT broker rejects the push token. We poll via HTTPS and don't rely on MQTT push, so the failure doesn't affect data — but the spam flooded the `/live-logs` page. All four brand-MQTT loggers are now pinned to CRITICAL in `app.py`.

## v2.28.23 (2026-04-21)

### Two Fahrtenbuch-correctness fixes (Kia + Hyundai)

**1) Regen correctly anchors on SDK-reconciled departure.**
The v2.28.22 fix handled the arrival side but regen still read zero whenever the first post-drive sync hit `prev.last_seen_at` with fresh cumulative counters and stale GPS — both brands ship those transitional syncs where position says "still at home" but odo/regen already reflect the finished drive. Departure lookup now prefers `prev.departed_at` (which the reconciler snaps to the real SDK drive-start) and always uses `strict=True`, so the regen reading is always a sync strictly BEFORE the drive — never the ambiguous transitional one.

**2) Trip reconcile no longer waits for 03:00.**
New `request_post_move_reconcile()` flag in sync_service. The PE state machine sets it the moment a new parking event is opened (car arrived somewhere new). The sync loop picks the flag up within ~10 s, runs a one-shot `backfill(days=1)` (which internally reconciles the day), and clears it. Before this patch the just-ended trip would show its pre-reconcile state — often a 19-second "trip" at 07:05:19 → 07:05:38 if the force-refresh burst pulled departure and arrival in the same window — until the 03:00 nightly task realigned it. Now departed_at/arrived_at snap to the SDK timestamps minutes after the drive, not hours.

## v2.28.22 (2026-04-21)

### Fix: regen values collapsed to 0 after v2.28.20 arrived_at snap

v2.28.20 moved `curr.arrived_at` EARLIER (to the SDK-derived physical arrival moment, typically 3–7 min before the first at-destination sync). `trips_service._cum_regen_at(lookup, arrived_at)` still used `bisect_right - 1`, which now landed on the last PRE-drive sync — the car was still at origin, so `cum_regen` at that point equaled `cum_regen` at departure, and the diff was zero. Every reconciled trip's regen dropped to `0.00 kWh` overnight.

Fix: new helper `_cum_regen_at_or_after(lookup, ts)` picks the first sync at-or-after `arrived_at` (i.e. the first at-destination regen reading). Departure side unchanged — `prev.last_seen_at` is still a real poll timestamp, not touched by reconcile. Affects Kia UVO and Hyundai Bluelink trips (anywhere reconcile runs).

## v2.28.21 (2026-04-20)

### Log daily reconcile: include arr_applied counter

v2.28.20 added arrival-timestamp correction but the daily-loop log line still only reported the `departed_at` counter. Now emits `dep=N arr=M conflicts=K` so the new counter is visible in journal logs.

## v2.28.20 (2026-04-20)

### Reconcile arrival timestamps from SDK trip data (Kia + Hyundai)

Until now, `ParkingEvent.arrived_at` was the timestamp of the first at-destination poll — up to ~10 minutes after the car actually stopped (sync cadence in smart mode). Only `departed_at` was being snapped to the SDK start-time; the PE arrival kept the poll-lag error, so Fahrtenbuch drive durations looked a few minutes too long.

SDK `day_trip_info` has minute-accurate `start_time + drive_minutes + idle_minutes` per trip, which gives the real end-of-drive timestamp. `services/trip_reconcile.py` now additionally sets:

```
curr.arrived_at = sdk.start_time + drive + idle
```

Guarded by `new_arr < old_arr`: first-parked-sync is by definition at-or-after the physical arrival, so we only ever shift EARLIER — never later (that would be spurious clock noise). Same logic and tolerance window (`±20 min`) as the existing `departed_at` correction; runs for both Kia UVO and Hyundai Bluelink since both brands ship the endpoint.

Summary dict gains `arr_applied` / `total_arr_applied`; log line now reports both counters (`N departed_at + M arrived_at corrected`).

## v2.28.19 (2026-04-20)

### Fahrtenbuch table: drop redundant edit-button column (iPhone 17 Air fits without scrollbar again)

User report with screenshot: on iPhone 17 Air (~400 px viewport) the Fahrtenbuch table overflowed the screen — the right-most edit-button column got cut off and `table-responsive` showed a horizontal scrollbar.

Fix: removed the edit-button column entirely. The button was redundant — Von/Nach cells already had `role="button"` plus an identical `openTripEditor(fid, tid)` binding, i.e. they triggered the exact same modal. 6 columns instead of 7 → table fits without horizontal scroll.

## v2.28.18 (2026-04-20)

### Background maintenance for long-form geocode cache entries

v2.28.17 introduced the short-address format, but the bulk post-deploy migration (44 PE on the Kia install, 16 on the Hyundai install) triggered a Nominatim 429 rate-limit block — three hosts in parallel bursts hammered past the 1 req/s policy. Individual hosts retrying afterwards inherited the same block for hours.

v2.28.18 shifts the migration into a background thread so it can never spike:

- New thread `geocode-maintenance` starts inside `create_app()` alongside the vehicle-sync thread.
- **Idle**: no legacy entries (`raw_json IS NULL`) in the cache → 10 min sleep, next probe.
- **Pending**: rebuild *one* row, 2 s sleep, next row. Fixed-slow — Nominatim can't be overloaded.
- **Nominatim error** (e.g. the initial 429 block): 60 s backoff, retry. Self-heals when the block lifts.
- After each successful rebuild, cascades the new short address onto every `ParkingEvent.address` whose rounded coords match — no manual `geocode_missing_events` pass needed.

The remaining ~10 legacy entries per host trickle through in 20–30 min without further user action. Future-proof: any transient Nominatim failure (timeout → `raw_json IS NULL` stays) gets retried on the next wake-up.

## v2.28.17 (2026-04-20)

### Short, readable addresses in Fahrtenbuch (POI / street / postcode / city)

Until now the geocoding cache stored Nominatim's verbose `display_name` (`"1, Street, Suburb, City, State, Postcode, Country"`). On /trips that column ate most of the width and the at-a-glance "where was that?" failed.

### Short format

`services/geocode_service._format_short()` parses Nominatim's structured `address` object and builds:

- **POI** (shop / amenity / leisure / tourism): `"Lidl, 12345 City"`, `"IKEA, 12345 City"`, `"Kirche St. Anna, 12345 City"`
- **Street**: `"Hauptstraße 42, 12345 City"`
- **Fallback**: `"12345 City"` when nothing more specific is known.

The full Nominatim response now persists in a new `geocode_cache.raw_json` column — the short format can be re-derived any time from the raw data without another API call (rate-limited to 1 req/s anyway).

### Favorites / home / work preserved

The short format only replaces `ParkingEvent.address`. The `label` field (`home` / `work` / `favorite` / `other`) and `favorite_name` are untouched — the UI still renders favorites with their user-assigned name; the address only falls back in for `label == 'other'`.

### Migration

`rebuild_legacy_entries()` re-fetches every cache row without `raw_json` once via Nominatim (rate-limited). Then `PE.address` gets cleared and `geocode_missing_events()` runs — that copies the now-short addresses out of the freshly filled cache into the PE rows, without another API call. One-shot after deploy across all three hosts.

## v2.28.16 (2026-04-20)

### Hide phantom trips from the driving log

v2.28.15 made phantom trips visually identifiable (0 km + no SDK stats). User's follow-up: "phantom fahrten gleich entfernen, die braucht keiner." Done.

`get_trips` now drops any PE pair that has **both** `km ∈ {0, None}` **and** no matching SDK trip. A real drive always either moves the odometer or produces a `/tripinfo` record (usually both) — anything satisfying neither is GPS-jitter noise: the car briefly appearing at a distant spot for a handful of seconds, then going back where it actually was. The underlying `ParkingEvent` rows stay in the database (useful for diagnostics and for any future reconcile heuristics that want to fix rather than hide); only the `/trips` rendering filters them out.

Also: `pe_covered_dates` now gets filled *after* the phantom filter, so days where the only PE pair was a phantom cleanly fall through to the SDK-only fallback path — rare, but keeps the driving log honest on "polling missed everything, only SDK trips exist" days.

Read-only — no DB migration, no replay.

## v2.28.15 (2026-04-20)

### SDK-stats dedup + reconcile conflict-check refactor

Two related bugs surfaced on ev-dirk: a phantom Home→Work PE pair (GPS-jitter "trip" of 0 km right after a real Work→Home drive) was inheriting the *real* drive's SDK stats (drive_minutes / idle_minutes / avg_speed / max_speed all identical to the line above). Looked like a duplicate entry.

### Bug 1 — `_find_sdk_stats` had no dedup, 60-min tolerance

```text
PE1 Work→Home  depart 16:23  → SDK trip X (drive 29, idle 11, max 83)
PE2 Home→Work  depart 16:53  → SDK trip X (same!)
```

Both pairs' `departed_at` were within the old 60-min tolerance of the same SDK trip's start, and `_find_sdk_stats` took the closest for *each* pair independently. Two PE pairs, one SDK trip, stats duplicated on both.

**Fix**: tightened tolerance to ±20 min (consistent with `trip_reconcile`), and `_find_sdk_stats` now accepts an `exclude_ids` set. `get_trips` maintains a `used_sdk_ids` set as it iterates, so each SDK trip binds to at most one PE pair. After v2.28.12 reconcile, real pairs have `prev.departed_at == sdk.start_time` exactly — delta 0, so they always outscore any phantom competitor. Phantoms now render with no stats attached, making them easy to spot.

### Bug 2 — reconcile greedy-allocator burned SDK on conflicted matches

While investigating, found a latent issue in `trip_reconcile.reconcile_day`: the greedy allocator marked `(pair, sdk)` as "used" before checking adjacency conflicts. If the best-delta candidate conflicted (e.g. the phantom pair won the score sort because its `curr.arrived_at ≈ sdk.end_time` was tighter, but applying `new_dep = sdk.start_time` violated `new_dep >= prev.arrived_at`), the SDK got marked used but never applied — and the valid alternative pair couldn't claim it on its next iteration.

**Fix**: moved the conflict check *inside* the greedy allocator, before `used_sdk.add(si)`. Rejected candidates now leave the SDK / pair slots open for the next-best match.

### Rollout

- `services/trips_service.py` — tolerance tightened, `exclude_ids` plumbed through, `used_sdk_ids` in `get_trips`.
- `services/trip_reconcile.py` — conflict check relocated; apply loop simplified (no longer re-checks).
- Both changes read-only in data terms; rerun `reconcile_range(days=30)` on hosts so existing PE pairs that were previously blocked by the greedy-allocator bug get their `departed_at` corrected.

## v2.28.14 (2026-04-20)

### Fahrtenbuch trip-end SoC — ignore cache-echo on arrival

v2.28.13 fixed the start-SoC side. Debugging the remaining "0 % consumption" trips on ev-robert (Kia) surfaced a mirror-image bug at the *other* end. Example: PE#28→#29 on 2026-04-16, 30 km drive, start SoC 51 %. The first at-destination sync (15:39:22) reported SoC = **51 %** — identical to the pre-drive value. The next sync (15:48, 9 min later) reported SoC = **43 %**. That's the real trip end. 51 − 43 = 8 % consumption (≈ 2.7 %/10 km, plausible for an e-GMP 64 kWh battery).

### Cache-echo at arrival

Both Kia UVO and Hyundai Bluelink sometimes upload fresh GPS / odometer but lag on SoC: the first sync at the destination carries the SoC value the ECU held *before* the drive, the true post-drive SoC lands a few minutes later. Taking `curr.soc_arrived` at face value on those trips produces `start_soc == end_soc` → `soc_used = 0`.

### Fix

Trip-end SoC now reads `min(soc) in [curr.arrived_at, curr.arrived_at + 30 min]` via a new helper `_soc_min_in()`. If any sync in that window has a lower SoC than the arrival echo, it's the real value. Safe under destination charging: charging only raises SoC, so the minimum window stays below the eventual at-destination SoC — but *is* the real trip-end. Fallback to `curr.soc_arrived` if the window is empty (edge case only).

Combined with v2.28.13 (start-SoC from VehicleSync), the driving log now reports realistic consumption on both brands regardless of cache-echo quirks at either end of the trip. Read-only — no DB migration, no PE replay.

## v2.28.13 (2026-04-20)

### Fahrtenbuch SoC — read from VehicleSync, not from PE.soc_departed

Follow-up to v2.28.11/v2.28.12: one more blind spot became visible once the timestamps were correct. The `soc_used` column in the driving log was showing 0 % for trips that obviously consumed energy. Example: park at home Fri 34 %, PV-charge to 100 % over the weekend, drive out Sun with 100 %, arrive 85 %. Expected: 15 % used. Shown: 0 %.

Root cause: `PE.soc_departed` only advances when `update_parking_from_sync` decides the sync is an at-spot sync. Since v2.28.11 the staleness filter discards cached-echo Hyundai syncs before they reach that branch — and Kia behaves similarly because deep-sleep responses return `location = null`. Net effect on both brands: no at-spot sync advances `soc_departed`, so it stays frozen at `soc_arrived` for the entire parking spell. Meanwhile the SoC on those same filtered syncs is perfectly fresh (the ECU reports SoC correctly even when it doesn't re-broadcast GPS), and every one of those SoC readings lives in the `VehicleSync` table regardless of whether the PE hook used it.

### Fix

`get_trips` now uses `_soc_before(prev.departed_at)` as the *primary* source for trip start SoC, with `prev.soc_departed` and `prev.soc_arrived` as fallbacks only when no earlier sync exists at all. `_soc_before` reads directly from `VehicleSync` and picks up every SoC update, including ones from syncs that the PE hook correctly ignored on GPS grounds.

After v2.28.12, `prev.departed_at ≈ sdk.start_time` (the ECU's own record of when the drive began), so "last SoC strictly before departed_at" is a clean anchor: any charging session that ended before the drive gets reflected, any ongoing session up to the moment of departure gets reflected, and nothing from the drive itself leaks in.

### Effect

- Both brands (same read-path) — PV-charge scenarios now show correct consumption.
- Drives with no mid-park charging are unchanged (VehicleSync's last SoC before departure equals the arrival SoC in that case).
- Read-only fix — no DB migration, no PE replay; just redeploy and restart.

## v2.28.12 (2026-04-20)

### Correct `departed_at` on **both** Kia and Hyundai — "car sleeps at origin" fix

Even after v2.28.11 killed the GPS cache-echo artefacts, one structural problem remained on *both* brands: the PE `departed_at` column is set to `last_seen_at` — the last sync that confirmed the car was still at the origin. When the car sleeps at the origin through the entire smart-window (user parks at home 18.04 12:42, drives away 20.04 06:30), there are zero intermediate at-spot syncs, so `last_seen_at = arrived_at` and closing the PE records `departed_at = 18.04 12:42` — a full 42 h before the actual departure. Both brands exhibit this: Kia returns `location = null` during deep-sleep, Hyundai returns a stale cache echo (which v2.28.11 now filters out) — in *both* cases there are no real at-spot syncs to advance `last_seen_at`.

The live-polling side has no way to observe an actual drive-off moment: by the time the car pings fresh GPS, it has already been moving for a while. But the SDK's `/tripinfo` endpoint reports the exact minute the car's ECU started the trip. We already pull it nightly (v2.28.10). v2.28.12 rewires `trip_reconcile` to use it.

### What changed

**Anchor flipped: match by arrival, not departure.** Before v2.28.12 the reconcile matched an SDK trip to a PE pair by comparing `sdk.start_time` with `prev.departed_at` (±20 min). Since `prev.departed_at` can be days off, matches were getting lost exactly when they were most needed. Now the anchor is `sdk.start_time + drive + idle ≈ curr.arrived_at` (±20 min) — `arrived_at` is the reliable end of a PE pair (set by the first live at-destination sync, typically within ~10 min of actual arrival).

**Write semantics: correct `departed_at` only, never touch `arrived_at`.** The PE arrival came from a real sync; it's the best signal we have. v2.28.11 was overwriting *both* ends with SDK-derived values, which could subtly shift a correct arrival timestamp in either direction. v2.28.12 rewrites only `prev.departed_at = sdk.start_time`.

**Brand gate: Kia + Hyundai both qualify.** Previously guarded by `_is_hyundai()`. Both brands expose `update_day_trip_info`; both exhibit the "sleep at origin → stale departed_at" failure mode. The gate is now `_brand_supports_trip_info()` and admits both.

**Pair selection widened.** Old code only considered PE pairs whose `prev.departed_at.date() == target_date`. With stale departed_at values that excluded the exact pairs that needed the most correction. Now: pair qualifies if either end (`prev.departed_at` OR `curr.arrived_at`) falls on the target date.

### Rollout

- `services/trip_reconcile.py` — new matching logic, new brand gate, reduced write scope
- `services/vehicle/trip_log_fetch.py` — inline reconcile after each fetch runs for both brands (no more Hyundai-only `_is_hyundai` check)
- `services/vehicle/sync_service.py` — renamed `_maybe_daily_hyundai_reconcile → _maybe_daily_trip_reconcile`, comments updated

All three production hosts: wipe + replay ParkingEvents under v2.28.11's staleness filter, then `reconcile_range(days=30)` with the new anchor. Both the Kia install and the Hyundai installs benefit — same code path, same correction.

## v2.28.11 (2026-04-20)

### GPS staleness filter for ParkingEvent — fixes phantom transitions on Hyundai

Two cars feeding into the same parking-event state machine were producing wildly different trip logs: Kia was clean, Hyundai e-GMP full of phantom stops and 0-km trips. Forensics on the raw responses revealed the root cause is **not** in our processing of the data but in what each brand actually sends:

- **Kia UVO** returns `vehicleLocation.coord = null` when the car is deep-sleeping (35 % of syncs over a 3-day window had no lat/lon).
- **Hyundai Bluelink** *always* returns a lat/lon — but when the car is asleep, it's the cached last-known position, sometimes hours old (observed: 40 min, 229 min, 344 min, 404 min GPS ages on otherwise-healthy syncs).

The existing PE guard `if sync.location_lat is None: return None` was meant to catch the "no GPS" case. It did its job on Kia (which honestly signals "no fix"), but on Hyundai it never triggered because Hyundai lied about having a fresh fix. PE accepted every cached echo as if it were live and built a fantasy timeline out of them.

### The fix

**New column** `vehicle_syncs.location_last_updated_at` (DateTime, nullable). Captures the ECU-side GPS timestamp — i.e. when the car last actually reported a position, as opposed to when we polled. Populated by a new parser `_extract_location_last_updated(status, raw_json)` that prefers the SDK's `Vehicle.location_last_updated_at` attribute (already datetime-typed) and falls back to `data.vehicleLocation.time` (`YYYYMMDDHHMMSS` string) in the raw payload.

**New staleness gate** in `services.trips_service.update_parking_from_sync()` — added right after the existing `location_lat is None` guard:

```python
if sync.location_last_updated_at is not None:
    age_min = (sync.timestamp - sync.location_last_updated_at).total_seconds() / 60.0
    if age_min > STALE_GPS_MAX_MIN:   # 30.0 by default
        return None
```

30 min is generous — smart-mode polls run every 10 min so legitimate fresh data is typically 0–20 min old. Anything older has a very high prior of being a cache echo. Legacy rows with `location_last_updated_at = NULL` fall through to the old behaviour (trust the GPS), which keeps things working during the backfill transition.

**Migration** follows the existing `_new_sync_cols` idiom in `app.py`. Column addition is idempotent on every boot.

**Dedup unchanged:** `VehicleSync.TRACKED_FIELDS` is deliberately *not* extended with the new column. Staleness should never drive "a new sync row must be persisted" — only actual movement of the tracked fields (lat, lon, soc, odo, etc.) should.

### Effect on the two brands

| | Kia | Hyundai |
|---|---|---|
| Syncs where column would help | ~1–2 per week | ~4 per day |
| Expected impact on PE | near-zero | dramatic reduction in phantom events |

Post-deploy: one-time backfill populates `location_last_updated_at` from existing `raw_json` rows, and a PE replay (wipe + `backfill_parking_events`) on Hyundai-heavy installs rebuilds the trip log under the new filter. Kia hosts get the column filled but are expected to produce the same PE set as before.

## v2.28.10 (2026-04-20)

### Nightly reconcile fires at 03:00 local instead of "first sync tick"

v2.28.9 hung the daily Hyundai PE↔SDK reconcile off the main sync loop: it ran on the first sync tick of a new calendar day, which in smart mode means 06:00 at the earliest (whenever the active window opens), later on cached/force modes. That's "sometime in the morning," not a deterministic time.

Moved to a dedicated thread (`_nightly_maintenance_loop`) that wakes at **03:00 local** regardless of sync mode or smart-window settings:
- Hyundai has yesterday's `/tripinfo` fully populated by then (the car uploads trip records within minutes of engine-off).
- The car is almost certainly parked / asleep, so the passive SDK pull doesn't collide with live polling.
- Well before smart-window start → main sync cadence is undisturbed.

Startup catch-up: if the service comes up past 03:00 on a day where the reconcile hasn't run yet, it fires once immediately instead of waiting until 03:00 the next morning.

Brand gate (`_is_hyundai()`) is still checked inside `_maybe_daily_hyundai_reconcile`, so Kia installs just no-op each wake-up. The thread itself runs everywhere — there's no point gating the wake, because the cost of a wake + no-op is effectively free (one AppConfig read per day).

### Confirming both sources in the DB

Side-note for anyone reading this via the settings UI: this repo *always* stores both the polling-based view (`ParkingEvent` table — GPS, time, SoC, odometer, labels) and the Hyundai server view (`VehicleTrip` table — `/tripinfo` rows with minute-accurate start / drive / idle / distance). Neither replaces the other. The reconcile just aligns the PE timestamps to SDK where a clean 1:1 match is found — it does not merge, split, or delete PE rows.

## v2.28.9 (2026-04-20)

### Daily PE ↔ SDK trip reconcile (Hyundai only)

On Hyundai e-GMP installs, ParkingEvent timestamps (`departed_at` = last at-spot sync, `arrived_at` = first sync at new spot) run several minutes to several hours off real drive start / end — the polling cadence is coarse and smart-mode syncs can be 10 min apart. Meanwhile the Hyundai server's `/tripinfo` endpoint returns minute-accurate start / drive-time / idle-time / distance per trip (we already store this in `VehicleTrip`).

New module **`services/trip_reconcile.py`** greedy-matches PE pairs to SDK trips — strict tolerances (±20 min on start_time, km delta within 25% or ≤3 km absolute), each SDK trip binds to at most one PE pair, and matches that would cross adjacent-event boundaries are skipped. When a clean 1:1 match lands, we overwrite `prev.departed_at = sdk.start_time` and `curr.arrived_at = sdk.start + drive + idle`.

**What this does NOT do:** merge PE pairs that split across a single SDK trip (GPS-jitter artefacts), split PE pairs that merged several short SDK trips (below-threshold stops), or backfill missed trips. Those represent different views of the same journey and can't be reconciled without data loss.

**Brand gate:** runs only when `vehicle_api_brand == 'hyundai'`. Kia (ev-robert reference install, 400 V platform) is untouched — the `_is_hyundai()` check short-circuits every entry point. Tested both branches locally.

### Trigger points

1. **Inline after SDK fetch** (`trip_log_fetch.fetch_day_trip_info`): whenever a day's SDK trips are pulled fresh, the same day's PE pairs are reconciled immediately. Wrapped so a reconcile failure never blocks the fetch result.

2. **Daily from the sync loop** (`sync_service._maybe_daily_hyundai_reconcile`): once per calendar day, after a normal sync tick, backfill the last 3 days of SDK trips and reconcile. `last_reconcile_at` in `AppConfig` prevents same-day re-runs. The gate `should_run_daily()` is Hyundai-only.

### What a typical run looks like on ev-dirk

Runs recorded (2026-04-20): 17 PE pairs on the last 5 days, 2 applied (PE#3→#4 arrived_at 12:48 → 10:45, PE#5→#6 overnight arrival 06:01 → 00:16), 1 skipped conflict, 14 unmatched. The unmatched cases are legitimate structural mismatches — PE merging multiple short SDK trips or PE splitting via GPS jitter — not cases we should be touching.

Daily API budget impact: +3 calls/day (three days of `backfill`) on Hyundai hosts. Kia hosts: zero extra calls.

## v2.28.8 (2026-04-20)

### Dashboard SoH: kill the bogus consumption-based fallback that always showed ~125 % on e-GMP

Root cause of the "SoH still shows 125 % after the v2.27.0 baseline fix" that survived even v2.28.7's localStorage cache bust:

The dashboard JS has had, for a long time, a fallback path when `/api/vehicle/status` returns `battery_soh = null`:

```js
const soh = (consumedKwh / 1000 / battKwh * 100).toFixed(1);
```

This is *not* SoH — it's "lifetime energy drawn / nominal pack capacity × 100", i.e. how many nominal-full-batteries-worth the car has consumed over its life. For an Ioniq 5 with ~904 kWh lifetime consumption and a 72.6 kWh pack, that's `904.6 / 72.6 ≈ 12.46`, scaled to a percent somehow lands at ~125 %. Total coincidence that it looks like a plausible SoH; it has nothing to do with battery health.

It fires whenever the connector returns `battery_soh_percent = None`, which on Kia/Hyundai is the common case — the API only includes SoH on force-refresh responses, not on cached ones. So every non-force dashboard poll silently rewrote the SoH card with this fake number.

### Fix

1. **Backend** (`/api/vehicle/status` in `app.py`): when the connector's fresh `s.battery_soh_percent` is `None`, fall back to the most recent `VehicleSync` row that carries a non-null SoH. This means cached polls still surface the last-known SoH (via `scale_soh()`) instead of sending `null` downstream.

2. **Frontend** (`templates/dashboard.html`): remove the consumption-based fallback branch entirely. If the backend can't provide a SoH even from DB history, we show `—` instead of inventing one.

Force-refresh behaviour is unchanged: when the API does return a fresh SoH, that's what surfaces.

## v2.28.7 (2026-04-20)

### Dashboard: version-key the vehicle localStorage cache

Companion to v2.28.5's update-banner cache fix. The dashboard's live-vehicle card caches the full `/api/vehicle/status` response in `localStorage['ev_vehicle_data']` so page loads render instantly from the previous fetch. Key was fixed (no version suffix), so any semantic change to the payload survived across versions: after v2.27.0 introduced display-time SoH baseline scaling, users on the Hyundai install kept seeing the pre-scaling raw 125 % because the old cached value was rendered on page load and — depending on polling state — never got replaced.

Keying the cache on `Config.APP_VERSION` (same approach as `ev_update_check_cache_v2_…` in v2.28.5) means every deployed version sees its own slot. An applied update auto-orphans the prior slot. One-shot cleanup on load removes the legacy unkeyed entry and any previous-version slots so `localStorage` doesn't grow one entry per release.

After deploying, **a single browser reload** is enough — the new version's JS starts writing to the new key immediately, the old key gets removed on first visit.

## v2.28.6 (2026-04-20)

### Settings → Gefahrenzone: "Komplett zurücksetzen" Button

Until now, repurposing an install (getting it back to a freshly-provisioned state — no password, no DB, no credentials, no settings) meant shell access: `rm data/ev_tracker.db && systemctl restart ev-tracker.service`, or a full reinstall. Convenient for admins, not for the actual user. New UI path:

**Settings → Gefahrenzone → "Komplett zurücksetzen …"** opens a modal that asks for the current web-UI password (if auth is enabled) and a typed `RESET` confirmation, then wipes:

- `data/ev_tracker.db` entirely (all tables, including `AppConfig`, so password + API credentials go with it)
- `data/notify.json` if it exists

Before wiping, the server writes a timestamped copy to `data/backups/ev_tracker-pre-factory-reset-YYYYMMDD-HHMMSS.db` — not surfaced in the UI, just sitting on disk as an "oh shit" rescue. After the wipe, the backend disposes the SQLAlchemy engine, schedules a systemd restart (same mechanism as backup-import), and `os._exit(0)`s as a belt-and-suspenders so a supervisor still brings us back if the sudo-restart fails.

On the restart, the app comes up with an empty DB, `is_auth_enabled()` returns False (the flag was in `AppConfig`), and the user lands on `/` with an unlocked dashboard — exactly the state of a fresh install right after the first visit, before any password has been set.

**This does not touch LUKS, the venv, or the repo** — it's purely an app-data wipe. Anything outside `data/` (updates/, logs/, /srv/ev-data's mountpoint setup) is untouched. If you need to reset the *LUKS volume* because you've forgotten the passphrase, that remains a shell-only operation (documented in `deploy/README.md`).

Twenty new translation keys (`set.danger_*`) in all six languages.

### Safety gates

Three gates prevent accidental / drive-by resets:

1. **Login required** when auth is on (standard auth guard already handles this)
2. **Typed confirmation**: the request body must contain `confirmation: "RESET"` verbatim — no variations, no checkboxes. Any JSON submission that skips this gets a 400 `confirmation_mismatch` before any file is touched.
3. **Password re-verification**: if auth is enabled, the current password is re-checked server-side via `verify_credentials()`. Wrong password → 401 `wrong_password`, no wipe happens.

Installs without auth (opt-out configurations) skip the password step but still require the typed confirmation.

## v2.28.5 (2026-04-20)

### Dashboard update-banner: bust cache after an applied update

The dashboard's update-check banner (`templates/dashboard.html`) caches the `/api/update/check` response in `sessionStorage` for 30 minutes to avoid spamming GitHub. The cache key was just `ev_update_check_cache_v1` — version-agnostic — so after a successful update, the stale cache entry (with `update_available: true`) kept showing the banner until either the TTL expired or the user closed the tab. A plain page reload does NOT clear `sessionStorage`, so reloading didn't help the confused user.

Keying the cache on `{{ config.APP_VERSION }}` makes every version see its own cache slot: the moment the app restarts on a new version, the old key is orphaned and a fresh fetch happens on the first dashboard load.

## v2.28.4 (2026-04-20)

### Trip log: decouple `departed_at` from next `arrived_at` + motion-triggered force-refresh

v2.28.3 fixed *what* `soc_departed` / `odometer_departed` contained, but their **timestamp** — `prev.departed_at` — was still `sync.timestamp` of the *first sync at the new location*. Because that's the same sync that opens the next ParkingEvent, `prev.departed_at == curr.arrived_at` in every case: the trip-edit modal showed identical timestamps on both sides, and the "time the car spent driving" was structurally 0 min.

**Fix:** when closing the event on move detection, set `departed_at = last_seen_at` (the last sync while still at the old spot), not the post-drive sync timestamp. Now `prev.departed_at < curr.arrived_at`, and the gap reflects the actual blind window between "last known at origin" and "first known at destination" — i.e. drive time plus any poll-interval slack.

### Trip-list SoC/km calc simplified

`get_trips()` now uses `prev.soc_departed` and `prev.odometer_departed` directly as the origin values (post v2.28.3 they hold the last at-spot readings). `_soc_before()` drops to fallback status for legacy events that still hold pre-v2.28.3 capture data. Kilometrage similarly prefers `prev.odometer_departed` over `prev.odometer_arrived`.

### Motion-triggered force-refresh

The original user concern — "the system should get a fresh SoC before I drive" — can't literally be met: we can't predict departure. But we *can* make the **arrival** side fresh. When `update_parking_from_sync()` detects a move (GPS jump ≥ 100 m), it now calls `sync_service.request_force_refresh('motion_detected')`, which queues a force-refresh on the next sync-loop tick. The sleep loop breaks out within ~10 s so the follow-up wake happens quickly. This ensures `curr.arrived_at` / `soc_arrived` / `odometer_arrived` reflect a fresh reading even if the sync that detected motion was cached.

### Data-gap hint in the trip-edit modal

Between the Startpunkt / Zielpunkt cards there's now an info banner showing the drive window: "Fahrt: ca. 34 min — Abfahrt 11:56 → Ankunft 12:30. Während dieser Zeit keine Sync-Daten (Fahrt-Intervall)." Makes the polling-interval uncertainty explicit so the user knows how fresh the endpoint data is. Two new i18n keys (`trips.edit_gap_short`, `trips.edit_gap_hint`) in all six languages.

### Migration

Existing ParkingEvents still have `departed_at = post-drive sync timestamp`. Run **Fahrtenbuch → "Aus Historie nachbauen"** (with wipe) once after updating to rebuild the event chain with the new logic. New events captured from this release onward are already correct.

## v2.28.3 (2026-04-20)

### Trip edit modal: fix SoC/km on `departed` side + clearer labels

Two closely related issues in the trip-edit modal:

1. **`soc_departed` / `odometer_departed` captured the wrong sync.** `_maybe_update_parking_event` in `services/trips_service.py` only wrote these fields at the moment the car was detected at a new location — meaning the values came from the *first sync at the destination*, not the last sync while still at the origin. The trip's SoC drop and drive-km delta ended up attributed to the *origin parking event* instead of the trip. Example from the field: Elbepark showed arrival 81 % → departure 67 % (14 % "lost while parked"), while the subsequent drive to home showed 67 % → 67 % (zero SoC used for a 20 km trip).

   Fix: update `soc_departed` / `odometer_departed` on every same-place sync so they continuously hold the last-known at-spot values. On move detection, don't overwrite — the fields already contain the correct pre-departure state. `_open_event` also initialises `*_departed = *_arrived` so single-sync events still have meaningful data.

   The displayed trip consumption in the table was already correct (uses `_soc_before()` via `VehicleSync`), so only the edit modal and the raw `ParkingEvent` values needed the fix. Existing events can be repaired with the **"Aus Historie nachbauen"** button (wipe + replay).

2. **Labels "Angekommen / Abgefahren" were ambiguous.** On the Startpunkt card, "Angekommen" means the previous trip's end, and "Abgefahren" is this trip's start — on the Zielpunkt card it's the opposite. Same label pair in both places, with the trip-relevant one flipping sides. Users had to infer from context which field mattered.

   Added a coloured subheader on each card ("Fahrtbeginn — Abfahrt von diesem Ort" / "Fahrtende — Ankunft an diesem Ort") and a small `(Fahrtbeginn)` / `(Fahrtende)` suffix in the trip's primary colour on the trip-relevant field labels. The non-trip-relevant column stays neutral. Four new translation keys (`trips.edit_trip_start`, `trips.edit_trip_end`, plus `_hint` variants) in all six languages.

## v2.28.2 (2026-04-19)

### Installer auto-picks a free port; `APP_PORT` env var overrides

Running the installer on a machine that already had something on port 7654 failed with `Address already in use` right after the file swap, because the systemd unit hardcoded the port and the installer had no fallback. Now:

- `Config.APP_PORT` reads from the `APP_PORT` env var, falling back to `7654` (same for `APP_HOST`).
- `deploy/install.sh` checks whether 7654 is free before writing the systemd unit. If it's taken, it walks 7655 → 7700 and picks the first open port. The chosen port is injected into the unit via `Environment=APP_PORT=<n>`.
- `EV_APP_PORT=<n>` env override lets you pin a specific port for the installer (skip auto-discovery).
- The Tailscale-serve step and the "fertig" banner at the end both use the actual chosen port, not a hardcoded 7654.

`deploy/ev-tracker.service` was updated to include an `Environment=APP_PORT=7654` line as the default; the installer overwrites it with whatever port it picked.

## v2.28.1 (2026-04-19)

### Installer: show pip progress instead of silent hang

v2.28.0 passed `--quiet` to `pip install` in `deploy/install.sh`, which suppresses the download/build output entirely. On slow networks or ARM hardware where pandas/matplotlib/numpy take several minutes, the installer looked hung between `Baue Python-venv & installiere requirements.txt …` and the next line. Dropped `--quiet`, added `--progress-bar on`, and preceded the step with a "kann 2–5 min dauern" log line so expectations are set. Also split pip-upgrade and requirements install into two log blocks for clarity.

## v2.28.0 (2026-04-19)

### One-shot installer for new hosts

Bringing up a new install used to mean manually stepping through: apt-get the right packages, create the `ev-tracker` user, clone the repo, build a venv, drop the systemd unit in place, write the sudoers file, enable and start the service. That's about 20 lines of shell and two or three things that are easy to get subtly wrong.

Now it's one command. On any fresh Debian/Ubuntu host:

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/install.sh | sudo bash
```

The installer lives at `deploy/install.sh` and ships alongside three configuration artefacts (`deploy/ev-tracker.service`, `deploy/sudoers.ev-tracker`, `deploy/ev-unlock`) plus a README with env-var overrides, non-interactive usage, and LUKS setup notes. It's idempotent — rerunning it on an existing install pulls latest code and re-runs pip without touching the database.

Tailscale install + `tailscale serve --https=443` is offered as an optional step at the end. Skip with `EV_WITH_TAILSCALE=0` or accept the interactive prompt.

**What the installer does NOT do:** LUKS volume setup (the installer detects `/srv/ev-data` as an existing mount point and leaves the filesystem alone, but it won't create the encrypted volume for you), Debian base install, fail2ban rule tuning. Those are pre-install steps documented in `deploy/README.md` for the full-paranoia setup the reference installs use.

## v2.27.1 (2026-04-19)

### Translate `set.soh_baseline` / `set.soh_baseline_hint` into ES/FR/IT/NL

v2.27.0 only added the two new SoH-baseline strings to the DE and EN files. The i18n fallback chain in `services/i18n.py:t()` goes `current_lang → de → key`, so any install running in Spanish/French/Italian/Dutch ended up rendering the German hint text verbatim on the Settings page — the exact "hardcoded German leaking through" regression v2.25.3 was supposed to prevent. Added native translations for the remaining four languages; key counts now DE/EN = 897, ES/FR/IT/NL = 605.

## v2.27.0 (2026-04-19)

### Battery SoH baseline — realistic percentage for Hyundai/Kia e-GMP

On the Hyundai install (Ioniq 5 AWD LR, 800 V e-GMP platform) the dashboard has been showing SoH values around 125 % for the past months. Not a bug in our code — the Hyundai/Kia BMS on e-GMP vehicles reports `ev_battery_soh_percentage` against an internal warranty-floor reference (≈ 80 % of gross capacity), so a fresh battery reads ~125 % and degrades toward 100 % as it ages out of warranty. The vehicle_raw detail page already annotated this ("kia_soh_over_100" note), but the user-facing number was still the raw reading. Older 400 V platforms (Kia Niro EV) report against nominal capacity and show correct 100 %-ish values, so the quirk is specifically e-GMP.

Added a user-configurable `battery_soh_baseline` setting (default 100; 125 for e-GMP). Scaling happens at display time (`scale_soh(raw) = raw / baseline * 100`) in `services/stats_service` — no DB migration, existing historical values in `vehicle_syncs.battery_soh_percent` stay raw and get rescaled on every render. Changing the baseline retroactively re-scales the whole history graph, so switching from 100 → 125 on an e-GMP install immediately makes the dashboard read ~100 % and the history curve shows the real degradation slope.

Wired at:
- `/api/vehicle/status` (`battery_soh` in the dashboard JSON)
- `services.stats_service.get_vehicle_history` (`series.soh[]` and `summary.last.soh`)
- `/vehicle/raw/<id>` (normalized panel now shows scaled + raw + baseline side-by-side)

New Settings field in Vehicle section with a hint explaining the 100 vs 125 choice. Two new DE/EN translation keys (`set.soh_baseline`, `set.soh_baseline_hint`) — both keyspaces at 897 now.

The Hyundai install is pre-configured to `battery_soh_baseline=125` as part of this deploy.

## v2.26.0 (2026-04-18)

### Trip log: ParkingEvent-primary again, SDK demoted to manual backfill

v2.24 had made the Kia/Hyundai `update_day_trip_info` SDK endpoint the *primary* source for the trip list, with ParkingEvent pairs doing GPS enrichment underneath. In steady state this worked, but whenever polling was sparser than the SDK's trip count — either because smart-sync was narrowed, or simply because short back-to-back trips clustered faster than the poll interval — the unmatched SDK rows fell through to "Ort unbekannt" instead of a real address. The user reported this as a regression: the pre-v2.24 ParkingEvent-pair view had shown fewer (merged) trips but *every* trip had a location.

Back to ParkingEvent pairs as the source of truth. `services/trips_service.get_trips()` now iterates `parking_events` pairwise exactly like it did in v2.23, and only falls back to SDK `vehicle_trips` rows on dates where zero ParkingEvent pairs exist (i.e. historical days from before polling was on). Where a polled trip's `departed_at` lines up within 60 min of an SDK row's `start_time`, the SDK row's stats (drive/idle minutes, avg/max speed) ride along as extra detail on the polled trip — best of both worlds.

**SoC calculation bug fix.** While rewriting `get_trips` I noticed the pre-existing SoC delta was wrong: it used `prev.soc_arrived` (SoC when the car arrived at the *start* location, possibly captured pre-charge) minus `curr.soc_arrived` (SoC at trip end). If the user charged between arriving at and leaving the start location, the "used" SoC included the charge and got clamped to 0 by the `max(..., 0)`. Changed to `prev.soc_departed` (SoC captured by the sync that first noticed the car leaving), falling back to `prev.soc_arrived` only when no departure sync fired. Values now match what the Bluelink/UVO app reports.

**Auto-fetch disabled.** `maybe_auto_fetch()` was called from `services/vehicle/sync_service._save_vehicle_sync` after every successful poll (rate-limited to 1×/h), silently pulling SDK trip-info. Since the SDK rows now only show as a fallback, there's no reason to keep burning API calls on auto-fetches for days that already have polling coverage. Removed the hook from the sync loop; the "Vom Fahrzeug-Server laden" button in Settings → Trips still works for manual backfilling of historical days. Corresponding helpers `fetch_recent` / `maybe_auto_fetch` and the `TODAY_REFRESH_AFTER_MIN` constant were deleted from `services/vehicle/trip_log_fetch.py`.

**Dead code removal.** `_assign_events_to_trips` (the 2-pointer merge introduced in v2.24.3), `_sdk_trip_to_dict`, `_load_sync_gps`, `_nearest_sync`, `_sync_point_to_dict`, and the `_SYNC_ENRICH_TOLERANCE_MIN` / `PARKING_MATCH_TOLERANCE_MIN` constants all supported the SDK-as-primary flow that no longer runs. Stripped them out. `_event_to_dict` simplified — no more need to handle `evt is None`.

**Template.** `templates/trips.html` used to show the cloud-check icon only on `source == 'sdk'` rows; now it shows whenever `drive_min` is present, i.e. on polled trips with SDK stats attached *and* on SDK-only historical rows. The tooltip also picks up `avg_speed_kmh` / `max_speed_kmh`.

**User-observed effect.** On the Kia install today the log shows 2 PE-pair trips (home → shopping → home) instead of the 1 consolidated SDK round-trip — matches what polling actually captured. On the Hyundai install where polling is sparser (smart-mode never got turned on explicitly), some days will now show fewer PE-pair trips than SDK had on record; user can enable smart-mode for denser polling, or manually backfill historical days if needed.

## v2.25.3 (2026-04-17)

### i18n audit: translate everything the user actually sees

The user flagged that charge operators show up in German regardless of the configured language. Audited the whole project and fixed every user-visible hardcoded German string.

**Charge operators.** `DEFAULT_OPERATORS` in `app.py` mixed real brand names (IONITY, EnBW, Aral pulse, Tesla Supercharger, etc. — culture-neutral, stay as-is) with three generic labels ("Zuhause / privat", "Arbeit", "Sonstiges") that were only sensible for German. Split the list into `_DEFAULT_OPERATOR_BRANDS` (hardcoded) plus `_DEFAULT_OPERATOR_GENERICS` (translation-keyed via `set.op_home_private` / `set.op_work` / `set.op_other`). New `get_default_operators()` function resolves the generics at call time so the dropdown always matches the current UI language.

**API error / success responses.** Thirteen `jsonify({'error': 'German string'})` and five `{'message': 'German string'}` strings in `app.py` covered the auth / password / database-import / update / MQTT / trip-backfill flows. All now route through new `err.*` and `msg.*` translation-key namespaces (13 error keys, 5 message keys, German + English).

**Maintenance reasons.** `services/maintenance_service.py` emitted "X d überfällig" / "in X d fällig" / "X km überfällig" / "in X km fällig" strings directly. Moved to four `maint.overdue_days|overdue_km|due_in_days|due_in_km` keys with `{days}` / `{km}` placeholders.

**Dashboard regen window.** "(3 Mon.)" suffix next to the regeneration counter was hardcoded. Now `dash.regen_window_short` — "(3 Mon.)" in German, "(3 mo)" in English.

**Token-fetch step instructions.** The three-step instructions in Settings → Vehicle API for the manual refresh-token flow were pure hardcoded German; their JavaScript status messages ("URL leer", "Tausche Code gegen Token...", "Token gespeichert...", "Fehler") same story. All translated now via six new `set.api_token_step{1,2,3}_{title,body}` keys plus four `set.api_token_paste_{url_empty,exchanging,saved,error_generic}` keys wired through the existing T JS object.

**Verification.** Ran a parity check after: DE 895 ↔ EN 895 keys, zero orphans either side. All four primary pages (/, /settings, /trips, /report) render 200 in both languages.

What's still German on purpose: real brand names in DEFAULT_OPERATORS (IONITY, Tesla etc.), "Stadtwerke" (German near-brand for municipal utilities with no snappy English equivalent), German comments in the source. The legacy PDF report generator (`services/report_service.py`) and the token-fetch Selenium helper (`services/vehicle/token_fetch.py`) still contain hardcoded German — both run in rare one-off flows (PDF download, initial setup) rather than the daily UI, so they're deferred.

## v2.25.2 (2026-04-17)

### Report: localise week / "all time" / month-name labels

The v2.25.0 report page leaked three hardcoded German strings through its JSON payload: the week-preset label (`KW 16/2026`), the all-time label (`Gesamtzeitraum`), and the per-bucket weekly chart label (`KW16`). On top of that, `strftime('%B %Y')` for the month-preset label depended on the server's LC_TIME locale, which isn't what the app language is actually set to.

Moved all four into a `_LABELS` pack keyed by language inside `services/report_range.py` with explicit month-name arrays for DE and EN, so "April 2026" renders as "April 2026" (both languages — coincidentally identical) while "KW 16/2026" becomes "Week 16/2026" in English and "Gesamtzeitraum" becomes "All time". `resolve_range()` and `build_report()` now accept a `lang` kwarg; the `/api/report` route pulls it from `AppConfig['app_language']` and passes it through.

No other v2.23–v2.25 changes had hardcoded user-visible German — verified by grepping the touched files for umlauts and German word tokens.

## v2.25.1 (2026-04-17)

### Warn users that narrowing the smart-sync window degrades trip location data

The smart-sync window controls when the background polling loop actually runs (default 06:00–22:00 every 10 min). Tightening it — starting later, ending earlier, or polling less often — has a subtle downstream effect on the v2.24+ trip log: the **SDK trip list itself stays accurate** because it's fetched from the Kia/Hyundai server's cache independently of our polling, but the **From/To address labels** come from `parking_events` / `vehicle_syncs`, which are only populated while the poll loop is active. Trips happening outside the active window therefore get "Ort unbekannt" / "Unknown location" instead of a real address.

Added an inline warning banner in Settings → Vehicle API that appears the moment any of the three smart-window selects deviates from the 06:00 / 22:00 / 10-min default. The banner explicitly calls out that trips will still be **detected** (SDK-sourced, polling-independent) but that their **location labels** will often be blank, and offers a one-click "Restore default" button that snaps all three selects back to the recommended values (the user still has to hit Save to persist the change).

The check fires on every `change` event on any of the three selects, and re-evaluates when the user toggles between cached / smart / force modes — so switching back to `smart` immediately re-runs the validation.

## v2.25.0 (2026-04-17)

### Report: new interactive page with period picker and twelve colourful plots

The old `/report` endpoint used to hand out a PDF immediately, which meant two pain points: no way to pick a time range, and no way to see the numbers without a round-trip through a 10-page document. Rebuilt as a proper interactive view.

**Period picker** at the top: Today, Week (current ISO week), Month, Quarter (current calendar quarter), Half-year (H1 / H2), Year, and "All" (earliest recorded charge/trip through today). Plus a custom-range picker with two date inputs when none of those fit. The bucket size for the X-axis adapts automatically — ≤14 days renders daily bars, ≤95 days weekly (KW-NN), longer spans monthly — so a year chart doesn't become 365 illegible bars and a week chart doesn't collapse to a single one.

**Four headline KPIs:** total kWh, total cost (with €/100 km avg), distance driven (with avg consumption in kWh/100 km), and CO₂ saved vs. an equivalent-distance ICE using the user's configured `fossil_co2_per_km`.

**Three highlight cards** underneath: Euros saved vs. ICE (computed against `ice_cost_per_100km`, default €11.55 — configurable in future), biggest single charge in the window (date, kWh, type, cost), and longest single trip (date, km, minutes, max speed when the SDK reports it).

**Twelve plots**, laid out in a responsive 2-column grid:

1. kWh per bucket — blue bar
2. Cost per bucket — red bar
3. km per bucket — green bar
4. CO₂ per bucket — orange bar
5. AC / DC / PV split — doughnut, with kWh + count in the tooltip
6. Consumption over time (kWh/100 km per bucket) — purple filled line
7. Hour-of-day charging pattern — cyan bar (0 – 23)
8. Day-of-week pattern — dual-axis bar (kWh) + line (trips), Mon – Sun
9. Trip-length distribution — green bar (<5 / 5–20 / 20–50 / 50–100 / 100+ km buckets)
10. Electricity price per charge over time — red scatter, €/kWh on Y
11. Top charging operators — horizontal bar by kWh, tooltip shows cost and count
12. Most-visited locations — horizontal bar by parking-event count (home/work/favourite/address)

All plots are rendered client-side via Chart.js 4.4.6 (already a page dependency); the `/api/report?preset=…` endpoint returns a single JSON payload with every series pre-aggregated and pre-rounded so the frontend just has to paint.

**PDF export is preserved** as a "PDF" button on the page — still the original generator at `/report/export.pdf`, so nothing that worked before is gone. The button sits next to the range label so users can download a printable copy of whatever window they have open.

The navbar icon flips from the misleading `bi-file-earmark-pdf` to `bi-bar-chart-line` and the link finally gets a label ("Bericht" / "Report") instead of just "Report". New translation keys: about 45 of them, all under the `report.*` namespace in both `de.json` and `en.json`.

## v2.24.3 (2026-04-17)

### Trip log: sequential 2-pointer matcher fixes adjacent-trip cross-matches

v2.24.2's enrichment looked up the nearest-by-abs-delta parking event for each SDK trip independently, which broke down when two short trips boundaried each other inside the 60-minute matching tolerance. Concrete case from 2026-04-17: the 15:47 trip latched onto the 15:32 `work.departed_at` (polling lag from the previous trip leaving work), pushing the 15:03 trip into the sync fallback and surfacing a visible "home/work" mismatch on consecutive rows.

Replaced the independent per-trip search with an **ordinal two-pointer merge**:

1. Sort SDK trips by start_time.
2. Sort parking events by departed_at (and separately by arrived_at).
3. Walk both lists in lockstep. For each trip, advance the event pointer past events whose timestamp is more than 5 minutes BEFORE the current trip (can't belong to us). If the current event is within the 60-minute forward tolerance, claim it and advance both. If not, leave the trip without a parking match (falls to VehicleSync fallback → 'unknown').

Why this works: our parking-event timestamps always LAG the SDK's authoritative trip boundary (polling notices movement N minutes after it happened). That's an asymmetric relationship, not a symmetric one — a proper matcher has to respect the order. Each parking-event endpoint is consumed at most once per side, so adjacent trips can never fight over the same event.

Verified against the Kia install: consecutive-pair label consistency jumped from 8/13 to 12/13 across the last 14 days. The remaining mismatch on 2026-04-16 is a legitimate data gap (the SDK doesn't report a trip between the "other" arrival at 15:52 and the "home" departure at 16:10 — which would imply a 3rd trip the server didn't record). That's a question for the Bluelink server, not the matcher.

## v2.24.2 (2026-04-17)

### Trip log: honest "unknown location" + VehicleSync fallback for SDK trips

Backfilling 30 days of Bluelink/UVO trips on a host that had the background sync disabled (the Hyundai install before v2.23.2) exposed a gap in the SDK-trip enrichment path: the only location data we store outside the SDK is `parking_events`, and that table was almost empty for the backfill window, so most historical SDK trips couldn't be matched. The UI then fell through to the "Adresse wird ermittelt" branch and sat there forever — not because geocoding was broken, but because there were no coordinates to geocode in the first place.

Two fixes:

**VehicleSync as the second-chance source.** When a SDK trip's start/end doesn't line up with any `parking_events` row (±30 min), we now look for a `vehicle_syncs` row with GPS within ±2 hours. For the "from" endpoint we prefer a sync at or before the trip start; for "to" we prefer one at or after the trip end. That avoids using a mid-trip sync (smart-mode polling at 10-min cadence inside a drive) as a trip endpoint. When we find one, we reverse-geocode via the existing Nominatim-backed address cache — essentially free after the first call per coordinate.

**Explicit "unknown" label.** When neither a parking event nor a nearby sync exists, the endpoint stub now carries `label='unknown'` instead of the generic `'other'` the old code used. The template renders it as `bi-geo-alt-slash` + "Ort unbekannt" / "Unknown location" so users see an honest "we have no GPS for this trip" instead of a perpetual "resolving address". This shows up on the Hyundai install's March-era backfill where the car was driven but no polling data was captured.

The SoC/regen columns continue to work whenever both endpoints match a parking event, which is the common recent case. Nothing changes for users on hosts with a fully populated parking-event history.

## v2.24.1 (2026-04-17)

### Hotfix: 30-day SDK backfill crashed on the first clean day

The `backfill()` helper checked `r.get('skipped_reason', '').startswith(...)` to detect API-quota stops — but the normal-case code path explicitly sets `skipped_reason` to `None`, so `dict.get(key, '')` happily returned `None` (the present-but-None case), and the `.startswith` call blew up on the first successful day. The 30-day button therefore never got past the first fetch.

One-line fix: `reason = r.get('skipped_reason') or ''` before the `startswith` check, so both "key absent" and "key is None" resolve to an empty string.

## v2.24.0 (2026-04-17)

### Driving log: pull trips directly from the Kia/Hyundai server

Until now the driving log was derived from our own GPS polling: every time the car changed location between two syncs, we closed the previous parking event and opened a new one. That works as long as polling is frequent enough to catch every stop. On a Hyundai install where the background sync was disabled, a week's worth of trips collapsed into four parking events, one of which claimed the car had driven 51 km while sitting at home. This release fixes that at the source.

**What changed.** The `hyundai_kia_connect_api` SDK we already depend on exposes `update_day_trip_info(vehicle_id, yyyymmdd)`, which hits the same `/spa/vehicles/<id>/tripinfo` endpoint the Bluelink and UVO mobile apps use when you open the driving log. The car uploads a trip record at the end of every drive as part of its normal telemetry — that upload is independent of anything we do — and the manufacturer's server caches the list. Pulling it costs one API call per day requested (counted against the existing 200/vehicle daily budget) and **does not wake the car**: no cellular modem activation, no 12V battery drain. It's the same data the Bluelink app shows, just pulled from the server instead of the car.

Each trip comes back with a start time (HH:MM:SS), drive and idle minutes, distance in km, and average/max speed — no GPS coordinates, since those live behind a separate endpoint we don't need. We store the trips in a new `vehicle_trips` table and match each one up with the nearest `parking_events` row (±30 minutes tolerance) to fill in the "from" and "to" location labels our UI already shows.

**When it runs.** The background sync loop now calls `maybe_auto_fetch` after every successful vehicle sync, which refreshes today and yesterday at most once per hour — two API calls per day in the steady state. A manual "Vom Fahrzeug-Server laden" / "Load from vehicle server" button in the trips toolbar pulls the last 30 days on demand, useful for first-time setup or after a gap; it stops early if the daily quota is about to run out so it can't lock the user out of the normal polling path.

**How the fallback works.** The existing polling-derived view still runs on any day the SDK didn't return data (or the brand doesn't support the endpoint — currently only Kia and Hyundai do). That keeps historical dates intact and means users on other brands see no regression. Days that have at least one SDK-sourced trip suppress the polled trips for the same day, so there are no duplicates.

**UI.** SDK-sourced trips get a small cloud-check icon next to the timestamp with a tooltip explaining the source. The trip edit button is suppressed for SDK trips whose end couldn't be matched to a parking event (the underlying parking-event edit flow doesn't apply). Everything else — the map, km/SoC/regen totals, CSV/GPX export — works unchanged.

**Why this solves the Hyundai-merges-trips complaint.** Kia and Hyundai use the same SDK, so the same code path serves both. The merged-trip appearance on the Hyundai install was caused by polling being disabled there combined with our polling-only trip derivation. With the SDK pull, the trip log becomes server-authoritative and matches what the Bluelink app shows, regardless of how often we poll for GPS.

## v2.23.2 (2026-04-17)

### Dashboard: all remaining charts are now click-to-enlarge

The vehicle-history tiles already supported a click-to-fullscreen flow in previous versions, but the six "stats" charts on the same page did not — clicking them did nothing. Those are now all expandable too:

- Weather correlation (kWh vs temperature per month)
- Monthly cost
- Cumulative cost
- Monthly kWh
- Monthly CO2
- Cumulative CO2 savings vs battery-production break-even

Implementation is a single helper `makeExpandableChart(canvasId, cfg, title)` that wraps every `new Chart(...)` call. It keeps a deep-cloned copy of the config (preserving callback functions via `typeof` sniffing, since `structuredClone` won't copy them), adds a fullscreen icon to the card header, wires a click + Enter/Space handler on the whole card, and lazily opens a shared Bootstrap fullscreen modal that spins up a fresh Chart.js instance from the cloned config. The main chart stays live underneath; the modal chart is destroyed on close.

One subtlety worth noting: the weather chart is built inside a `fetch().then()` callback, so by the time the callback runs the scripts block at the bottom of the page has already defined the helper — safe to reference by bare name. The five stats charts live in the same scripts block and see the helper directly.

A separate `#dashFullscreenModal` sits outside the `{% if vehicle_history %}` guard so it's available even on installs where the car-history row isn't rendered (no Kia/Hyundai API configured, insufficient samples, etc).

## v2.23.1 (2026-04-17)

### Settings sidebar: scroll-spy fixes

Two scroll-spy regressions reported immediately after v2.23.0 rolled out:

- **Clicking "HTTPS Security" jumped to "Notifications"** on Tailscale hosts, because the SSL card is `{% if not hide_ssl_card %}`-gated and isn't rendered at all — but the SSL entry in the sidebar nav was still there. Clicking the orphan link fell through to the next visible card. Fix: on page init, walk the nav links and hide every entry whose target id isn't in the DOM (or has zero height).
- **Clicking "System Updates" highlighted "Backup"** in the sidebar. The IntersectionObserver picked whichever card had the highest intersection ratio inside a narrow viewport band, which for short cards meant the *next* short card would beat the one just clicked. Fix: replaced IntersectionObserver with a deterministic "last card whose top has crossed the sticky-nav line" computation on scroll, and added a 700 ms spy-suppression window after a click so smooth-scroll can't be overruled mid-flight.

Also: the alternating tint stripe is now computed on the filtered (visible) card list rather than the raw list, so hiding the SSL card doesn't break the two-tone pattern below it. And the manual scroll now subtracts the live navbar + mobile-tab-bar height, so target cards land cleanly under the sticky header on both breakpoints instead of relying on CSS `scroll-margin-top` alone.

## v2.23.0 (2026-04-17)

### Settings page: sidebar navigation, scroll restore, alternating section tint

The settings page had grown to fifteen cards stacked in a single long column. Finding a specific section meant scrolling, and saving inside a section always bounced the user back to the top of the page. Both are fixed.

**Navigation.** On screens ≥ 992 px a sticky left-hand sidebar lists all fifteen sections (Language, Vehicle, THG, Locations, PV, API, ENTSOE, Operators, Database, SSL, Auth, Notifications, System Updates, Backup, About). Clicking an entry jumps to the section; as the user scrolls, the active entry highlights via IntersectionObserver. Below 992 px the sidebar collapses into a horizontally-scrolling tab bar pinned under the top navbar — no dropdown, no hamburger, just a swipeable strip.

**Scroll restore on save.** Every form inside a settings card now includes the section id on submit, either as a hash appended to the form action (for POSTs that render the page directly) or as a hidden `return_section` field (for the two handlers that redirect — `save_language` and the vehicle-API refresh flow). After the round trip, the browser lands back on the section the user was editing, not at the page top. Section ids are validated server-side against a simple `sec-<alphanum>` whitelist to keep the redirect URL safe.

**Alternating tint.** Every second card gets a subtle background tint (`--bs-tertiary-bg` in light mode, a hair-thin white overlay in dark mode), making the boundaries between sections obvious at a glance.

**Anchor-link compatibility.** Legacy anchors `#updaterCard` (dashboard's update banner) and `#thg` (the THG reminder banner) have been updated to the new scheme (`#sec-app` and `#sec-thg`). External bookmarks to the old names will no longer scroll to the card but will still land on the settings page — the fallback is graceful.

Reference layout borrowed from the Shelly Energy Analyzer project's settings page, adapted to Bootstrap 5 / Jinja instead of the JS-rendered schema used there.

**Deployment note.** This rolls out to the primary install only for initial testing. The other installs stay on v2.22.2 until feedback comes back; GitHub release and tag are held until the bundle grows.

## v2.22.2 (2026-04-17)

### Hotfix: Favorites invisible + add-button dead on Tailscale hosts

Users on Tailscale peers (all three installs, accessed via the internal VPN) reported existing favorites not rendering in Settings → Locations, and the "add favorite" flow doing nothing. Two separate-sounding symptoms, one root cause.

**Root cause:** The HTTPS/SSL controls card is intentionally hidden when the request comes from a Tailscale peer (the VPN already provides transport encryption, so the self-signed-cert UI is just clutter there). The SSL-setup JavaScript lives in an IIFE that is historically nested **inside** the Locations IIFE. When the SSL card is absent, every `document.getElementById('btnSslSave')` etc. returns null, and the first `addEventListener` call throws a TypeError. Because the SSL IIFE is nested, the throw escapes into the outer Locations IIFE and aborts it mid-flight — before `loadFavs()` is called and before the add-favorite click handler is wired up.

That's why existing favorites stay invisible (`loadFavs()` never runs, so the `<ul>` stays empty) and why clicking the add button does nothing (the listener was never attached).

**Fix:** Early-return guard at the top of the SSL IIFE:

```js
if (!document.getElementById('btnSslSave')) return;
```

One line. The SSL IIFE now exits cleanly when its card isn't present, the outer Locations IIFE continues, `loadFavs()` runs, the add-favorite handler is registered.

Hit all three production hosts in the same rollout. Verified by fetching the rendered `/settings` page on each and confirming the `Siedlung` + `Eltern` favorites Robert has configured now show up in the list markup.

---

## v2.22.1 (2026-04-17)

### Mobile fix: operator picker as a real dropdown

A user reported that the operator picker did not work on mobile — "it's not a dropdown there". The cause was the `<input list="operatorsList">` with an HTML `<datalist>`: on iOS Safari it did not render a dropdown at all, and on Android the behaviour was inconsistent.

**Fix:** On the charge form (`/input`) and the edit form (`/edit/<id>`), replaced it with a native `<select>` that uses the native picker on every mobile browser. The last option is labelled "Custom operator (free input)…" — if selected, a text field appears below for free input. A hidden input `name="operator"` is kept in sync by JS so the backend POST stays unchanged.

When editing a charge with a custom operator (not in the list), "Custom operator" is preselected and the text field is prefilled with the existing value.

Price autofill still hangs off the resolved operator value.

---

## v2.22.0 (2026-04-17)

### Trip log: all fields editable + map picker + favorites editing

Follow-up on v2.21.0 following user feedback: "all fields should be editable, start/stop time, km, SoC, regen. from/to selectable via the map. location favorites can't be edited".

**1. Trip editor rebuilt: two columns, all fields**

The old modal only edited the label/favorite name/address of a single stop. The new one edits the entire trip — both the start AND the destination stop in two columns side by side. Per side, editable:

- Label (Home/Work/Favorite/Other) + favorite name
- Address
- Arrival time + departure time (datetime-local)
- Odometer on arrival + on departure
- SoC % on arrival + on departure
- Coordinates (lat/lon) — type directly or pick via map

Derived values (trip km, SoC consumption, recuperation) are **not** edited separately — they are automatically recalculated from the saved odometer/SoC/time values. A hint text below the map explains this.

**2. Map picker in the modal**

A Leaflet map is now shown below the two columns. Both markers (blue = start, red = destination) are draggable. Each side also has a "Pick on map" button — clicking it puts the map into selection mode and the next map click sets the coordinates of that side. The modal calls `invalidateSize()` on `shown.bs.modal` so Leaflet does not render tiles at 0x0.

**3. Favorites editable in settings**

The list previously only had delete. Now there are three action buttons per favorite:

- **Rename** (pencil): opens an inline input with check/cancel, Enter = save, ESC = cancel. Clicking the name also opens the same edit mode.
- **New position** (pin): the map enters selection mode, the next click sets the new coordinates of this favorite.
- **Delete** (trash): unchanged.

### New endpoints / API changes

- `POST /api/parking_event/<id>` now additionally accepts `lat`, `lon`, `arrived_at`, `departed_at`, `odometer_arrived`, `odometer_departed`, `soc_arrived`, `soc_departed`. An empty string for `departed_at` clears the column (for "currently parked" entries). `arrived_at` is mandatory.
- `PUT /api/locations/favorites` — new endpoint: `{index, name?, lat?, lon?}` patches a single favorite. Missing keys are left alone (partial update).

### Server protection

The 7-day check (409 when `confirm_old` is missing) is unchanged — still applies to all POST modifications.

### No migration

No schema change. No breaking change for old clients — the existing POST fields (label/favorite_name/address) still work the same.

---

## v2.21.0 (2026-04-17)

### Four UX improvements following user feedback

**1. Log charge: cancel button + "My location" now comes from the car**

Previously the form had no way to discard entered values — you had to navigate back and ignore browser warnings. Now: an explicit Cancel button next to "Save" which optionally prompts (only if something has actually been entered) and also clears the local timer session.

The "My location" button previously pulled `navigator.geolocation` from the smartphone — which requires HTTPS and usually failed silently, and did not reflect the charging location anyway (phone is at home on the table, the charge happened at IONITY on the A4). Now the button fetches the **last GPS position reported by the car** from `vehicle_syncs` via a new endpoint `/api/vehicle/last_gps`. Works without HTTPS. The smartphone GPS is preserved as a secondary button, but the default is now the vehicle.

**2. Operators in settings: proper table instead of a textarea + price autofill**

Instead of "Custom operators as free text" there is now one row per operator (built-in + custom) with name and price per kWh. Prices are stored as JSON in `operator_prices`. In the log-charge form, the matching price automatically fills the `eur_per_kwh` field when the operator is selected — but only if the user has not yet touched the field themselves (prevents unwanted overwrite). Same logic in the edit form.

**3. Trip log: edit stops after the fact with a 7-day guard**

Each stop cell (from/to) is now clickable and opens a modal for editing the label (Home/Work/Favorite/Other), favorite name and address. Coordinates and timestamps stay immutable — they come from the car and changes would invalidate the derived km/SoC statistics.

Entries older than 7 days require an explicit confirmation checkbox ("Entry is older than 7 days — really change?"). The check also runs server-side in `/api/parking_event/<id>` — a hand-crafted modal call without confirmation gets a 409 back. Warns with the day count and a hint about retroactive effects on the statistics.

**4. Settings → Locations: clearer operation**

The Save buttons only had a disk icon without text — easy to overlook, which explains the "missing" save button in the user's report. Now: explicit "Save" on Home and Work buttons, separate "Pick on map" button, favorites button with hint text below (Name → Button → Map), and a numbered 3-step instruction at the top of the card.

### New/changed endpoints

- `GET /api/vehicle/last_gps` — last known GPS position from the car
- `GET/POST /api/parking_event/<id>` — view/edit a stop (with 7-day guard)
- Settings POST `action=save_operators` now accepts parallel arrays `op_name[]`/`op_price[]`/`op_builtin[]` instead of one textarea blob

### Data model

No schema change. New config keys are created lazily: `operator_prices` (JSON dict).

---

## v2.20.4 (2026-04-17)

### Hotfix: Dashboard crash for charges without a cost value

A production user reported "Internal Server Error" when opening the dashboard, could no longer get into the app.

**Cause:** In [services/stats_service.py:268](services/stats_service.py#L268) the monthly statistics computation for `cost_per_kwh` only checked whether `r.kwh > 0`, not whether `r.cost` is NULL. As soon as a charge with `kwh_loaded` but no `total_cost` is in the DB (typical for PV charges or a price not yet entered), the division blows up:

```
TypeError: unsupported operand type(s) for /: 'NoneType' and 'float'
```

The dashboard handler aggregates monthly statistics on render → every request → 500.

**Fix:** Check extended to `if r.cost and r.kwh and r.kwh > 0` — when the cost value is missing, `cost_per_kwh: 0` is returned, analogous to the existing `round(r.cost or 0, 2)` one line above.

No DB migration needed. No breaking change. A single line.

Affected: all users with at least one charge where `total_cost` is NULL and `kwh_loaded > 0`.

---

## v2.20.3 (2026-04-16)

### Vehicle history: real frames per plot, fullscreen works, map also appears in cached sync

Three follow-up bugs from v2.20.1/.2 addressed after the user reported "all plots need their own frame, location also not showing yet and zoom doesn't work".

#### 1. Click → fullscreen actually works now

Cause (deeper than the ID collision in v2.20.2): `bootstrap.bundle.min.js` is loaded in [templates/base.html:106](templates/base.html) **after** `{% block content %}`. But my inline script in the vehicle-history card ran **during** content parsing and called `new bootstrap.Modal(modalEl)` immediately — at that point `bootstrap` was not yet defined. ReferenceError → rest of the IIFE aborted → no click handler, no map.

Fix: The modal is now created **lazily** on the first click via a `getModal()` helper. By then `bootstrap` is long since available. The `hidden.bs.modal` listener is attached once at the same opportunity.

#### 2. Each plot now has its own frame

The 7 charts used to be bare `<div>`s with a label + canvas in a shared `.row`. No visual separation. Now a Jinja loop over a `vh_plots` tuple list wraps each plot in its own `<div class="card h-100 vh-chart-tile shadow-sm">` with:
- **Card header** (white, narrow) with title + fullscreen icon on the right
- **Card body** with the chart canvas
- `shadow-sm` and `h-100` so heights are uniform in the row
- Whole card is the click target (`role="button"`, `tabindex="0"`, `cursor:pointer`)

The location map gets the same card styling + the geo icon in the header.

#### 3. Map also appears in Kia/Hyundai "Cached" mode

The map card was previously gated on `vehicle_history.summary.last.lat` — i.e.: only if the **very last sync** delivered GPS. Under Kia/Hyundai Cached mode the API usually does not deliver GPS, so the map was permanently missing.

Now: a new server helper in the [app.py](app.py) `dashboard` route scans the `series.lat/lon` arrays backwards and takes the **last known** GPS point. The result ends up as a `vehicle_history_last_gps` dict in the template with `{lat, lon, stale, at}`. `stale=True` when the point was not the most recent sync row → the UI then shows "last known position" behind the location label.

The Leaflet asset include in the `<head>` is now also gated on this new variable instead of `summary.last.lat`, so the assets are loaded for Cached-mode users.

#### Side note: Jinja template trap

A comment in the JS contained the literal `{% block content %}` as an explanation, which Jinja mistakenly interpreted as a block open → `TemplateSyntaxError`. Reworded the text so the parser leaves it alone. Classic "didn't see that coming" trap with template engines.

#### Verification

- `py_compile` clean on app.py
- Dashboard renders with status 200, 63k chars
- Exactly 7 `card h-100 vh-chart-tile` frames in the HTML
- `function getModal()` in the bundle, **no** eager `new bootstrap.Modal` anymore
- Scenario A (latest sync has GPS): map renders normally, no "stale" badge
- Scenario B (only earlier sync has GPS, latest does not): map renders with last-known coordinates, "last known position" label in the header
- Scenario C (no GPS at all): map block simply gone, rest of page renders without crashing

## v2.20.2 (2026-04-16)

### Hotfix: Vehicle history — ID collision broke several plots simultaneously

In v2.20.1 I named the new period dropdown `<select id="vhRange">` — but the **range chart canvas** carries the same ID `<canvas id="vhRange">`. With two identical IDs, `document.getElementById('vhRange')` returns the **first** element in the DOM: the select (in the card header, comes before the canvas). Chart.js then calls `getContext('2d')` on a select element → TypeError → the rest of the IIFE does not run:

- Range chart is not drawn (that's the chart with the broken ID)
- All charts after Range are also not drawn (IIFE aborted)
- Location map is not drawn (initialized after the chart loop)
- Click-to-fullscreen handlers are never registered (done at the very end of the IIFE) → "Enlarge doesn't work"
- Dropdown change handler is also missing

That explains the three symptoms at once: "enlarge doesn't work · range is missing · location is missing · all plots somehow hang together".

**Fix:** Renamed the select to `vhRangeSel`. Pulled the JS reference to the dropdown location along. Canvas still keeps `id="vhRange"` as expected in `CHART_DEFS`, so the render loop stays unchanged.

Automatic verification in the dashboard HTML:
- `id="vhRange"` now appears exactly once (canvas)
- `id="vhRangeSel"` appears exactly once (select)
- JS contains `getElementById('vhRangeSel')`, no longer `getElementById('vhRange')` on the dropdown path
- All 7 chart IDs are in CHART_DEFS
- Modal + 7 clickable tiles in the DOM

## v2.20.1 (2026-04-16)

### Vehicle history: click → fullscreen + period selection

Two requested usability improvements to the vehicle history card on the dashboard:

**1. Each plot is clickable → opens a fullscreen modal.**
All 7 mini charts (SoC, range, odometer, 12V, SoH, recuperation, consumption) now have a fullscreen icon hint in the top right corner and are click- (and Enter/Space-) active. A click opens a Bootstrap `modal-fullscreen` with a larger version of the same chart: thicker line, more axis ticks (12 instead of 5), grid visible, data points as small circles, tooltips with Intersect-off mode for easy hovering. ESC / click outside closes.

**2. Period dropdown in the card header row.**
New choices: **24h · 7 days · 30 days · 90 days · 1 year · All**. On change, `/api/vehicle/history?days=N&persist=1` is called via AJAX, the charts are destroyed and redrawn with the new data — loading overlay with spinner during the request. The choice is persisted in `AppConfig` under `dash_history_days`, the next page load shows the same range directly.

**Default range is now 30 days** (previously "all"). For accounts with many months of data, 30 days is more readable; anyone who wants more clicks "1 year" or "All" in the card — that then sticks for future visits too.

**Technical:**
- [app.py](app.py) new route `/api/vehicle/history?days=N[&persist=1]` with clamp on 0..10 years, calls the existing `get_vehicle_history(days=...)` — no change to the stats service needed.
- Dashboard template refactored: chart configs now live in a `CHART_DEFS` list with `id`, `field`, `color`, `fmt`, `label`. The `renderAll(series)` function destroys old chart instances (`charts[id].destroy()`) and rebuilds them, so range switches are clean and have no memory leaks.
- Fullscreen modal reuses `buildChartConfig` with a `{fullscreen: true}` flag, which enables larger font sizes, grid and points. A second chart instance is drawn on `#vhFullscreenCanvas`; on modal close it is destroyed again. Chart.js needs a visible container to measure, so `setTimeout(..., 120)` after `modal.show()`.
- 8 new i18n keys in de + en (`dash.vh_range_title`, `dash.vh_range_{1d,7d,30d,90d,365d,all}`, `dash.vh_click_fullscreen`).

**Verified:**
- py_compile clean
- Dashboard renders with dropdown + modal + 7 clickable tiles
- `/api/vehicle/history?days={1,7,30,90,365,0}` delivers the correct shape for all values (`days`, `series`, `summary`), `series` has 11 expected fields
- Unknown `days=abc` clamped to 30
- `persist=1` updates `AppConfig['dash_history_days']`
- Server render selects the saved option correctly
- No data / empty range: API returns `series=null`, JS guarded with `if (data && data.series)`, no crash

## v2.20.0 (2026-04-16)

### Automatic rollback on broken updates + dashboard update banner

Two user-requested features that belong closely together: the update experience becomes more visible (anyone not checking settings sees the new version) and safer (broken updates don't leave the user in an app that won't start).

#### 1. Automatic rollback

**Problem:** If an update introduces a bug that prevents the app from starting (migration crashes, import error, missing dependency), the user was left with a dead app and no fallback. systemd tries to restart, crashes each time, gives up — the only remedy was SSH + manual git checkout.

**Solution:** A small state-machine guard that runs on every app boot (`services/update_service.py:pre_boot_rollback_check`).

**Flow:**
1. Before every update file-swap, a **backup of the files to be overwritten** is created in `updates/backup_pre_v<OLD>/`, plus an `UPDATE_PENDING.json` marker with old/new version, backup path and attempt counter.
2. On app boot, `pre_boot_rollback_check()` reads the marker. Three cases:
   - **No marker** → normal boot, nothing to do.
   - **Marker present, `attempts < 3`** → bump the counter, start a verification timer that deletes the marker after 60 seconds of successful uptime.
   - **Marker present, `attempts >= 3`** → rollback: swap the backup back, write `LAST_ROLLBACK.json`, `os._exit(0)` — supervisor restarts with the old code.
3. Second line of defense: after the restart, `updater_helper.py` watches port 7654 for 60 seconds for binding. If it doesn't → direct rollback without waiting for the boot counter.

**Why 3 attempts instead of 1?** Transient errors (port briefly occupied, race on sqlite open) should not falsely trigger a rollback. Only when **three** starts in a row fail before the 60s timer fires is the new version truly broken.

**Platform-agnostic:** The mechanism needs nothing but a supervisor that restarts on crash. Works under systemd (`Restart=always`), under macOS Terminal+nohup, under Windows (if anyone uses that).

**Data-safe:** `data/`, `venv/`, `.git/`, `logs/`, `updates/` are never touched by the backup/restore. The user's SQLite DB stays untouched no matter what happens.

**Manually confirmed with an end-to-end simulation:**
- Real Flask app copied into a temp dir
- Backup created, `app.py` replaced with a broken version where `create_app → RuntimeError`
- Three boot attempts each run into the exception, counter goes 1, 2, 3
- Fourth boot triggers rollback: `app.py` restored, marker gone, `LAST_ROLLBACK.json` written
- Afterwards the app boots successfully again. DB file size unchanged the whole time.

**Test suite (18 tests across 4 scenarios, all green before release):**
- `/tmp/test_rollback.py`: 10 tests of the decision logic (no marker, attempts 1→2→3, rollback fires, missing backup, corrupt JSON, read/clear API, backup pruning)
- `/tmp/test_helper_rollback.py`: 5 tests of the duplicated backup/restore paths in the helper + port watch
- `/tmp/test_e2e_rollback.py`: real Flask-app boot simulation with a deliberately broken `create_app`
- API + template smoke test: `/api/update/last-rollback` GET/DELETE, settings banner rendering

#### 2. Dashboard banner for available updates

At the top of the dashboard there are now two banners filled in via JS/AJAX after load:

- **Update banner** (yellow, refresh icon): appears when `/api/update/check` reports a new version. Shows "New version available v2.X.Y". **Click jumps directly to `/settings#updaterCard`** — the app-info card now has the anchor ID and the browser scrolls there automatically.
- **Rollback banner** (blue): only appears once if the app automatically reverted to the old version on the last boot. Explains from/to which version. Dismissible with "X" button — `DELETE /api/update/last-rollback` clears the `LAST_ROLLBACK.json`.

The update-check response is cached in `sessionStorage` for 30 minutes so clicking between pages does not call `/api/update/check` on every page load.

**Settings page** additionally gets:
- Anchor ID `updaterCard` for the deep link from the banner
- Permanent hint under the buttons: "Before every update, a backup is created. If the new version doesn't come up, it automatically reverts to the previous one."
- Info box with details if a `LAST_ROLLBACK.json` exists

#### Technical

- New file [services/update_service.py](services/update_service.py) — single source of truth for the decision logic (`MAX_ATTEMPTS=3`, `VERIFICATION_DELAY_S=60`).
- [updater.py](updater.py) `_inline_swap` (systemd path): before the file swap, `create_pre_update_backup()` + `write_pending_marker()` are called.
- [updater_helper.py](updater_helper.py) (non-systemd path): duplicated stdlib-only implementation of the same backup/restore paths plus port watch after restart. Duplication intentional — the helper must run even if the venv is broken.
- Pre-boot check as the first statement in [app.py:create_app()](app.py) — BEFORE `db.create_all()`, because a broken migration is exactly the kind of thing that should trigger a rollback.
- At most 3 backups are kept in parallel (oldest pruned by mtime).
- 5 new i18n keys (de + en): `dash.update_available_title/hint`, `dash.rollback_title`, `set.app_last_rollback_title`, `set.app_rollback_safety_hint`.

**Important note about the first rollout:** This release v2.20.0 brings the protection mechanism. That means concretely: **the update FROM v2.19.x TO v2.20.0 is not yet protected by rollback** (the old v2.19.x updater does not know about the backup step yet). From v2.20.0 → v2.20.1 onwards the mechanism kicks in automatically on every update.

## v2.19.2 (2026-04-16)

### CSV import preview: see what happens before it happens

Previously the import was a leap into the unknown: upload, choose mode, click — and hope that the columns were recognized correctly and nothing important went wrong. Now there is a preview step.

**Workflow:**
1. User chooses CSV + mode → clicks **Preview** (no longer direct Import)
2. Browser fetches `POST /api/import/preview` via AJAX — the backend analyses the file without importing it and returns structured JSON
3. UI renders:
   - **Info row**: detected delimiter, whether a header was detected, how many rows are already in the DB
   - **Column table**: each CSV column with index, header name, auto-detected mapping (as a dropdown, **changeable**), and a sample value
   - **Missing app fields**: which of our logical fields (e.g. operator, charge location) the CSV does not contain — these remain empty
   - **Summary**: total rows / new / updated / duplicates / empty / errors
   - **Sample rows** (first 20) with an action badge per row (`new`, `update`, `duplicate`, `empty`, `error`)
   - **Error list** with row numbers, if any occurred
4. User can **change the column mapping via dropdown** if auto-detection mapped a column incorrectly. The overrides end up in a hidden `column_override` form field.
5. Satisfied → **Import** button (the previous one) — the POST carries the `column_override` along and applies it during the real import.

**Architecture:**
- Refactor in [import_gsheet.py](import_gsheet.py): shared helpers `_analyze_csv`, `_parse_one_row`, `_classify_row`. Both `preview_csv_data()` (new function, DB-read-only) and `import_csv_data()` (commit path) go through the same code path, so the preview is guaranteed to show the same result the import will later produce.
- New route endpoint `/api/import/preview` (POST multipart form) → JSON with columns, samples, summary, errors.
- `import_csv_data(column_override=...)` now optionally accepts an override dict `{logical_field: col_index}` that patches auto-detection. `null` unmaps a field.
- The POST `action=import_csv` in the settings handler also parses `column_override` as JSON from the form and passes it through.
- Settings template gets JS that AJAX-fetches the preview, renders a complete UI (tables + badges + dropdowns), and on dropdown change writes a correct `column_override` JSON back into the hidden field.
- 49 new i18n keys in de + en (preview labels, action labels, and user-friendly field labels like "Operator" instead of `operator`).

**Verification:**
- Preview unit test with 3-row CSV (2 valid, 1 bad date) → summary correct, samples correct, unmapped CSV columns detected
- Column-override unit test: CSV with "My Special Field" header, auto = None → override to `operator` → correctly parsed as operator
- End-to-end API test: `POST /api/import/preview` returns valid JSON with all fields
- End-to-end import test: upload with `column_override='{"date":0,"operator":1,"kwh_loaded":2}'` through `/settings` → data correctly saved with mapped operator
- Regression: existing import without override keeps working (dedup kicks in, import counts correct)

## v2.19.1 (2026-04-16)

### Fix: Vehicle sync crash "Object of type DailyDrivingStats is not JSON serializable"

The enriched `raw_data` dump from v2.19.0 could not serialize the Kia/Hyundai `daily_stats` field (a list of `DailyDrivingStats` objects from the SDK).

**Root cause**: My introspection check in [connector_hyundai_kia.py:_dump_vehicle](services/vehicle/connector_hyundai_kia.py) tested serializability with `json.dumps(val, default=str)` — but `default=str` silently stringifies **every** unknown object and the check always passed. What got saved was then the original object; only when `json.dumps(raw_data)` was later called without the default argument did it blow up.

**Fix**: The introspection now does a real round-trip `json.loads(json.dumps(val, default=str))` — which produces a truly JSON-safe copy (nested `DailyDrivingStats` → string repr). As a belt, all five call sites of `json.dumps(raw_data)` (in `services/vehicle/sync_service.py` and four places in `app.py`) additionally get `default=str`, so a future regression doesn't crash the sync again.

Regression-tested with a mock vehicle that carries `daily_stats = [DailyDrivingStats(...), …]` plus a nested dict with objects in it → dump now fully JSON-safe, no more exceptions.

## v2.19.0 (2026-04-16)

### Big update: CSV import data safety, raw-data viewer, complete edit page, operator dropdown

Production release — every feature was verified with a Python syntax check, migration test, template smoke test and end-to-end unit tests for the import logic before tagging.

#### 1. CSV import is now safe and tolerant

The old importer [import_gsheet.py](import_gsheet.py) was **position-based** and had only one mode (`replace=True` → `DELETE FROM charges`, then re-insert). That meant: **charges manually added after the fact were simply deleted on re-import**. For a production tool users actively rely on, that's a data loss risk.

New:

- **Columns are detected by header**, not by position. Fuzzy matching (case-insensitive, typos via `SequenceMatcher` ratio ≥ 0.82) against an alias table: `Datum` / `date` / `tag` / `day` all hit the same logic, likewise `EUR/kWh` / `€/kWh` / `preis`, `Uhrzeit` / `zeit` / `hour`, `Anbieter` / `provider` / `cpo` / `betreiber`, etc.
- **Delimiter is autodetected**: semicolon (our own export), comma (Google Sheet), tab, pipe. The character with the highest median column count across the first 5 rows wins.
- **Date format is autodetected**: `YYYY-MM-DD`, `DD.MM.YYYY`, `MM/DD/YYYY` (Google Sheet US), and as fallback `DD/MM/YYYY`.
- **Legacy fallback**: if no header row is found (Google Sheet export starts directly with date rows), the old position-based mapping runs.
- **Four import modes** instead of one boolean:
  - `skip` (default) — rows with the same `(date, charge_hour, kwh_loaded)` as existing entries are skipped. **Manual data is never overwritten.**
  - `update` — no duplicate is created, but if the CSV has fields that are empty in the existing entry (e.g. operator or location), they are filled in. Existing values are left untouched.
  - `append` — everything is inserted, including exact duplicates. For advanced users.
  - `replace` — the old nuclear behaviour. Now shows a red warning box with a mandatory checkbox (`replace_confirm`). Before the delete, **a DB backup is automatically written** to `data/backups/pre_import_YYYYMMDD_HHMMSS.db`; the last 5 backups are retained.
- **Export is lossless again**: the own CSV export (`/api/export/csv`) now contains all fields including time, odometer, operator, location, lat/lon. A round trip (export → re-import in skip mode) leads to 0 new entries and 0 errors — tested with the live DB (474 entries).

#### 2. "Edit entry" finally shows all fields

The old [edit.html](templates/edit.html) was incomplete — neither location nor operator were shown, and the backend POST handler in [app.py:edit_charge](app.py) ignored the fields even if they were set in the input form (data was lost on every edit).

- All location fields (name, lat, lon) are now on the edit page
- **Inline Leaflet map picker** (like in settings): "Map" button unfolds an OSM map, click sets the marker position, marker is draggable, coordinates are written live into the lat/lon fields
- Quick buttons "Home"/"Work" take the coordinates from settings
- Backend saves `location_name`, `location_lat`, `location_lon`, `operator`

#### 3. Operator field (CPO) on every charge

New column `operator` (VARCHAR(64), nullable) on the `charges` table — migration runs automatically on first start after update via `ALTER TABLE`, idempotent.

- Input form and edit page have a `<datalist>` field with 19 built-in operators (IONITY, EnBW mobility+, Aral pulse, Tesla Supercharger, Shell Recharge, Allego, Fastned, Elli (VW), EWE Go, Maingau, Lidl, Kaufland, Aldi Süd, REWE, Mer, Stadtwerke, Home / private, Work, Other). The user can type freely or choose from the list.
- New settings card **"Charging station operators"** with a textarea for custom entries (newline- or comma-separated). Custom entries appear in addition to the built-in list. Stored as JSON under AppConfig key `custom_operators`.
- History table shows operator + location as a small second line under the date (`…` if too long, full text in tooltip).

#### 4. Raw-data viewer for all vehicle brands

New routes `/vehicle/raw` (list of all syncs) and `/vehicle/raw/<id>` (details of one sync row). Reachable via a "View raw data" button in the vehicle API card as soon as a brand is configured.

- Detail page shows **all normalized fields** (as stored in the DB) above and **the complete raw API dump** as pretty-printed JSON with a scroll container + copy-to-clipboard button.
- `raw_data` of the Kia/Hyundai and VAG connectors was expanded from `{'vin': …}` to **full introspection**: all public, JSON-serializable attributes of the Vehicle object end up in the dump (primitive types directly, `datetime` → ISO string, nested objects stringified with a length cap). That's what makes the viewer useful in the first place.
- **SoH banner for Kia/Hyundai**: if `battery_soh_percent > 100` is in a snapshot, a blue info box appears explaining why — the API measures against the factory-released usable capacity, while the battery still has physical reserve. New batteries typically show 110–125 %, the value drops with ageing towards 100 %.

#### 5. Migrations & backwards compatibility

- `charges.operator` is idempotently added via `ALTER TABLE ADD COLUMN operator VARCHAR(64)` on the first start with v2.19.0. Existing rows have `operator = NULL`, which is handled consistently everywhere (history shows nothing, CSV export writes empty, edit page renders as an empty field).
- The old `replace=True` parameter to `import_csv_data()` still works (maps to `mode='replace'`), so existing callers don't break.
- i18n: 62 new keys in `de.json` and `en.json` — both files have identical key sets.

#### Verification

Before commit:

- `python -m py_compile` on all changed Python files — clean
- CSV import unit tests with 6 format variants (semicolon+header, re-import dedup, update mode manual-protection, legacy comma-no-header, ISO-date+tab, replace-with-backup) — all green
- App boot test: migration runs, `charges.operator` exists, all core routes return 200
- Edit POST round-trip: operator + location are persisted
- Raw viewer: handles invalid/null raw_json gracefully
- CSV round-trip: own export (474 rows) → re-import in skip mode → 0 new, 0 errors

## v2.18.3 (2026-04-16)

### Fix: Hyundai Selenium waited for a non-existent button even though the login chain was already through

Screenshots from the user showed the cause of the 5-min timeout: **The browser lands on Hyundai directly on the final URL** `prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token?code=...&state=ccsp&login_success=y` — that is, with a valid CCSP code in the address bar. But Selenium was waiting for the CSS selector `button.mail_check, button.ctb_button`, which does not exist at all on this JSON response page. Result: 300 seconds of idling despite successfully obtained code.

The research agent verified against the upstream script [`hyundai_kia_connect_api/Hyundai Token Solution/hyundai_token.py`](https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/tree/master/Hyundai%20Token%20Solution): the selectors `button.mail_check`/`button.ctb_button` **are not in the upstream code**. Upstream uses a terminal `input("Press ENTER after login is complete...")` gate, no Selenium wait. So the selectors were a homegrown construction that searched on a page the browser had long since left.

Fix: Instead of waiting for a CSS selector, we now wait on the **URL pattern** — either the browser lands directly on the `redirect_final` host (Hyundai: auto-chain via session cookies) or on the `login_redirect` host (Kia: intermediate landing on kia.com, after which step 2 is needed). The logic distinguishes the two cases and jumps directly to code extraction when the chain is already through, or performs the second `driver.get()` otherwise. Kia's `a[class='logout user']` selector stays as an additional trigger for the oneid flow.

Second fix in the same pass: the manual paste-URL validation rejected URLs with `login_success=y` as "step 1" — but `login_success=y` also appears in the final `prd.eu-ccapi` URL. Now the distinction is made only by **host** (`ctbapi.hyundai-europe.com` = step 1, otherwise accepted).

## v2.18.2 (2026-04-16)

### Fix: Step-2 URL with unencoded redirect_uri triggers 400 Bad Request

Clicking the Step-2 link in the manual instructions produced "400 Bad Request — Invalid request". Cause: the `redirect_uri` query parameter contained an unencoded `https://...:8080/api/v1/user/oauth2/token`. If you send the URL through Selenium's `driver.get()`, Chromium normalizes it automatically — but on an `<a href>` click from the UI, the browser sends the URL raw, and Hyundai's OAuth server is strict enough to reject the request.

Fix: `get_manual_step2_url()` now uses `urllib.parse.quote(cfg['redirect_final'], safe='')` for the `redirect_uri` value. The same URL is used in the Selenium path (`_do_fetch`) too — previously this path had a separate builder, which was risky for future changes. Now one builder, one source of truth.

## v2.18.1 (2026-04-16)

### Manual token flow: 3-step instructions with clickable step links

The user pasted the **ctbapi URL** in the manual paste flow (Stage 1, the login result with `?code=...&login_success=y`). The code there is issued for `peuhyundaiidm-ctb` — the token endpoint rightfully says "code is not exist in redis" because it is not known for the API client `6d477c38-...`. But that message doesn't help the user at all.

Two improvements:

**1. ctbapi URL is explicitly intercepted.** `exchange_manual_url()` now checks `'ctbapi.hyundai-europe.com' in url` or `'login_success=y' in url` and returns a clear message: "This is the login URL (stage 1), not the final token URL (stage 2). Next step: open this URL in the same browser...".

**2. UI shows 3-step instructions with clickable links.** When the user expands the "Manual" details, the page loads via `GET /api/vehicle/token/manual/step_urls?brand=...` the two step URLs:
- **Step 1**: login URL → user opens in their own browser, logs in
- **Step 2**: CCSP authorize URL → user opens *in the same browser*. Due to the IdP session cookie from step 1, this URL automatically redirects via 302 to the final URL with `?code=Y` (the correct CCSP code)
- **Step 3**: user copies the final URL from the address bar and pastes it in

Both links are directly clickable (`target="_blank"`), the placeholder in the paste field is dynamically set to `prd.eu-ccapi.hyundai.com:8080/.../token?code=...` (Hyundai) or `.../redirect?code=...` (Kia).

## v2.18.0 (2026-04-16)

### Manual URL paste as fallback for Kia/Hyundai token + better InvalidSessionId message

Two topics:

**1. InvalidSessionIdException handling.** If the user closes the browser window in noVNC during the Selenium flow (or Chromium crashes), Selenium throws `InvalidSessionIdException` with a long stack trace. Previously the raw stack trace ended up in the UI. Now: specific detection of the error plus friendly message "Browser session ended. Please do not close the browser window while the token is being fetched."

**2. Manual paste fallback.** If Selenium crashes, hangs or gets killed by the user for any reason, previously the whole process had to be restarted. New: under the "Fetch token" button there is a collapsible `<details>` element "Manual fallback: paste URL with code". Workflow:
1. User opens Kia/Hyundai login in their own browser (Mac/iPhone, wherever)
2. Logs in, lets the flow run through, lands on the URL with `?code=...` (for Hyundai: `prd.eu-ccapi.hyundai.com:8080/.../oauth2/token?code=...`)
3. Copies the URL from the address bar
4. Pastes it into the new text field in the app, clicks "Fetch token from URL"
5. App extracts the code via regex, POSTs to the token endpoint, saves the refresh token in the password field

Completely independent from the Selenium path, also works when Chromium/noVNC are down, ARM hosts where ChromeDriver has issues, etc. New route `POST /api/vehicle/token/manual`, new function `exchange_manual_url()` in `token_fetch.py`. 3 translation keys per language.

## v2.17.7 (2026-04-16)

### Hyundai token fetch: missing 2nd authorize step (final fix)

After verbatim comparison with two working upstream scripts (`Hyundai%20Token%20Solution/hyundai_token.py` by the library authors and `RustyDust/bluelinktoken.py`) it was clear: the CTB flow has **two authorize steps**, just like Kia. My code never did the second step.

**The actual flow:**
1. User logs in via `login_client_id=peuhyundaiidm-ctb` → browser lands on `ctbapi.hyundai-europe.com/api/auth?code=X`. `button.mail_check` / `button.ctb_button` appears — that's where the browser stops.
2. The script must **programmatically** navigate to a second authorize URL: `idpconnect-eu.hyundai.com/.../authorize?response_type=code&client_id=6d477c38-...&redirect_uri=prd.eu-ccapi.hyundai.com:8080/.../oauth2/token&state=ccsp`. Thanks to the IdP session cookie from step 1, this URL immediately 302-redirects to the final URL with the CCSP code Y.
3. Extract code Y, exchange for token.

In v2.17.2 I mistakenly replaced the CSS selector wait with a URL wait on prd.eu-ccapi — but the browser never navigates there by itself, hence the "permanent hang". Now: CSS wait → driver.get(redirect_url) → 15-second poll on URL match. The entire CTB special case is gone, Kia and Hyundai now run through the same code path.

## v2.17.6 (2026-04-16)

### Fix: Hyundai token fetch — wait for CCSP code, not for ctbapi code

Revert of v2.17.5 plus root cause. The Hyundai CTB flow has **two codes** in the redirect chain:
1. `ctbapi.hyundai-europe.com/api/auth?code=X` — code for `client_id=peuhyundaiidm-ctb` (the login client). This code does NOT belong to the token POST.
2. Then a server redirect to `prd.eu-ccapi.hyundai.com:8080/.../oauth2/token?code=Y` — Y is the CCSP code for `client_id=6d477c38-...` (the API client). This is the code the token endpoint expects.

In v2.17.4 I loosened the URL check to "contains `code=`" — Selenium consequently grabbed code X from ctbapi. My v2.17.5 attempt with `redirect_uri=ctbapi` on the token POST failed because the API client does not have ctbapi registered as a redirect at all (→ "Invalid redirect uri").

Correct fix:
1. Wait condition reverted to **URL contains `prd.eu-ccapi.hyundai.com` AND `code=`**. So Selenium waits for the second redirect and gets the correct CCSP code Y.
2. Token POST again uses **`redirect_uri=redirect_final`** (matching the URL against which the CCSP code was issued). v2.17.5 branching rolled back.
3. Error message on wait timeout now explicitly shows which URL was reached, so in a log case we can immediately see whether it got stuck on a third redirect host.

Kia (oneid, 2-step authorize) unchanged.

## v2.17.5 (2026-04-16)

### Fix: Hyundai token POST uses wrong `redirect_uri`

Hyundai token endpoint returned 400 with `"Mismatched token redirect uri. authorize: https://ctbapi.hyundai-europe.com/api/auth token: https://prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token"`. OAuth2 requires that the `redirect_uri` parameter on token exchange is **exactly** the same as on the preceding authorize request.

My code blindly used `cfg['redirect_final']` for the POST — that's correct for Kia (whose second authorize step actually runs with `redirect_final` as redirect_uri), but not for Hyundai CTB. Hyundai has only **one** authorize step with `redirect_uri=login_redirect` (`ctbapi.hyundai-europe.com/api/auth`). The browser subsequently lands on `prd.eu-ccapi.hyundai.com:8080/.../oauth2/token?code=...` (that's the CTB display URL), but the code was issued by idpconnect against `ctbapi...`.

Fix: on the token POST, decide per flow — `ctb` → `login_redirect`, `oneid` (Kia) → `redirect_final`. Kia stays byte-for-byte the same.

## v2.17.4 (2026-04-16)

### Hyundai token fetch: URL match more robust + better error messages

Two fixes in one release:

**1. URL match relaxed.** In v2.17.2 the wait required the final URL to start with `https://prd.eu-ccapi.hyundai.com` **and** contain `code=`. In practice the browser sometimes lands on `ctbapi.hyundai-europe.com/api/auth?code=XXX` instead of directly on prd.eu-ccapi depending on the flow variant — my match did not accept that and ran until the 5-minute timeout. Now it suffices: URL contains `code=`, no matter on which host.

**2. Empty error messages resolved.** The user reported a red "message:" display without further text next to the token button — that was either a Selenium `TimeoutException` with an empty message, or a swallowed exception body. All error paths in the token fetch flow now explicitly return `{type}: {message}`, plus context (last URL on timeout, HTTP body on token POST error, etc.). On a completely empty `str(e)` the code falls back to the exception type name. Additionally, the complete traceback is logged on the server (`journalctl -u ev-tracker.service`) so server-side diagnosis is also possible.

Kia path unchanged.

## v2.17.3 (2026-04-16)

### Fix: VAG connector — import path for the CarConnectivity class

In `carconnectivity >= 0.11` the `CarConnectivity` class is no longer in the top-level package but in the submodule `carconnectivity.carconnectivity`. The old import `carconnectivity.CarConnectivity(...)` threw: `module 'carconnectivity' has no attribute 'CarConnectivity'` — which only became visible at all with v2.17.1 (the error-surfacing fix); in v2.17.0 and earlier, the generic "check username and password" flash hid the actual error.

Fix: Import with fallback — first try the new submodule, then the old top-level import. Works on both library versions.

## v2.17.2 (2026-04-16)

### Fix: Hyundai token fetch hangs in Selenium wait

v2.17.0 waited for `button.mail_check` or `button.ctb_button` as the "login detected" condition for Hyundai — selectors from the RustyDust script, but they sit on an intermediate confirmation page that Hyundai apparently **skips** in some flows. The browser lands directly on `prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token?code=XXX` and shows the JSON body `{"result":"E","data":null,"message":"url is not defined"}` — which is, incidentally, **not an error** but the expected end state (the server strips the `code` query param when rendering). But Selenium kept waiting for buttons that never come, and ran into the timeout after 5 min.

Fix: per-flow logic in `_do_fetch()`. For the CTB flow (Hyundai), don't wait on DOM elements but on the URL change — as soon as `driver.current_url` starts with `prd.eu-ccapi.hyundai.com` and contains `code=`, the login is through. Selenium extracts directly from the URL and skips the separate `driver.get(redirect_url)` step (which Hyundai already performs itself in the CTB flow). The Kia oneid flow stays 1:1 as before: CSS wait on `a.logout.user`, then manual navigation to the CCSP authorize endpoint.

## v2.17.1 (2026-04-16)

### Fix: VAG (VW/Skoda/Seat/Cupra/Audi) shows the real error instead of generic "check password"

VW Group's identity server (`identity.vwgroup.io`) regularly — after password changes, T&C updates or new data protection provisions — requires **renewed acceptance** by the user. The CarConnectivity library throws in that case an exception with the exact URL to accept (`Try visiting: https://identity.vwgroup.io/...`). Previously `VAGConnector.test_connection()` silently discarded this exception with `except Exception: return False` and the app flashed the generic "Connection failed. Check credentials." — whereupon every user naturally thought the username/password was wrong, which then triggered login throttling on testing.

Fix: `test_connection()` no longer catches the exception, lets it propagate to the app route, which outputs it with `flash.error` in the flash message — including the consent URL. `authenticate()` (used for the background sync) stays defensive and now additionally saves `self._last_error` as a hint for log analysis.

**User-side**: if the error comes up again, the flash message now contains the URL the user needs to open in a browser, log in and click the consent. Then the Skoda/VW/Audi/Seat/Cupra connection works again.

## v2.17.0 (2026-04-15)

### Hyundai refresh token: correct OAuth URLs (CTB flow)

The "Fetch token" button now works for Hyundai EU too. Background: in v2.16.0 and earlier, `services/vehicle/token_fetch.py` simply copied the Kia configuration for Hyundai and only swapped the domain — which could never work because Kia and Hyundai EU use **completely different OAuth flows** even though they both belong to the same parent company and build on the same `hyundai_kia_connect_api` library.

**The differences:**

| Field | Kia EU (oneid) | Hyundai EU (CTB) |
|---|---|---|
| Flow | oneid/online-sales on kia.com | CTB (Connected Car Telematics Business) on ctbapi.hyundai-europe.com |
| `login_client_id` | `peukiaidm-online-sales` | `peuhyundaiidm-ctb` |
| `login_redirect` | `www.kia.com/api/bin/oneid/login` | `ctbapi.hyundai-europe.com/api/auth` |
| `state` | Base64-URL + `_default` suffix | Short country code + `_` (e.g. `EN_`) |
| `redirect_final` | `.../oauth2/redirect` | `.../oauth2/token` |
| `client_secret` | Literal string `"secret"` | Real 48-character key `KUy49Xx...` |
| User-Agent | Mobile Android | Desktop Chrome |
| Extra authorize params | none | `connector_client_id`, `captcha=1`, `ui_locales`, `nonce` |

The old config had six out of seven fields wrong — only `client_id` was correct. The token exchange also always failed on the hard-coded `client_secret: 'secret'` because Hyundai's endpoint returns 401 on a wrong secret.

**Fix:**

- `services/vehicle/token_fetch.py` — `BRAND_CONFIG['hyundai']` completely replaced, `BRAND_CONFIG['kia']` explicitly given `client_secret: 'secret'` (previously hard-coded, now consistent). New field `user_agent` per brand (mobile for Kia, desktop for Hyundai, both retain the `_CCS_APP_AOS` suffix that bypasses the "use the app" block). New field `flow` per brand as discriminator. New helper function `_build_login_url(cfg)` builds the login URL per flow — CTB needs `connector_client_id`, `captcha=1`, `ui_locales` etc., which the Kia oneid flow doesn't know at all. The token exchange POST now pulls `cfg.get('client_secret', 'secret')` instead of the hardcoding.
- `services/vehicle/connector_hyundai_kia.py` — docstring updated, both connectors (Kia + Hyundai) again share the refresh token flow, but still have separate `credential_fields()` overrides for clean labels.
- `templates/settings.html` — `updateVehicleFields()` back to `isKiaHyundai` for the token-hint section and the refresh-token label. Hyundai users see the "Fetch token" button again (it was mistakenly hidden in v2.16.2, because at the time I thought Hyundai worked with password login).

**Sources**: two independent working scripts from the hyundai_kia_connect_api community (Hyundai Token Solution subfolder in the upstream repo + RustyDust/bluelink_refresh_token) confirm all values identically. Plus the library source itself (`KiaUvoApiEU.py`) with `CCSP_SERVICE_ID` and `CCS_SERVICE_SECRET` as runtime constants — they are validated on every subsequent API call, so guaranteed to be current.

The Kia flow stays **1:1 unchanged** except for the extraction of `client_secret` into the config — the working path is not touched.

## v2.16.2 (2026-04-15)

### Hyundai: login with password + PIN instead of refresh token

Until now, the app required a refresh token for both Kia and Hyundai (both ran through `CREDENTIAL_FIELDS` with the label "Refresh token"). For **Kia EU** that has been mandatory since 2025 because reCAPTCHA blocks direct password login, but for **Hyundai EU** the classic flow with email + password + 4-digit PIN still works.

Fix: split credential_fields per brand.

- `services/vehicle/connector_hyundai_kia.py` — two separate lists: `KIA_CREDENTIAL_FIELDS` (refresh token, help text refers to the token fetch button) and `HYUNDAI_CREDENTIAL_FIELDS` (normal password field). Both connector classes override `credential_fields()` with their own list.
- `templates/settings.html` — frontend logic in `updateVehicleFields()` splits `isKiaHyundai` into `isKia` and `isHyundai`. Token-hint section and "Refresh token" label now only for Kia (and Tesla) — Hyundai shows a normal "Password" field, no "Fetch token" button.
- Kia flow stays **exactly** as it is (untouched, works).

If Hyundai EU enables reCAPTCHA at some point too, this will surface with an auth attempt error and we'll have to push Hyundai into the token flow. For now user + password is enough.

## v2.16.1 (2026-04-15)

### Fix: /api/system/updates/status crashes on permission-denied on UU log

`/var/log/unattended-upgrades/unattended-upgrades.log` is by default `root:adm` with mode 640 — the ev-tracker user cannot read it. In v2.16.0 my code caught `PermissionError` only on `open()`, but not on the preceding `.is_file()` on the Path object (which also blows up on a 640 directory). Result: 500 on the status route, the card stayed at "Status is being loaded…".

Fix: `.is_file()` removed entirely, instead direct `open()` with a broad `except (FileNotFoundError, PermissionError, OSError)`. If the log is unreadable, the card just shows "never" as the last run — that's not an error, because the `pending_count` from the dry-run delivers the current info anyway.

## v2.16.0 (2026-04-15)

### System updates (Debian security-only) in the settings menu

New settings card "System updates (security)" between notifications and backup. Debian security updates can now be manually checked, installed from the browser, and a required reboot can be triggered — with minimal attack surface.

**Design decision: strict security-only.** No full apt access from the web UI. Reason: whoever cracks the web login would otherwise effectively get root on the OS (apt can install arbitrary packages + run post-install scripts as root). Instead, on the VM the Debian standard tool `unattended-upgrades` is set up, which pulls exclusively from `${distro_id}:${distro_codename}-security`. The sudoers rule allows the ev-tracker user exactly **one** command: `/usr/bin/unattended-upgrade -v`. An attacker with web login can at best trigger a security patch run — not install any package of their choice.

**New features:**

- Card shows: number of available security patches, date of last automatic run, "reboot required" warning banner when `/var/run/reboot-required` is present
- "Install security updates now" button starts `unattended-upgrade -v` in a background thread. The UI polls the status every 2.5 s and shows the log live.
- "Restart now" button only appears when a reboot is needed, with double confirmation (user has to re-enter the LUKS passphrase after boot)
- Unattended-upgrades continues to run normally on its own via Debian's `apt-daily.timer` and `apt-daily-upgrade.timer` — the UI is only the manual override plus status display

**Technical:**

- `services/system_update_service.py` — encapsulates reading the UU log (`/var/log/unattended-upgrades/unattended-upgrades.log`), counting pending updates (via `unattended-upgrade --dry-run -v`), the background thread runner for apply, and the reboot scheduler. State lives in a thread-safe module dict, no DB entry needed.
- `app.py` — new routes: `GET /api/system/updates/status`, `POST /api/system/updates/apply`, `POST /api/system/reboot`. All three behind the auth guard.
- `templates/settings.html` — new card plus a separate `<script>` block (following the same pattern as the notify card in v2.15.2, so a JS error higher up doesn't kill the sysupd handlers)
- **19 new translation keys** (`set.sysupd_*`) in all 6 languages

**Setup on the VMs (paste block as root):**

- `apt install -y unattended-upgrades` if missing
- Enable `/etc/apt/apt.conf.d/20auto-upgrades` (`APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";`)
- Check `/etc/apt/apt.conf.d/50unattended-upgrades`: `${distro_id}:${distro_codename}-security` must be active, other origins must stay commented
- Add sudoers lines: `/usr/bin/unattended-upgrade -v`, `/usr/bin/unattended-upgrade --dry-run *`, `/sbin/shutdown -r now`

## v2.15.2 (2026-04-15)

### Fix: Notify card handlers no longer ran at all

v2.15.1 removed the `<form>` and switched the save button to `type="button"`. That prevented the reload, but now **nothing** happened on click — the button did not respond. Safari console confirmed: button element exists, but the click handler was not attached. That means: the IIFE did not run through to the `addEventListener`.

Cause likely: in the large `<script>` block of `settings.html`, Leaflet, location map and various forms run higher up. An error somewhere earlier blocked the parse of the notify IIFE in Safari. Backup form happened to still be OK (maybe a different code path), notify not.

Fix: the notify handler was pulled completely out of the large script and now runs in **its own `<script>` block at the end of the page**. No IIFE pyramiding, no Promise-based `.then()` instead of `async/await` (in case Safari has an edge case there), explicit `credentials: 'same-origin'` in the fetch calls, plus console.log at strategic points (`[notify] init start`, `[notify] handlers attached`, `[notify] save click`) so at the next problem, one immediately sees in the console what's happening.

## v2.15.1 (2026-04-15)

### Fix: Notifications card did not save

In v2.15.0 the notifications card was built as a real `<form>` element with a `<button type="submit">`. For reasons not yet understood, the JS submit handler did not fire in Safari (probably an ordering problem with a preceding IIFE in the same `<script>` block which in certain cases aborts further parsing). Effect: on click on "Save", the browser did a native form submit (GET without body), the page reloaded, the fields were empty again — even though the backend code and routes worked fine (verified directly via fetch from the DevTools console: POST and GET return `{ok:true, ...}`).

Fix is pragmatic rather than surgical: `<form>` → `<div>`, `<button type="submit">` → `<button type="button">` with a direct click handler. No more form-submit event = no possible reload, no matter what else happens in the script. Functionally identical, just without the hidden reload trap.

## v2.15.0 (2026-04-15)

### Push notification on VM restart (ntfy.sh)

The VMs on the NAS come back up automatically after a reboot (power outage, NAS update, manual restart), but the LUKS volume is sealed afterwards — the user has to manually open the unlock page in the browser and enter the passphrase. Problem: without a back channel, the user only notices that when he next opens the app. This version builds a lightweight push channel via **ntfy.sh**:

- New settings card **"Notifications"** (between access protection and backup). Checkbox to enable, field for the ntfy topic name, optional custom ntfy server, save and test button. The topic name is freely chosen; it is the only "secret" of the push channel — the UI explicitly notes to choose a hard-to-guess name.
- The user installs the free **ntfy app** (iOS/Android), subscribes to the same topic name there — done. No account, no server, no fees.
- **Config lives outside the LUKS volume** at `/var/lib/ev-tracker/notify.json`. That's important because the unlock helper (`ev-unlock-web`) runs exactly when LUKS is sealed — it could not read any config from the app DB. The folder is owned by `ev-tracker:ev-tracker` with mode 0750, so neither sudo nor root is needed. Trade-off: the topic name is in plaintext outside the encryption. Whoever has root on the VM can read it — but whoever has root has already won anyway, so that's acceptable.
- Technical: `services/notify_service.py` encapsulates reading/writing the JSON file (with fallback to `data/notify.json` for local development) and the actual HTTP POST via `urllib.request` — no curl, no additional dependency. New routes `GET/POST /api/settings/notify` (load/save config) and `POST /api/settings/notify/test` (test message).
- **15 new translation keys** per language in all 6 languages (`set.notify_*`).

**Setup on the VMs (paste block as root):**

Since the actual push has to fire from the boot path (before LUKS is unlocked, so outside the app updates), a small new systemd unit `ev-notify-boot.service` plus the helper script `/usr/local/bin/ev-notify-boot` comes with it. The unit runs as a oneshot before `ev-unlock-web.service`, but only if LUKS is still sealed (`ConditionPathExists=!/srv/ev-data/app/venv/bin/python`). It reads `/var/lib/ev-tracker/notify.json`, and if `enabled:true` and a topic is set, sends a single POST to `<server>/<topic>` with hostname + time in the message. If the POST fails → exit 0, so a down ntfy server can never block the boot.

## v2.14.0 (2026-04-15)

### Wizard step 2 becomes "create web login" + backup/restore feature

**Wizard rework**

The setup wizard on freshly provisioned VMs now has a different second step. Previously it wanted to change the `ev-tracker` unix SSH password, which cut exactly the admin SSH connection and made maintenance unnecessarily harder. Instead:

- **Step 1** stays: change the LUKS passphrase. User must perform this.
- **Step 2 NEW**: the user creates a **web UI user + web UI password**. The option to change the shell password is removed entirely — the shell user is left untouched, so the admin can still get onto the VM via SSH with the ev-provision temp password for maintenance. The web login is from now on the only way into the dashboard.

Technical details:

- `templates/setup.html` — step 2 completely reworked: input fields for username + password + confirm, submit now calls `POST /api/setup/create_web_login`. Progress pills and the step-welcome list name the new step. The wizard header now also shows the app version as a badge.
- `services/setup_service.py` — `change_user_password()` and the sudoers dependency on `chpasswd` are gone. Wizard state key is now `weblogin_done` instead of `password_done`. The module docstring is updated and explicitly states that the wizard **does not** touch the unix login.
- `app.py` — new endpoint `POST /api/setup/create_web_login` replaces `POST /api/setup/change_password`. It calls `auth_service.set_credentials()` (which automatically arms the guard), logs the user in directly, and cleans up the setup marker on completed wizard state combination. `app_version` is now also passed through to the wizard template.

Settings → access protection stays unchanged and allows the user at any time to change, add or deactivate his web user/password.

**Database backup & restore**

New feature for VM migrations, backups and recovery after errors:

- New settings card "Backup & restore" (placed between access protection and app info).
- **Export**: `GET /api/backup/export` flushes the SQLite WAL via `PRAGMA wal_checkpoint(TRUNCATE)` and sends the complete `data/ev_tracker.db` as a download with timestamp in the filename (`ev-tracker-backup-YYYYMMDD-HHMMSS.db`). Contains absolutely everything: charges, trip log, maintenance log, AppConfig (incl. vehicle API credentials, home/work coordinates, ENTSO-E key, THG quotas, access-protection hash, session secret), geocode and weather cache, VehicleSync history. A single file.
- **Import**: `POST /api/backup/import` as multipart upload. Validates the file as a real SQLite DB and checks that the required tables `charges`, `app_config`, `vehicle_syncs` are inside. Before overwriting, creates a safety copy of the current DB in `data/backups/ev_tracker-pre-import-<ts>.db`, then closes the SQLAlchemy engine (important on POSIX, otherwise the old inode keeps the DB alive) and copies the new DB over. Then background thread with 500ms delay → `sudo systemctl restart ev-tracker.service`. The browser reloads automatically after 4.5 seconds.
- **Warning in the UI** is deliberately drastic: the import overwrites access-protection credentials and vehicle API keys. After an import, the web login from the backup applies, not the previous one.

New in `config.py`: `DATA_DIR` is now exported so `app.py` can resolve the DB path cleanly for the export/import routes.

**Translations**

25 new keys in all 6 languages (de/en/fr/es/it/nl): `wiz.welcome_step1_luks`, `wiz.welcome_step2_weblogin`, `wiz.weblogin_title`, `wiz.weblogin_desc`, `wiz.weblogin_username`, `wiz.weblogin_password`, `wiz.weblogin_password_hint`, `wiz.weblogin_password_confirm`, `wiz.weblogin_info`, `wiz.weblogin_submit`, `wiz.status_creating`, `wiz.err_user_empty`, and 13 `set.backup_*` keys.

**Upgrade on running VMs**

The old tags v2.11.x / v2.12.0 / v2.13.0 were deleted and `main` was reset to the v2.9.0 commit. Running VMs that previously had one of these versions can no longer get to the current main via `git pull` (history has been rewritten). Instead `git fetch origin && git reset --hard origin/main` — see the upgrade paste block in the release notes.

## v2.9.0 (2026-04-14)

### Translations for all v2.7.x/v2.8.x features + HTTPS autohide + README

- **60 new translation keys** in all 6 languages (de/en/fr/es/it/nl) — covers the setup wizard (`wiz.*`), the login page (`login.*`) and the access-protection block in the settings (`set.auth_*`). With that, all new features from v2.7.0–v2.8.1 are fully localized.
- **Setup wizard (`templates/setup.html`)** now uses `t()` instead of hardcoded German — title, welcome, both wizard steps, done screen, error messages and button texts.
- **Login page (`templates/login.html`)** is fully translated including footer text.
- **Access-protection block in settings** translated including error messages and disable-confirm dialog.
- **HTTPS autohide**: If the request comes from the Tailscale CGNAT range (`100.64.0.0/10`), `/settings` hides the entire HTTPS card. Tailscale already encrypts the transport — a self-signed certificate on top is just noise. Direct LAN or localhost access still sees the card as before.
- **README updated** with sections on web UI login, first-run setup wizard, VM deployment flow and the systemd awareness of the in-app updater. String count updated to ~540 per locale.

## v2.8.1 (2026-04-14)

- **Dashboard: average line in the SoH plot** — The SoH chart in the vehicle history gets a horizontal grey dashed line with the mean of all displayed measurement points. Makes drift/trends visible at a glance. The mean is shown in the legend below the chart as `Ø xx.x%`. Only active when ≥3 data points are present. Other charts stay unchanged.

## v2.8.0 (2026-04-14)

### Optional: Web UI login as a gatekeeper page

Tailscale protects network access — but whoever knows the share link and is in the tailnet lands in the dashboard without further hurdles. This release brings a built-in password gatekeeper page as defense-in-depth:

- **Optional**: off by default. Whoever wants it enables it in Settings → "Access protection". Existing installs are unchanged after update, nobody is locked out of their own app.
- **Integrated**: part of the app, not shoved in front of the app. Updates from the GitHub repo roll through normally and don't break auth.
- **Session cookies**: Flask sessions with a per-install generated 32-byte secret persisted in AppConfig (see `services/auth_service.py:get_or_create_session_secret`). 30-day lifetime.
- **Password hashing**: Werkzeug `generate_password_hash` / `check_password_hash` (bcrypt-compatible). Plaintext never lands on disk.
- **Simple UX**: simple username+password login, no email, no account management. Single flow for the one-person case.

New endpoints: `/login`, `/logout`, `/api/auth/enable`, `/api/auth/disable`, `/api/auth/change_password`. Guard runs as a `before_request` hook in parallel with the setup-wizard guard — setup has priority, so a freshly provisioned user can first get through the wizard without already being auth-configured.

The prerequisite for real security is still that the VM is only reachable via Tailscale (UFW only on `tailscale0`). The app login is the second layer after the VPN.

## v2.7.4 (2026-04-14)

- **Setup wizard: LUKS device detection without root privileges** — `get_luks_device()` previously called `cryptsetup status evdata`, but that has to open `/dev/mapper/evdata`, which on Debian belongs to `root:disk 660`. The app user `ev-tracker` is not in the `disk` group, so the call failed with Permission denied. Consequence: the wizard footer showed "LUKS device: (unknown)" and — much more seriously — the actual passphrase change aborted with "LUKS device not found". Now the path is resolved via **sysfs**: `/dev/mapper/evdata` → `dm-N` → `/sys/block/dm-N/slaves/` → parent block device. Sysfs is world-readable, so this needs neither sudo nor group membership.

## v2.7.3 (2026-04-14)

- **Setup wizard: make the browser redirect reliable** — The `before_request` hook checked the `Accept` header to distinguish browser accesses from API calls. That was too fragile: depending on browser/Accept header, the user landed on the JSON response `{"error":"setup_pending",...}` instead of the wizard. Now it's simple: all GET requests are redirected to `/setup` during setup, only non-GET (POST/PUT/DELETE) still get the JSON 503 response for API clients.

## v2.7.2 (2026-04-14)

- **Setup wizard explicitly restricted to Linux** — `is_setup_pending()` now hard-returns `False` on macOS and Windows, without even checking the marker path. In practice that was already the case (the path `/srv/ev-data/.setup_pending` doesn't exist on non-Linux hosts anyway), but now it's also clearly documented in the code that the wizard is VM-specific. Additionally protects against the edge case where somebody accidentally creates a file under the path and thereby triggers the wizard even though the necessary `sudo cryptsetup`/`chpasswd` commands don't exist.

## v2.7.1 (2026-04-14)

- **Setup wizard: second step for the SSH login password** — The first-run wizard now takes, alongside the LUKS passphrase, a new login password for the `ev-tracker` user. Under the hood calls `sudo chpasswd` (needs an additional NOPASSWD sudoers entry for `/usr/sbin/chpasswd`). Wizard progress is tracked in `/srv/ev-data/.setup_state.json` so a mid-wizard reload puts the user seamlessly at the next open step instead of asking for LUKS again. Only when both steps are done is the marker deleted and the dashboard released. With that, the admin can forget both temp credentials after provisioning — the user is fully self-sufficient.

## v2.7.0 (2026-04-14)

### First-run setup wizard for VM deployments

Previously the end user of a freshly provisioned VM had to SSH in and manually run `sudo cryptsetup luksChangeKey /dev/sdb` to replace the temporary LUKS passphrase. That was a big hurdle for non-technical users. Now a setup wizard automatically appears on first browser access:

1. The provisioning pipeline (`ev-provision`) creates a marker `/srv/ev-data/.setup_pending` at the end.
2. A `before_request` hook redirects all non-setup requests to `/setup` as long as the marker exists.
3. The wizard (a single-page HTML wizard UI in `templates/setup.html`) prompts for the temp and the new passphrase, calls `sudo cryptsetup luksChangeKey` on the device from the running `cryptsetup status evdata`, and removes the marker on success.
4. After a successful change, the user has "arrived" — from that moment on nobody but the user knows the passphrase, not even the admin.

The wizard is German-only in its text (setup is a one-time flow and the target audience is German users), the rest of the app stays translated as before. Non-VM hosts (e.g. developer laptops) are unaffected because the marker never exists.

**Requirement for live operation**: `ev-provision` has to create the marker at the end and set the sudoers rule for `cryptsetup luksChangeKey`. Both are documented in the admin guide; for existing VMs, retrofit once.

## v2.6.0 (2026-04-14)

### Fix the in-app updater under systemd

On Linux installations with `ev-tracker` as a systemd service, the update button via the app UI effectively did nothing: click → brief "Update is being installed" display → after refresh still the old version. Root cause: the updater spawns a detached `updater_helper.py` process that is supposed to do the file swap after the Flask process exits. Under systemd, the helper lands in the **same cgroup** as the service — and when systemd kills the service for restart, the helper is dragged along **before it has swapped the files**. Result: service restarts, nothing has changed.

Fix: systemd is now detected (via `INVOCATION_ID` or `/run/systemd/system`), and in that case the file swap runs **inline in the Flask process** before it exits. Python bytecode is already in RAM, so overwriting the `.py` files on disk is safe. `pip install -r requirements.txt` runs synchronously, then `os._exit(0)` — and `Restart=always` in the systemd unit ensures the service comes back up with the new code.

For standalone installations (macOS, Windows, or Linux without systemd), the existing helper path stays unchanged.

## v2.5.9 (2026-04-13)

- **Kia/Hyundai token fetch: Selenium flow made fit for headless Linux environments** — On VMs without a DBus session (e.g. server installs with Xvfb+noVNC for the login flow), the Selenium-based token fetch stumbled several times: (1) Chromium crashed with "DevToolsActivePort file doesn't exist" due to missing `--no-sandbox` / `--disable-dev-shm-usage` flags, (2) `webdriver-manager` pulled an outdated ChromeDriver version (max 114) that didn't fit modern Chromium 147, (3) Debian's Chromium lives under `/usr/bin/chromium` instead of `/usr/bin/chrome`, which Selenium didn't find automatically.
- Fix: `webdriver-manager` completely removed in favour of the built-in **Selenium Manager** (from Selenium 4.11), which pulls the matching ChromeDriver automatically. Chromium binary path is now automatically detected from `/usr/bin/chromium|chromium-browser|google-chrome`. Sandbox and shared-memory flags are always set. Requirement bumped where needed to `selenium>=4.11`.

## v2.5.8 (2026-04-12)

- **Trip log: regen column was always empty** — On every movement detection, `prev.departed_at` and `curr.arrived_at` are the same sync timestamp (the moment movement was detected), so the cumulative regen delta was always 0. The departure now anchors on `prev.last_seen_at` (last confirmed sync at the old spot before departure), the arrival stays `curr.arrived_at` — so the delta calculation spans two different syncs.

## v2.5.7 (2026-04-11)

- **Charge and regen cycles as whole numbers** — `charge_cycles` and `recup_cycles` in `get_summary_stats` now round to whole cycles instead of one decimal place. Fractional cycles make no intuitive sense; a whole cycle is the unit.

## v2.5.6 (2026-04-11)

### Hybrid recuperation: keep the km × 0.086 estimate, layer measured on top

v2.5.4/5 replaced the full lifetime recuperation with the tiny measured cumulative (6.92 kWh for the first 92 km of tracking), which threw away years of historical km where the old `km × 0.086` estimate was the best number available.

This release uses a hybrid:

- **km before the first vehicle sync** → `first_sync_odometer × static_rate` (default 0.086 kWh/km, still configurable in Settings)
- **km from that point on** → real measured `regen_cumulative_kwh` from the vehicle API

Result on a real Kia Niro dataset: `82217 × 0.086 + 6.92 = 7077.6 kWh` — matches the pre-v2.5.4 lifetime total, and from here on grows only via measured values as the car drives.

- The Recuperation KPI card now shows `7.071 + 6.9 (kumuliert)` to make the split obvious.
- The measured rate (0.075 kWh/km from the last 90d) is still shown with the broadcast icon — that's the *current driving efficiency*, separate from the historical baseline.
- The "Gemessene Rekuperation" card remains unchanged: it only shows real per-period measurements and never touches the historical km × 0.086 portion.

## v2.5.5 (2026-04-11)

### Regen scale hotfix: raw is Wh, not hundredths of kWh

v2.5.4 divided the raw Kia/Hyundai `total_power_regenerated` by 100, which still left values 10× too high. The actual unit is **Wh** (watt-hours) for a rolling 3-month window — the correct divisor is **1000**. On a real Kia Niro EV dataset the v2.5.4 values showed a regen rate of 0.75 kWh/km (physically impossible); after this fix the rate settles at ~0.075 kWh/km (matches the car's spec).

- **`_build_vehicle_sync`** now divides by 1000.0 instead of 100.0.
- **New migration `regen_scale_fix_v2`** applies a second `/10` pass on `total_regenerated_kwh` — so pre-v2.5.4 rows (already `/10`'d by v1) land on `/100` total, and v2.5.4 rows land on `/10`. Both converge on the correct `raw/1000` kWh scale.
- **`regen_cumulative_kwh` is wiped and recomputed** after the v2 migration so the monotonic series matches the corrected inputs.
- Live vehicle widget and dashboard "Gemessene Rekuperation" card now show realistic numbers.

## v2.5.4 (2026-04-11)

### Rekuperation: korrekt interpretiert, kumuliert, pro Fahrt

The Kia/Hyundai API returns `total_power_regenerated` as **hundredths of kWh for a rolling 3-month window** — not lifetime, not tenths. Every stat that touched that value was previously off by a factor of 10 and mistook the rolling window for a cumulative total. This release fixes the interpretation and builds real per-period / per-trip statistics on top of it.

#### Data fix
- **Divisor corrected** in `_build_vehicle_sync` ([app.py](app.py)): raw value is divided by **100** (not 10). A raw reading of `21534` now stores the correct `215.34 kWh` instead of `2153.4 kWh`.
- **One-time migration** on startup divides every existing `vehicle_syncs.total_regenerated_kwh` by 10 to retroactively fix rows written under the old scale. Gated by `regen_scale_fix_v1` in AppConfig so it only runs once.
- **New column `regen_cumulative_kwh`** on `vehicle_syncs` — monotonically increasing "measured regen since first sync". Built from delta-walking the raw series: positive deltas add up, rollovers (new raw < previous raw, meaning a month fell off the 3-month window) contribute 0. Backfilled for existing rows automatically on first boot after upgrade.

#### Dynamic recuperation rate
- **`kWh/km` recuperation rate is now measured from the last 90 days of vehicle syncs** (cumulative regen delta / odometer delta) instead of the hardcoded `0.086`. Falls back to the configured static value when there's no vehicle data. Settings page shows a green "automatisch" badge + the measured rate when in use.
- `get_summary_stats` now prefers the real measured lifetime cumulative over the extrapolated `total_km * recup_rate` estimate whenever vehicle history is available.

#### New `get_regen_stats()`
- Returns measured recuperation aggregated by: **today, this week, this month, last 30d, last 90d, this year, lifetime**, plus `km_equivalent` (lifetime regen converted to km at the car's actual consumption).
- Uses `bisect` lookups against a single sorted pull of the cumulative series — O(log n) per query.

#### Per-trip recuperation
- Each trip in `get_trips()` gets a `regen_kwh` field via cumulative-at-timestamp lookups at `departed_at` and `arrived_at`.
- Trip summary (`get_trip_summary`) adds `total_regen_kwh` and `regen_per_km` across the visible window.
- New **Rekup** column in the `/trips` table and in the PDF Fahrtenbuch table (80 most recent trips).

#### Dashboard
- New **"Gemessene Rekuperation"** card directly under the KPI grid: 6 period cards + km-equivalent, only shown when vehicle sync data exists.
- **Recuperation KPI card** now shows the measured `kWh/km` rate instead of the configured one, plus a `bi-broadcast` icon when the rate is being pulled live from the car.
- **Vehicle-history Regen chart** switched from the rolling 3-month raw value (which fluctuates month-to-month) to the monotonic cumulative, so the line actually grows instead of wiggling.
- **Live vehicle widget** label updated to `Rekuperiert (3 Mon.)` and the double `/10` bug fixed — the widget now shows the correct kWh value.

#### PDF report
- New page **"Gemessene Rekuperation"** with an 8-cell KPI table (today / week / month / 30d / 90d / year / lifetime / km-equivalent) + the auto-detected rate.
- Fahrtenbuch table gets a **Rekup** column (column widths adjusted).
- Vehicle-history "Rekuperation gesamt" chart title updated to "Rekuperation (gemessen, kumuliert)".
- `regen_delta` summary line on the vehicle-history page is now labelled "Rekup. kumuliert".

#### Translations
- 13 new keys × 6 languages for the regen period cards, the settings badge, and the trips column.

## v2.5.3 (2026-04-10)

### Cross-platform polish

- **Windows: startup banner & emoji log lines** — `app.py` reconfigures stdout/stderr to UTF-8 with `errors='replace'` at import time, so `python app.py` in a legacy cmd code page no longer raises `UnicodeEncodeError` on the "⚡ EV Charge Tracker" banner. `start.bat` already set `chcp 65001` for its own window, but manual launches from an unconfigured shell now survive too.
- **Linux: IP discovery in `start.sh`** — now tries `ip -4 -o addr show scope global` first (modern distros), then `hostname -I` (glibc), then `ifconfig` (BSD / macOS / older). Each branch is tolerant of missing binaries. Previously Alpine/BusyBox machines saw an empty "Smartphone-URL" line for no good reason.
- **Updater: restore exec bit after update** — GitHub source zips strip the POSIX exec bit, so after an in-app update `./start.sh` was no longer directly executable on Linux/macOS. [`updater_helper.py`](updater_helper.py) now `chmod +x`'s `start.sh` and `start.command` right after the file swap on non-Windows platforms.
- **`datetime.utcnow()` → timezone-aware** — `services/ssl_service.py` replaces the deprecated call with `datetime.now(timezone.utc)` for cert generation. `get_cert_info()` also handles both `not_valid_before`/`after` (cryptography <42) and `not_valid_before_utc`/`after_utc` (>=42) so it works across versions without a DeprecationWarning.

## v2.5.2 (2026-04-10)

### Unified vehicle sync log line
- **Every vehicle sync now logs the same structured one-liner** regardless of which code path triggered it:
  ```
  Vehicle sync [smart->force, src=bg-loop]: SoC=73%, odo=14283km, GPS=yes, charging=False, api=34/200
  ```
- **mode** reflects the actual API mode that was used:
  - `cached` / `force` for the straight modes
  - `smart->cached` (smart mode ran cached because GPS fresh or car charging)
  - `smart->force` (smart mode escalated to force because GPS stale and not charging)
- **src** reflects the caller, so you can tell which trigger caused the call:
  - `bg-loop` — background sync service (the 10-min smart cadence)
  - `trips-auto` — auto-fresh on `/trips` page load (background thread)
  - `manual` — "Jetzt synchronisieren" button on the trips page
  - `settings` — "Sync (Cached)" / "Sync (Live)" buttons in Settings
  - `dashboard` — the cached/live refresh on the dashboard widget
- **GPS=yes/no** — whether the response carried a location (important for the Fahrtenbuch; Kia cached mode usually returns `no`).
- **api=N/200** — current daily API counter right after the call, so you can see budget burn in real time in the `/logs` feed.
- New helper `log_sync_result()` in [services/vehicle/sync_service.py](services/vehicle/sync_service.py) is the single source of truth — all five call sites now route through it.

## v2.5.1 (2026-04-10)

### Live log viewer
- **New `/logs` page** with its own nav entry. Shows whatever the app's Python loggers emit: vehicle sync activity, parking hook decisions, Nominatim reverse lookups, updater events, ENTSO-E calls, errors — everything that used to only be visible in the terminal.
- **In-memory ring buffer** (last 2000 records) via a custom `RingBufferHandler` attached to the root logger on startup. Thread-safe, zero disk I/O, zero config. New file: [services/log_service.py](services/log_service.py).
- **Live polling** every 2 s via `/api/logs?after=<last_id>` — only new records cross the wire, so the tab stays cheap even when it's sitting open all day. Delta-based, not a full re-fetch.
- **HTTP access logging is opt-in**, toggle in the toolbar. Off by default (keeps the feed clean); flip it on and every `GET /api/...` line from werkzeug shows up too. The preference is persisted in AppConfig (`log_show_requests`) so it survives restarts.
- **Toolbar controls**: auto-refresh on/off, auto-scroll on/off, level filter (DEBUG+ / INFO+ / WARNING+ / ERROR+), free-text filter (matches logger name + message), clear, download as `.log` file.
- **Color-coded by level** — DEBUG grey, WARNING amber, ERROR red, CRITICAL bold red — in both light and dark mode. Monospace font, timestamp with milliseconds.
- `POST /api/logs/clear` and `POST /api/logs/requests` round out the API.

### Translations
- 11 new keys × 6 languages.

## v2.5.0 (2026-04-10)

### Fahrtenbuch: honest numbers, smarter sync, real addresses

#### Dropped misleading trip duration / avg-speed
- **Trip duration, "Fahrzeit" KPI and "Ø km/h" column removed.** With any realistic polling cadence the "arrived_at" of the next parking event is off by up to the sample interval, so any duration/speed number was a fiction. What we report now is what we actually know: **km from the odometer** and **SoC used**.
- PDF "Fahrtenbuch" table drops min / km-h columns and widens From / To columns instead.
- Highlights page drops "Schnellste Fahrt"; "Längste Fahrt" shows km only.
- CSV export drops the dauer/km-h columns.

#### Smart-sync active window
- **New `smart_active_start_hour` / `smart_active_end_hour` / `smart_active_interval_min`** AppConfig keys (defaults 6 / 22 / 10). Fully configurable from Settings → Vehicle API (the new row appears when `Smart` mode is selected).
- Smart mode now runs **every 10 min between 06:00 and 22:00 by default** and **does not sync at all at night** — better granularity for catching real movement without burning the 190/200 daily Kia quota and without waking the car's 12V battery while you sleep.
- With the default 10 min × 16 h = ca. 96 cached calls/day plus the existing "force if GPS stale >6 h and not charging" logic for the Live upgrades. Settings hint shows the math next to the row.
- `_compute_sleep_secs()` in [services/vehicle/sync_service.py](services/vehicle/sync_service.py) handles both smart-window and the legacy hourly cadence for `cached`/`force` modes. Outside the window the loop sleeps until the window opens without firing any API calls.
- Interval options: 5 / 10 / 15 / 20 / 30 / 45 / 60 min. Minimum hardcoded to 5 min.

#### Unknown locations are always resolved to an address / POI
- **No more raw `53.12, 10.45` coordinates** in the Fahrtenbuch. Every parking event gets its `address` field populated via Nominatim reverse-geocoding. POIs (shops, restaurants, parking lots) are captured too because Nominatim's `display_name` leads with the POI name when one exists.
- **Background worker** fires on every `/trips` page load and fills addresses for any parking event that doesn't have one yet (up to 50 per run, 1 req/s per Nominatim ToS, permanent DB cache → after the first full pass it's a no-op).
- **New `POST /api/trips/geocode_missing`** for manual re-trigger.
- Trips table now shows: 🏠 Zuhause / 💼 Arbeit / ⭐ Favorit / full street address — never raw coordinates. While a new event is waiting to be resolved, the row shows "Adresse wird ermittelt…".
- New `geocode_missing_events()` helper in `services/trips_service.py`.

### Translations
- 8 new keys × 6 languages (`trips.home`, `trips.work`, `trips.resolving`, `set.api_smart_window_label`, `set.api_smart_from`, `set.api_smart_to`, `set.api_smart_every`, `set.api_smart_hint`).

## v2.4.3 (2026-04-09)

### Trips page is fast again
- **Background auto-fresh** — the live vehicle sync that runs when you open `/trips` no longer blocks page rendering. It now runs in a daemon thread, so the page renders in ~12 ms instead of 5-10 s waiting for Kia to wake the car. The page will show whatever GPS data we already have; the background sync drops in updated data that will appear on the next reload.
- **Threshold raised** from 30 minutes to 2 hours. With smart-mode enabled and the background-fresh debounce, the API counter doesn't get burned on every visit during the day.
- **5-minute debounce** so two `/trips` visits in quick succession only kick off one background sync (and the second one isn't told a stale "in flight" sync was a fresh sync).
- **GPS freshness indicator** in the toolbar: "GPS vor 12 min", "GPS vor 3 h", "GPS vor 2 Tagen". You can see at a glance how stale the map data is and decide whether to hit "Jetzt synchronisieren" manually.

### Translations
- 3 new keys × 6 languages.

## v2.4.2 (2026-04-09)

### Fahrtenbuch — actually working with sparse Kia polling

This release fixes a stack of subtle bugs that prevented parking events from being created on a real-world database with the Kia/Hyundai cached-mode sync.

#### Root cause fix
- **Parking hook now runs on EVERY save**, not only when `differs_from(last)` returns True. Previously, a force-refresh that delivered the *same* GPS coordinates as the existing latest row (because the car hadn't moved) would skip the hook entirely — so no parking event was ever created. Fixed in `_save_vehicle_sync` ([app.py](app.py)).

#### Backfill
- New `backfill_parking_events()` in `services/trips_service.py` replays every existing `VehicleSync` row chronologically through the parking hook. This catches up databases populated before v2.3.0 (no hook) or after weeks of cached polling where the hook only fired occasionally.
- **Auto-runs on startup** if the parking_events table is empty AND there is at least one VehicleSync row with GPS data.
- **`POST /api/trips/backfill`** for manual triggering. New "Aus Historie nachbauen" button on the `/trips` page.

#### Smart sync mode
- New `'smart'` option in Settings → Vehicle API alongside `cached` and `force`.
- Smart mode runs cached by default but **upgrades to a force-refresh when the latest GPS-bearing sync is older than 6 h** (configurable via `smart_force_max_hours`) and the car is not currently charging. This catches movement without burning the 12V battery on every cycle.
- Tracks `last_force_refresh_at` so the smart-mode decision logic has something to compare against.

#### Tighter trip durations
- New `last_seen_at` column on `parking_events` is updated on every sync that confirms the same position. The trip-duration calculation now uses `last_seen_at` of the previous event as the lower bound instead of `arrived_at`, which would have overstated the trip duration by the entire parking spell.
- Auto-migrated on startup.

#### Trips page auto-fresh
- Opening `/trips` automatically triggers a force vehicle sync if all of these are true: a brand is configured, auto-sync is enabled, the brand supports GPS (per the feature matrix), the latest GPS sync is >30 min old, and the daily API counter is below 180/200. Skipped silently otherwise. Means the map is current the moment you open the page.

#### Manual sync button
- **"Jetzt synchronisieren"** button on `/trips` triggers an immediate force refresh and reports back whether GPS came through or not.

#### Settings UX
- **Warning banner** when sync mode is `cached` and brand is Kia/Hyundai: "GPS für Fahrtenbuch erfordert Smart oder Live, oder manueller Sync (Live)".
- Last-sync line in Vehicle API card now shows a 📍 icon when the most recent row has GPS data.

### Translations
- 13 new translation keys × 6 languages.

## v2.4.1 (2026-04-09)

### Restart button
- **New "App neustarten" button** in Settings → App-Info, plus an inline "Jetzt neustarten" button that appears after saving HTTPS settings or generating a new certificate. No more manual `start.command` after switching HTTPS mode.
- **Restart-only mode** for `updater_helper.py`: `--staging-dir` is now optional. Without it the helper skips the file swap and the pip install, just waits for the parent PID and spawns a fresh `venv/bin/python app.py` with the same nohup-wrap, env-strip, and health check as the update flow.
- **`POST /api/restart`** triggers the same delayed-shutdown pattern as `/api/update/install`, and the Settings page polls until the app is back online and reloads the browser.

## v2.4.0 (2026-04-09)

### HTTPS / TLS support
- **Self-signed certificate** auto-generation via the `cryptography` library (preferred) or `openssl` CLI (fallback). Cert is stored in `data/ssl/server.{crt,key}` and reused across restarts. SAN entries cover `localhost`, `127.0.0.1`, and the LAN IP, so the same cert works on desktop AND smartphone.
- **Three modes** in Settings → "HTTPS / Sicherheit": `off` (HTTP), `auto` (self-signed), `custom` (paths to your own Let's Encrypt cert).
- **Cert metadata viewer** — subject, valid-until date, SHA256 fingerprint shown in the UI. Parsing falls back from `cryptography` to `openssl x509 -text` so it works without the library.
- **"Cert herunterladen"** button serves the public cert as a `.crt` download — install it on your iPhone/Android via Profile to get rid of browser warnings permanently.
- **HTTP/insecure warning** banner in Settings if the user accesses the app over HTTP from a non-localhost address (Geolocation API and PWA features won't work over plain HTTP).

### Brand feature matrix
- New `services/vehicle/feature_matrix.py` with hand-curated capabilities for all 14 brands across 10 features (SoC, GPS, 12V battery, SoH, recuperation, 30-day consumption, doors/locks, climate, tire pressure, live status).
- **`/api/vehicle/features/<brand>`** returns the matrix for the selected brand.
- **Settings → Vehicle API** shows a 10-item grid with green/yellow/red indicators when a brand is picked. No more "wait, why isn't my Polestar showing recuperation data" surprises.

### Tesla connector expansion
- **Tire pressure warnings** computed from `tpms_pressure_*` vs `tpms_rcp_*_value` recommended pressures.
- **Climate detail**: defrost, rear window heater, steering wheel heater.
- **Software update detection** via `vehicle_state.software_update.status`.
- **Charging session detail**: `minutes_to_full_charge`, `charge_energy_added`, charger voltage and current — exposed in `raw_data`.
- **Sentry mode state** for the security-conscious.

### Manual location for charges
- **Charge form** ([templates/input.html](templates/input.html)) gets a new "Standort der Ladestation" section with:
  - Free-text location name (e.g. "Aldi Berlin Mitte", "Ionity A2")
  - Lat/lon fields
  - **"Mein Standort"** button uses the browser's Geolocation API (works on smartphones over HTTPS or on localhost)
  - **"Zuhause"** / **"Arbeit"** quick-fill from your saved Settings locations
  - Reverse-geocoding via Nominatim auto-fills the name field if you didn't type one
  - **Clear** button
- Captured charges feed the existing `Charge.location_lat/lon/name` columns, which the **charging stations memory** in the highlights service already groups by location for "cheapest stations on my regular routes".

### Database
- New `AppConfig` keys: `ssl_mode`, `ssl_custom_cert`, `ssl_custom_key`. Auto-created on first save.

### Dependencies
- Added `cryptography>=42.0.0` to `requirements.txt` (previously optional via openssl CLI).

### i18n
- 47 new translation keys × 6 languages.

## v2.3.4 (2026-04-09)

### Favorites picker — visible feedback + diagnostics
- **Crosshair cursor + blue outline** on the map when in pick mode (home/work/favorite). Previously the user had no visual confirmation that the map was waiting for a click.
- **Console logging** of every step in the favorites flow (pickMode transitions, map clicks, POST results) so issues can be debugged from browser DevTools.
- **`e.preventDefault()` + `e.stopPropagation()`** on `btnAddFav` click — defensive against any parent form swallowing the event.
- **Auto-focus the name field** when "Bitte Name eingeben" warning fires.
- **Status message includes coordinates and name** after a favorite is saved, so the user sees concrete confirmation.
- **Refactored map click handler** into a named `handleMapClick` function with explicit early returns per branch — easier to reason about and reduces the chance of state bleed between branches.

## v2.3.3 (2026-04-09)

### The actual updater fix (root cause)
- **`debug=True` → `debug=False` in `app.py`** — the Werkzeug auto-reloader passes a listening socket to its child via the `WERKZEUG_SERVER_FD` environment variable. That env var was propagating from the dying Flask through `os._exit` → `updater.py` → `updater_helper.py` → freshly-spawned Flask, where `socket.fromfd(WERKZEUG_SERVER_FD)` then crashed with `OSError: [Errno 9] Bad file descriptor`. For a self-hosted app, debug mode is the wrong default anyway.
- **Helper strips `WERKZEUG_*` env vars** before spawning, as belt-and-suspenders for older `app.py` files that still have `debug=True`.

### Fixes
- **Favorites can now be set on the map** — the `btnAddFav` button was missing `type="button"` and used a brittle one-shot click handler. Both fixed: button now has explicit type, and the favorites flow uses the same `pickMode` pattern as home/work picking. Pressing Enter in the favorite name field now also triggers add-mode. Errors are logged to status with the actual response code.

## v2.3.2 (2026-04-09)

### Updater fixes (the actual restart problem)
- **`nohup`-wrap the new Python process**, not just the bash launcher. macOS Terminal.app sends SIGHUP to *every* process in its session when the window closes, even processes that called `setsid`. The v2.3.1 fix bypassed `start.sh` correctly but the bare `python app.py` was still vulnerable. v2.3.2 wraps it in `/usr/bin/nohup` which sets `SIG_IGN` for SIGHUP — survives terminal close.
- **Health check after spawn** — the helper now waits 4 seconds, verifies the spawned process is still alive (`p.poll() is None`), and probes port 7654 with a TCP socket. Failure is logged with the exit code instead of being silent.
- **More verbose logging** — every step in `_restart_app` is timestamped so future failures are debuggable from `updates/restart.log` alone.

## v2.3.1 (2026-04-09)

### Updater fixes
- **Helper restarts the app reliably** — `updater_helper.py` now spawns `venv/bin/python app.py` directly instead of going through `start.command` → `start.sh`. This bypasses the redundant pip install loop and the `set -e` shell pitfalls, dropping restart latency from ~15 s to ~3 s.
- **Port-release race fix** — wait 2 s after the parent Flask process dies before binding the port again, so we can't hit `EADDRINUSE`.
- **Restart log** — every restart attempt is logged to `updates/restart.log` with timestamps and stdout/stderr of the spawned process. Previously failures were silent because output went to `/dev/null`.
- **`_spawn_helper` prefers the staging helper** — `updater.py` now launches `staging/.../updater_helper.py` (the new release) instead of the in-place helper, so future updater bugfixes take effect on the very first update that ships them.

## v2.3.0 (2026-04-09)

### New Features

#### Driving log / Fahrtenbuch
- **Auto-detected parking events** — every vehicle sync hooks into a new `ParkingEvent` log. The car's location is checked against the last open event; >100 m means "moved", a new event is opened, the previous one is closed with arrival/departure odometer + SoC.
- **Home / Work / Favorites picker** — click on a Leaflet/OpenStreetMap card in Settings to set your home and work coordinates (drag-to-fine-tune supported). Optional named favorites for parents, vacation home, etc. All parking events are auto-classified as `home`/`work`/`favorite`/`other` with a 200 m radius. Reclassification runs whenever you change a location.
- **Trips page** at `/trips` — KPI cards (count, total km, drive time, commute km), Leaflet map with marker clustering colored by location label, full trips table with from/to/km/duration/avg-speed/SoC.
- **CSV + GPX export** — `/api/trips/export.csv` for the tax advisor, `/api/trips/export.gpx` for Google Earth / Komoot / OsmAnd.
- **PDF report** gets a new "Fahrtenbuch" section with the last 80 trips and a header showing home↔work km (relevant for German Pendlerpauschale).

#### Maintenance log / Wartungs-Logbuch
- **New `/maintenance` page** — track inspections, tires, brakes, wipers, 12V battery, cabin filter, MOT/TÜV with date, odometer, cost and free-text notes.
- **Smart reminders** — every entry can have a `next_due_km` and/or `next_due_date`. The page surfaces a "due soon / overdue" banner; the form auto-fills sensible defaults (e.g. inspection = 12 months / 30 000 km).
- **PDF report** gets a "Wartungs-Logbuch" section with the full history and total cost.

#### Charging stations memory
- **Lat/lon/name on `Charge`** — the input form now optionally captures the location of a charge.
- **`/api/highlights` returns charging stations** grouped by rounded coordinates with cheapest €/kWh, total kWh, count and last-used date — for finding the cheapest stations within your usual routes.

#### Range calculator
- **Realistic range estimate** at `/api/range` — uses live SoC, the configured battery capacity, the 30-day average consumption from the API (or fallback to lifetime average), and the current outdoor temperature from Open-Meteo at your home location. Applies a temperature penalty (1.30× below 0°C, 1.18× < 10°C, 1.06× < 20°C, 1.10× > 30°C). Shown as a dashboard card.

#### Weather correlation
- **Open-Meteo integration** — `services/weather_service.py` fetches daily mean temperatures for your home location with DB caching (no API key, no rate-limit issues for normal usage).
- **Dashboard chart** — bar (kWh/month) + line (avg outdoor °C) showing exactly why winter is more expensive.

#### Highlights / fun facts
- **Dashboard "Highlights" card** — cheapest charge, most expensive charge, biggest single charge, longest trip (km), fastest trip (avg km/h), longest park (days). Also rendered on a dedicated page in the PDF report.

#### Reverse geocoding
- **Nominatim integration** — `services/geocode_service.py` resolves coordinates to street addresses, with a permanent DB cache and a 1-second rate-limiter (Nominatim ToS). Used by parking events on demand.

#### THG quota reminder
- **Banner** between January 1 and March 31 if no THG quota is logged for the previous year — direct link to Settings.

### Database
- New tables: `parking_events`, `maintenance_log`, `geocode_cache`, `weather_cache` — auto-created on startup.
- New columns on `charges`: `location_lat`, `location_lon`, `location_name` — auto-migrated.

### i18n
- **83 new translation keys** in all 6 languages (DE, EN, FR, ES, IT, NL) — every new page, banner, button and tooltip is fully localized.

## v2.2.0 (2026-04-09)

### New Features
- **Real in-app updater** — the "Update verfügbar" button in Settings now actually rolls out the update on the user's machine instead of opening the GitHub release page in a browser. Click → confirm → the app downloads the new release zip, stages it, hands off to a detached `updater_helper.py` process, gracefully shuts itself down, the helper swaps files (preserving `venv/`, `data/`, `.git/`), runs `pip install -r requirements.txt` and re-launches the app via the platform start script. The settings page polls until the app comes back online and reloads the browser automatically.
- **`POST /api/update/install`** and **`GET /api/update/check`** routes drive the new flow.

### How it works
The trick is the detour through a standalone `updater_helper.py` script: the running Flask process cannot safely overwrite its own `app.py` and templates while still serving requests, so the helper runs in a separate detached subprocess that waits on the parent PID, then performs the file swap. Pattern adapted from `shelly-energy-analyzer`.

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
