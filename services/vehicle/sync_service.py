"""Background service for periodic vehicle status sync."""
import json
import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_sync_thread = None
_sync_running = False
_nightly_thread = None
# v2.29: per-vehicle force-refresh queue. Keyed by vehicle_id (int);
# value is the reason string. ``request_force_refresh(reason, vid)``
# adds to it; the sync loop's per-vehicle iteration consumes the
# entry for the vehicle it's syncing. ``None`` key reserved for
# "any vehicle" / global triggers (e.g. a manual button).
_force_refresh_pending: dict = {}
_post_move_reconcile_pending = False  # set after a PE transition; triggers a one-shot backfill+reconcile

# Hour-of-day the nightly maintenance task fires. 03:00 local is chosen
# because (a) Hyundai has the previous day's /tripinfo fully populated
# by then, (b) the car is almost certainly parked / asleep so a passive
# SDK fetch doesn't collide with live polling, and (c) it's well before
# the smart-window start hour so the sync loop's main cadence is not
# disturbed.
NIGHTLY_HOUR = 3


def request_force_refresh(reason: str = 'manual', vehicle_id: int | None = None) -> None:
    """Queue an immediate force-refresh on the next sync-loop tick.

    Called from the parking hook when motion is detected: the sync that
    detected motion may have been cached (stale), so this asks the next
    tick to wake the car for a fresh GPS/SoC/odometer snapshot at the
    new location.

    v2.29: ``vehicle_id`` scopes the refresh — only that vehicle gets
    upgraded to force on the next tick. ``None`` triggers a refresh
    on every vehicle (used by manual buttons that don't know which
    car the user means). The sleep loop polls this dict in 10s
    increments, so the refresh happens within ~10 seconds of the
    request even if the smart-mode interval is 10 min.
    """
    global _force_refresh_pending
    _force_refresh_pending[vehicle_id] = reason
    logger.info(f"Force-refresh queued: {reason} (vehicle_id={vehicle_id})")


def request_post_move_reconcile() -> None:
    """Queue a one-shot SDK backfill + trip reconcile for today.

    Called from the parking hook after a PE transition: the car just
    arrived somewhere new and the SDK trip-info endpoint should have
    fresh data within a minute or two. Running backfill + reconcile
    immediately (instead of waiting for the 03:00 nightly task) snaps
    departed_at/arrived_at to SDK timestamps today and lets the regen
    lookup find the correct pre/post-drive values."""
    global _post_move_reconcile_pending
    _post_move_reconcile_pending = True
    logger.info("Post-move reconcile queued")

MIN_INTERVAL_HOURS = 1  # cached/force modes: 1 hour minimum
DEFAULT_INTERVAL_HOURS = 4

# Smart mode defaults: sample every 10 min between 06:00 and 22:00, sleep at night.
DEFAULT_SMART_INTERVAL_MIN = 10
MIN_SMART_INTERVAL_MIN = 5
DEFAULT_SMART_START_HOUR = 6
DEFAULT_SMART_END_HOUR = 22


def log_sync_result(status, mode_label: str, source: str) -> None:
    """Unified one-line summary of a completed vehicle sync.

    mode_label: 'cached' | 'force' | 'smart->cached' | 'smart->force'
    source:     'bg-loop' | 'trips-auto' | 'manual' | 'settings'

    Reads the current daily API counter for the trailing api=N/200 suffix so
    the user can see budget consumption in the /logs feed without guessing.
    """
    try:
        from models.database import AppConfig
        counter = AppConfig.get('vehicle_api_counter', '0')
    except Exception:
        counter = '?'
    has_gps = getattr(status, 'location_lat', None) is not None
    logger.info(
        f"Vehicle sync [{mode_label}, src={source}]: "
        f"SoC={status.soc_percent}%, "
        f"odo={status.odometer_km}km, "
        f"GPS={'yes' if has_gps else 'no'}, "
        f"charging={bool(status.is_charging)}, "
        f"api={counter}/200"
    )


def _in_smart_window(hour: int, start_h: int, end_h: int) -> bool:
    """Active window check, handling the (unlikely) wrap-past-midnight case."""
    if start_h <= end_h:
        return start_h <= hour < end_h
    # e.g. 22..6 = active from 22:00 through 05:59
    return hour >= start_h or hour < end_h


def _compute_sleep_secs(app) -> tuple[int, bool]:
    """Return (sleep_seconds, should_sync_now).

    For smart mode, respects the configurable active window and
    fine-grained interval; outside the window we sleep until it opens
    again and report should_sync_now=False. For cached/force, keeps the
    hourly cadence of previous versions.
    """
    with app.app_context():
        from models.database import AppConfig
        mode = AppConfig.get('vehicle_sync_mode', 'cached')
        if mode == 'smart':
            try:
                start_h = int(AppConfig.get('smart_active_start_hour', str(DEFAULT_SMART_START_HOUR)))
                end_h = int(AppConfig.get('smart_active_end_hour', str(DEFAULT_SMART_END_HOUR)))
                interval_min = int(AppConfig.get('smart_active_interval_min', str(DEFAULT_SMART_INTERVAL_MIN)))
            except (ValueError, TypeError):
                start_h = DEFAULT_SMART_START_HOUR
                end_h = DEFAULT_SMART_END_HOUR
                interval_min = DEFAULT_SMART_INTERVAL_MIN
            interval_min = max(interval_min, MIN_SMART_INTERVAL_MIN)

            now = datetime.now()
            if _in_smart_window(now.hour, start_h, end_h):
                return (interval_min * 60, True)

            # Outside window: sleep until it opens. If start==end (disabled),
            # fall back to cached behavior.
            if start_h == end_h:
                return (interval_min * 60, True)
            target = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
            if start_h <= end_h and now.hour >= end_h:
                target += timedelta(days=1)
            elif start_h > end_h and now.hour >= end_h and now.hour < start_h:
                pass  # target is already today
            secs = int((target - now).total_seconds())
            return (max(secs, 60), False)

        # cached / force: hourly cadence
        try:
            hours = float(AppConfig.get('vehicle_sync_interval_hours', str(DEFAULT_INTERVAL_HOURS)))
        except (ValueError, TypeError):
            hours = DEFAULT_INTERVAL_HOURS
        hours = max(hours, MIN_INTERVAL_HOURS)
        return (int(hours * 3600), True)


def _sync_one_vehicle(app, vehicle):
    """Sync a single Vehicle row. Returns the persisted VehicleSync or None.

    Each vehicle carries its own credentials + API counter (legacy
    AppConfig key for Vehicle#1, ``vehicle_{id}_api_counter`` for the
    rest). Smart-mode decisions are scoped to that vehicle's own sync
    history so a Niro's GPS staleness doesn't trigger a force-refresh
    on a Skoda. ``request_force_refresh()`` still uses a global flag
    (set by the parking hook on whichever car just moved) and applies
    to every vehicle on the next tick — Phase 2.1 can scope it per
    vehicle once the parking hook reports vehicle_id.
    """
    from models.database import AppConfig, VehicleSync
    from services.vehicle import get_connector
    from datetime import date, datetime, timedelta as _td

    brand = (vehicle.api_brand or '').strip()
    if not brand:
        return None
    if not vehicle.api_username:
        return None  # creds incomplete; skip silently

    # Rate-limit counter: per-vehicle keys keep different Kia/Hyundai
    # accounts' 200/day budgets independent. Vehicle#1 keeps the legacy
    # key names so an in-flight counter survives the v2.29 upgrade.
    if vehicle.id == 1:
        cnt_date_key = 'vehicle_api_counter_date'
        cnt_key = 'vehicle_api_counter'
    else:
        cnt_date_key = f'vehicle_{vehicle.id}_api_counter_date'
        cnt_key = f'vehicle_{vehicle.id}_api_counter'
    today_str = date.today().isoformat()
    if AppConfig.get(cnt_date_key, '') != today_str:
        AppConfig.set(cnt_date_key, today_str)
        AppConfig.set(cnt_key, '0')
    try:
        api_count = int(AppConfig.get(cnt_key, '0'))
    except (TypeError, ValueError):
        api_count = 0
    if api_count >= 190:
        logger.warning(
            f"Vehicle sync [{vehicle.name}] skipped: daily API limit "
            f"reached ({api_count}/200)"
        )
        return None
    AppConfig.set(cnt_key, str(api_count + 1))

    creds = {
        'username': vehicle.api_username or '',
        'password': vehicle.api_password or '',
        'pin': vehicle.api_pin or '',
        'region': vehicle.api_region or 'EU',
        'vin': vehicle.api_vin or '',
    }

    # ── Determine effective force flag ──
    mode = AppConfig.get('vehicle_sync_mode', 'cached')
    force = (mode == 'force')
    mode_label = mode

    # Per-vehicle force-refresh queue. Look for an entry keyed on this
    # specific vehicle, or the wildcard None ("any vehicle"). Whichever
    # matches gets consumed.
    global _force_refresh_pending
    triggered_reason = None
    if vehicle.id in _force_refresh_pending:
        triggered_reason = _force_refresh_pending.pop(vehicle.id)
    elif None in _force_refresh_pending:
        triggered_reason = _force_refresh_pending.pop(None)
    if triggered_reason:
        force = True
        mode_label = f'triggered:{triggered_reason}'
        logger.info(
            f"Vehicle sync [{vehicle.name}]: triggered force-refresh "
            f"({triggered_reason})"
        )

    if not triggered_reason and mode == 'smart':
        mode_label = 'smart->cached'
        try:
            # Per-vehicle staleness: the last GPS / last sync queries
            # are scoped to this vehicle so a fleet doesn't share one
            # smart-window heuristic.
            last_with_gps = (VehicleSync.query
                             .filter(VehicleSync.vehicle_id == vehicle.id)
                             .filter(VehicleSync.location_lat.isnot(None))
                             .order_by(VehicleSync.timestamp.desc())
                             .first())
            last_sync = (VehicleSync.query
                         .filter(VehicleSync.vehicle_id == vehicle.id)
                         .order_by(VehicleSync.timestamp.desc())
                         .first())
            is_charging = bool(last_sync.is_charging) if last_sync else False
            try:
                max_hours = float(AppConfig.get('smart_force_max_hours', '6'))
            except (ValueError, TypeError):
                max_hours = 6.0
            stale = True
            if last_with_gps:
                age_hours = (datetime.now() - last_with_gps.timestamp).total_seconds() / 3600
                stale = (age_hours >= max_hours)
            if not is_charging and stale:
                force = True
                mode_label = 'smart->force'
                logger.info(
                    f"Vehicle sync [{vehicle.name}]: smart→force "
                    f"(gps stale, max_hours={max_hours}, charging={is_charging})"
                )
        except Exception as e:
            logger.warning(
                f"Vehicle sync [{vehicle.name}]: smart decision failed, using cached: {e}"
            )
            force = False
            mode_label = 'smart->cached'

    connector = get_connector(brand, creds)
    status = connector.get_status(force=force)

    if force:
        AppConfig.set('last_force_refresh_at', datetime.now().isoformat())

    from app import _save_vehicle_sync
    battery_kwh = float(vehicle.battery_kwh) if vehicle.battery_kwh else 64.0
    sync = _save_vehicle_sync(
        status, battery_kwh,
        raw_json=json.dumps(status.raw_data, default=str),
        vehicle_id=vehicle.id,
    )
    log_sync_result(status, mode_label=f'{vehicle.name}/{mode_label}', source='bg-loop')
    return sync


def _do_sync(app):
    """Fetch vehicle status for every active fleet vehicle.

    v2.29: iterates ``Vehicle.query.filter_by(is_archived=False,
    auto_sync=True).filter(api_brand IS NOT NULL).all()`` and runs the
    per-vehicle sync for each. Failures on one vehicle don't stop the
    others. Returns a list of saved syncs (or partial list on errors).
    """
    with app.app_context():
        from models.database import Vehicle
        targets = (Vehicle.query
                   .filter_by(is_archived=False, auto_sync=True)
                   .filter(Vehicle.api_brand.isnot(None))
                   .filter(Vehicle.api_username.isnot(None))
                   .order_by(Vehicle.id.asc())
                   .all())
        if not targets:
            return []
        results = []
        for v in targets:
            try:
                r = _sync_one_vehicle(app, v)
                if r is not None:
                    results.append(r)
            except Exception as e:
                logger.error(f"Vehicle sync [{v.name}] error: {e}")
        return results


def _maybe_post_move_reconcile(app) -> None:
    """If a PE transition queued a one-shot reconcile, run backfill +
    reconcile for today. Pure observability-free path — failures log a
    warning but never interrupt the sync loop. The flag self-clears so
    multiple transitions within one sync window coalesce into a single
    reconcile call."""
    global _post_move_reconcile_pending
    if not _post_move_reconcile_pending:
        return
    _post_move_reconcile_pending = False
    try:
        with app.app_context():
            from services.trip_reconcile import _brand_supports_trip_info
            if not _brand_supports_trip_info():
                return
            from services.vehicle.trip_log_fetch import backfill
            logger.info("Post-move trip reconcile: fetching today's SDK trips")
            r = backfill(days=1)
            total = sum(d.get('added', 0) + d.get('updated', 0) for d in r.get('results', []))
            logger.info(f"Post-move trip reconcile done: {total} SDK trip(s) touched")
    except Exception as e:
        logger.warning(f"Post-move reconcile failed: {e}")


def _maybe_daily_trip_reconcile(app) -> None:
    """Once per calendar day on brands that expose day_trip_info (Kia +
    Hyundai), pull the last 3 days of SDK trip-info and realign PE-pair
    ``departed_at`` against them. No-op on unsupported brands and on
    same-day re-entry. Wrapped wide: a reconcile failure must never
    take the sync loop down.
    """
    try:
        with app.app_context():
            from services.trip_reconcile import should_run_daily, reconcile_range
            from services.vehicle.trip_log_fetch import backfill
            if not should_run_daily():
                return
            logger.info("Daily trip reconcile: fetching last 3 days of SDK trips")
            backfill(days=3)  # populate today + yesterday + day-before
            r = reconcile_range(days=3)
            logger.info(
                f"Daily trip reconcile done: "
                f"dep={r.get('total_applied', 0)} "
                f"arr={r.get('total_arr_applied', 0)} "
                f"conflicts={r.get('total_conflicts', 0)}"
            )
    except Exception as e:
        logger.warning(f"Daily trip reconcile failed: {e}")


def _sync_loop(app):
    """Background loop that syncs at configured interval.

    Smart mode: fine-grained cadence (every 10min by default) during the
    active window, full stop at night. Cached/force: hourly cadence.
    """
    global _sync_running
    _sync_running = True
    logger.info("Vehicle sync service started")

    while _sync_running:
        sleep_secs, should_sync = _compute_sleep_secs(app)
        if should_sync:
            try:
                _do_sync(app)
            except Exception as e:
                logger.error(f"Vehicle sync error: {e}")
            # Daily trip reconcile no longer rides on the sync tick —
            # it has a dedicated 03:00 thread (_nightly_maintenance_loop)
            # so it fires at a predictable time instead of "whenever the
            # smart window opens".
            _maybe_post_move_reconcile(app)
        else:
            logger.info(
                f"Vehicle sync: outside smart-mode active window, "
                f"sleeping {sleep_secs // 60} min"
            )

        # Sleep in small increments so we can stop quickly and react to
        # a queued force-refresh request within ~10 seconds.
        slept = 0
        while slept < sleep_secs and _sync_running and not _force_refresh_pending and not _post_move_reconcile_pending:
            time.sleep(min(10, sleep_secs - slept))
            slept += 10

    _sync_running = False
    logger.info("Vehicle sync service stopped")


def _nightly_maintenance_loop(app):
    """Dedicated thread that fires SDK-backfill + PE-reconcile once per
    calendar day at ~03:00 local.

    Independent of the main sync loop's smart-window schedule so it
    runs regardless of when smart mode's active window starts. The
    brand gate inside :func:`_maybe_daily_trip_reconcile` ensures only
    brands with a day_trip_info endpoint (Kia + Hyundai) actually
    perform work; other brands no-op each wake-up.

    Startup catch-up: if we come up after 03:00 on a day where the
    reconcile hasn't run yet, fire once immediately instead of waiting
    until 03:00 the next morning.
    """
    global _sync_running

    # Startup catch-up
    try:
        with app.app_context():
            from services.trip_reconcile import should_run_daily
            if should_run_daily():
                logger.info("Nightly maintenance: startup catch-up")
                _maybe_daily_trip_reconcile(app)
    except Exception as e:
        logger.warning(f"Nightly maintenance startup catch-up failed: {e}")

    while _sync_running:
        now = datetime.now()
        target = now.replace(hour=NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        sleep_secs = int((target - now).total_seconds())
        slept = 0
        # Sleep in small increments so stop_sync() is responsive.
        while slept < sleep_secs and _sync_running:
            time.sleep(min(30, sleep_secs - slept))
            slept += 30
        if not _sync_running:
            break
        logger.info(f"Nightly maintenance fired at {datetime.now().isoformat(timespec='seconds')}")
        try:
            _maybe_daily_trip_reconcile(app)
        except Exception as e:
            logger.warning(f"Nightly maintenance error: {e}")

    logger.info("Nightly maintenance thread stopped")


def start_sync(app):
    """Start periodic vehicle sync in a background thread."""
    global _sync_thread, _nightly_thread, _sync_running

    if _sync_running:
        logger.info("Vehicle sync already running")
        return False

    with app.app_context():
        from models.database import AppConfig, Vehicle
        enabled = AppConfig.get('vehicle_sync_enabled', 'false')
        if enabled != 'true':
            return False
        # v2.29: require at least one non-archived vehicle with API
        # credentials and auto_sync set. Falls back to the legacy
        # AppConfig check so installs that haven't migrated their
        # AppConfig values onto the Vehicle row yet still start.
        ready = (Vehicle.query
                 .filter_by(is_archived=False, auto_sync=True)
                 .filter(Vehicle.api_brand.isnot(None))
                 .filter(Vehicle.api_username.isnot(None))
                 .first())
        legacy_brand = AppConfig.get('vehicle_api_brand', '')
        if ready is None and not legacy_brand:
            return False

    logger.info("Starting vehicle sync service")
    _sync_running = True  # set before threads start so they see True immediately
    _sync_thread = threading.Thread(target=_sync_loop, args=(app,), daemon=True)
    _sync_thread.start()
    _nightly_thread = threading.Thread(target=_nightly_maintenance_loop, args=(app,), daemon=True)
    _nightly_thread.start()
    return True


def stop_sync():
    """Stop the sync thread."""
    global _sync_running
    _sync_running = False


def is_running():
    """Check if sync is currently running."""
    return _sync_running
