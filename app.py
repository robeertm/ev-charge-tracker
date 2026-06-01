"""EV Charge Tracker - Main Flask Application."""
import io
import json
import os
import shutil
import subprocess
import sys
import logging
from datetime import datetime, date
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file

# Make stdout/stderr tolerant of Unicode (Windows cmd with legacy code pages
# would otherwise UnicodeEncodeError on our startup banner and emoji-heavy
# log lines). Python 3.7+ exposes reconfigure on TextIOWrapper.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from models.database import (
    db, Charge, AppConfig, ThgQuota, VehicleSync,
    ParkingEvent, MaintenanceEntry,
)
from config import Config, DATA_DIR
from services.i18n import t

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory log ring buffer for the /logs page
from services.log_service import install as _install_log_ring, set_request_logging as _set_req_log
_install_log_ring(level=logging.INFO)
# Werkzeug access logging defaults to OFF (user can toggle on /logs page)
_set_req_log(False)

# CarConnectivity's Skoda MQTT connector repeatedly logs
# "Could not connect (Not authorized)" at ERROR level when the MQTT
# token channel is blocked by MySkoda. We poll via HTTPS and do not
# rely on MQTT push, so the failures are harmless — silence them to
# keep the live-logs page readable.
logging.getLogger('carconnectivity.connectors.skoda.mqtt').setLevel(logging.CRITICAL)
logging.getLogger('carconnectivity.connectors.volkswagen.mqtt').setLevel(logging.CRITICAL)
logging.getLogger('carconnectivity.connectors.seatcupra.mqtt').setLevel(logging.CRITICAL)
logging.getLogger('carconnectivity.connectors.audi.mqtt').setLevel(logging.CRITICAL)


def create_app(config_class=Config):
    # ── Update safety net (must run BEFORE anything else) ─────────
    # If the previous boot was an update and we've already crashed N
    # times, this will restore the pre-update backup and os._exit(0)
    # so the supervisor picks up the rolled-back files. Must run
    # before db.create_all() — a broken migration is exactly the kind
    # of thing that should trigger a rollback.
    try:
        from services.update_service import pre_boot_rollback_check
        pre_boot_rollback_check()
    except SystemExit:
        raise
    except Exception as _e:
        # Never let the safety net itself bring the app down.
        logger.error(f'pre_boot_rollback_check failed (continuing): {_e}')

    app = Flask(__name__)
    app.config.from_object(config_class)
    db.init_app(app)
    # Permanent sessions: cookie survives browser restarts; stays valid 30 days.
    from datetime import timedelta as _td
    app.permanent_session_lifetime = _td(days=30)

    with app.app_context():
        db.create_all()
        # Migrate: add charge_hour column if missing
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('charges')]
        if 'charge_hour' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN charge_hour INTEGER'))
            db.session.commit()
        if 'odometer' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN odometer INTEGER'))
            db.session.commit()

        # Migrate: add extended history columns to vehicle_syncs
        sync_columns = [c['name'] for c in inspector.get_columns('vehicle_syncs')]
        _new_sync_cols = [
            ('battery_12v_percent', 'INTEGER'),
            ('battery_soh_percent', 'REAL'),
            ('total_regenerated_kwh', 'REAL'),
            ('regen_cumulative_kwh', 'REAL'),
            ('consumption_30d_kwh_per_100km', 'REAL'),
            ('location_lat', 'REAL'),
            ('location_lon', 'REAL'),
            # v2.28.11: ECU-side GPS timestamp for PE staleness filter.
            # Without this, Hyundai's cached "echo" of last-known GPS
            # pollutes the ParkingEvent state machine with phantom moves.
            ('location_last_updated_at', 'DATETIME'),
        ]
        for col_name, col_type in _new_sync_cols:
            if col_name not in sync_columns:
                db.session.execute(text(f'ALTER TABLE vehicle_syncs ADD COLUMN {col_name} {col_type}'))

        # Migrate: add location columns to charges (for Ladestationen-Memory)
        if 'location_lat' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN location_lat REAL'))
        if 'location_lon' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN location_lon REAL'))
        if 'location_name' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN location_name VARCHAR(200)'))
        # Migrate: add operator column to charges (Anbieter/CPO, v2.19.0)
        if 'operator' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN operator VARCHAR(64)'))

        # Migrate: add Zusatzkosten columns to charges (v2.28.59) — Startgebühr
        # (one-off Vorgangskosten / Grundgebühr-Anteil) and Blockiergebühr
        # (Strafgebühr für zu langes Stehen / Säulenblockade).
        if 'start_fee_eur' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN start_fee_eur REAL'))
        if 'blocking_fee_eur' not in columns:
            db.session.execute(text('ALTER TABLE charges ADD COLUMN blocking_fee_eur REAL'))

        # Migrate: needs_review flag (v3.0.18) — set on auto-detected
        # charges (car charged somewhere but the user forgot to start a
        # session in the app). The row is created from the is_charging
        # window so the user can correct/confirm it; the History view
        # highlights it red until edited+saved.
        if 'needs_review' not in columns:
            db.session.execute(text(
                'ALTER TABLE charges ADD COLUMN needs_review BOOLEAN DEFAULT 0'))

        # Migrate: add last_seen_at to parking_events
        try:
            parking_columns = [c['name'] for c in inspector.get_columns('parking_events')]
            if 'last_seen_at' not in parking_columns:
                db.session.execute(text('ALTER TABLE parking_events ADD COLUMN last_seen_at DATETIME'))
        except Exception:
            pass  # table might not exist yet on a fresh install — create_all() will handle it

        # Migrate: add raw_json to geocode_cache (v2.28.17, stores full
        # Nominatim response so short address format can evolve without
        # forcing another API round-trip)
        try:
            geocode_columns = [c['name'] for c in inspector.get_columns('geocode_cache')]
            if 'raw_json' not in geocode_columns:
                db.session.execute(text('ALTER TABLE geocode_cache ADD COLUMN raw_json TEXT'))
        except Exception:
            pass  # table might not exist yet on a fresh install

        db.session.commit()

        # Scale fix v1 (shipped in v2.5.4): the pre-v2.5.4 code stored
        # total_regenerated_kwh as raw/10, so v1 divided existing rows by 10.
        # That was not enough — the raw API value (Kia/Hyundai) is actually
        # in Wh, so the correct divisor is /1000 end-to-end. v2 applies the
        # remaining /10 on top of v1 to reach /100 total for pre-v2.5.4 rows
        # and /10 for v2.5.4 rows, so either path lands on raw/1000 kWh.
        if AppConfig.get('regen_scale_fix_v1', '') != 'done':
            db.session.execute(text(
                'UPDATE vehicle_syncs SET total_regenerated_kwh = total_regenerated_kwh / 10.0 '
                'WHERE total_regenerated_kwh IS NOT NULL'
            ))
            db.session.commit()
            AppConfig.set('regen_scale_fix_v1', 'done')
            logger.info("Applied regen scale fix v1: total_regenerated_kwh /= 10 on all rows")

        if AppConfig.get('regen_scale_fix_v2', '') != 'done':
            db.session.execute(text(
                'UPDATE vehicle_syncs SET total_regenerated_kwh = total_regenerated_kwh / 10.0 '
                'WHERE total_regenerated_kwh IS NOT NULL'
            ))
            # Wipe cumulative so the backfill below recomputes from the corrected series
            db.session.execute(text('UPDATE vehicle_syncs SET regen_cumulative_kwh = NULL'))
            db.session.commit()
            AppConfig.set('regen_scale_fix_v2', 'done')
            logger.info("Applied regen scale fix v2: final /10 to reach raw/1000 (Wh → kWh)")

        # Backfill regen_cumulative_kwh if any row is missing it.
        _missing = db.session.execute(text(
            'SELECT COUNT(*) FROM vehicle_syncs '
            'WHERE total_regenerated_kwh IS NOT NULL AND regen_cumulative_kwh IS NULL'
        )).scalar() or 0
        if _missing > 0:
            _backfill_regen_cumulative()
            logger.info(f"Backfilled regen_cumulative_kwh for {_missing} vehicle_syncs rows")

        # ── v2.29.0 multi-vehicle migration ──────────────────────────
        # Goal: every Charge / VehicleSync / ParkingEvent / VehicleTrip
        # / MaintenanceEntry row carries vehicle_id; legacy installs
        # get exactly one Vehicle#1 seeded from AppConfig and all rows
        # backfilled to it. Idempotent — safe to run on every boot.
        _vehicle_migrate_inspector = inspect(db.engine)
        _vehicle_child_tables = (
            'charges', 'vehicle_syncs', 'parking_events',
            'vehicle_trips', 'maintenance_log', 'thg_quotas',
        )
        for _t in _vehicle_child_tables:
            try:
                _cols = [c['name'] for c in _vehicle_migrate_inspector.get_columns(_t)]
            except Exception:
                continue  # table doesn't exist yet — create_all already handled fresh installs
            if 'vehicle_id' not in _cols:
                db.session.execute(text(f'ALTER TABLE {_t} ADD COLUMN vehicle_id INTEGER REFERENCES vehicles(id)'))
        db.session.commit()

        # Seed Vehicle#1 from AppConfig once. Subsequent boots skip
        # this branch entirely so adding/removing vehicles via the UI
        # doesn't get clobbered.
        from models.database import Vehicle as _VehicleModel
        if _VehicleModel.query.count() == 0:
            def _f(key, default=None):
                v = AppConfig.get(key, default)
                if v is None or v == '':
                    return None
                try:
                    return float(str(v).replace(',', '.'))
                except (ValueError, TypeError):
                    return None
            _seed = _VehicleModel(
                name=(AppConfig.get('car_model', '') or 'Mein Auto').strip()[:64] or 'Mein Auto',
                brand=(AppConfig.get('vehicle_api_brand', '') or '').strip().capitalize() or None,
                model=(AppConfig.get('car_model', '') or '').strip() or None,
                color='#0d6efd',  # Bootstrap primary; user can edit later
                battery_kwh=_f('battery_kwh', '64'),
                battery_soh_baseline=_f('battery_soh_baseline', '100'),
                battery_co2_per_kwh=_f('battery_co2_per_kwh'),
                max_ac_kw=_f('max_ac_kw', '11'),
                fossil_co2_per_km=_f('fossil_co2_per_km', '164'),
                recuperation_kwh_per_km=_f('recuperation_kwh_per_km', '0.086'),
                api_brand=(AppConfig.get('vehicle_api_brand', '') or '').strip() or None,
                api_username=(AppConfig.get('vehicle_api_username', '') or '').strip() or None,
                api_password=(AppConfig.get('vehicle_api_password', '') or '') or None,
                api_pin=(AppConfig.get('vehicle_api_pin', '') or '').strip() or None,
                api_region=(AppConfig.get('vehicle_api_region', '') or '').strip() or None,
                api_vin=(AppConfig.get('vehicle_api_vin', '') or '').strip() or None,
                auto_sync=True,
                is_archived=False,
            )
            db.session.add(_seed)
            db.session.commit()
            logger.info(f"v2.29 migration: seeded Vehicle#{_seed.id} '{_seed.name}' from AppConfig")
            # Backfill all existing rows to the new Vehicle#1 in one
            # pass per table. Doing this with raw SQL keeps the
            # migration fast even on installs with thousands of rows
            # and avoids loading every ORM object into memory.
            for _t in _vehicle_child_tables:
                try:
                    db.session.execute(
                        text(f'UPDATE {_t} SET vehicle_id = :vid WHERE vehicle_id IS NULL'),
                        {'vid': _seed.id},
                    )
                except Exception as _e:
                    logger.warning(f"v2.29 backfill on {_t} skipped: {_e}")
            db.session.commit()
            logger.info(f"v2.29 migration: backfilled vehicle_id={_seed.id} on all child tables")

        # v3.0 thg_quotas backfill — runs on every boot but no-ops once
        # filled. Needed for installs that already ran the v2.29 seed
        # block (so it won't fire again) but didn't yet have the
        # thg_quotas vehicle_id column.
        try:
            _primary = _VehicleModel.query.filter_by(is_archived=False).order_by(_VehicleModel.id.asc()).first()
            if _primary is not None:
                db.session.execute(
                    text('UPDATE thg_quotas SET vehicle_id = :vid WHERE vehicle_id IS NULL'),
                    {'vid': _primary.id},
                )
                db.session.commit()
        except Exception as _e:
            logger.warning(f"v3.0 thg_quotas backfill skipped: {_e}")

    register_routes(app)

    # Initialize i18n
    from services.i18n import init_app as init_i18n
    init_i18n(app)

    # Auto-backfill parking events from existing vehicle sync history if
    # the parking log is empty (e.g. fresh upgrade from a pre-fahrtenbuch
    # version, or a database where the hook didn't fire on most syncs).
    with app.app_context():
        try:
            from models.database import ParkingEvent, VehicleSync
            if ParkingEvent.query.count() == 0:
                if VehicleSync.query.filter(VehicleSync.location_lat.isnot(None)).count() > 0:
                    from services.trips_service import backfill_parking_events
                    summary = backfill_parking_events()
                    logger.info(f"Auto-backfilled parking events: {summary}")
        except Exception as e:
            logger.warning(f"Auto-backfill failed: {e}")

    # Set stable per-install session secret (for Flask signed session cookies).
    # Generated lazily on first call and persisted in AppConfig so sessions
    # survive restarts and updates.
    try:
        with app.app_context():
            from services.auth_service import get_or_create_session_secret
            app.secret_key = get_or_create_session_secret()
    except Exception as e:
        logger.warning(f"Session secret init failed, using Config.SECRET_KEY: {e}")
        app.secret_key = Config.SECRET_KEY

    # Auto-start vehicle sync if configured
    try:
        from services.vehicle.sync_service import start_sync
        start_sync(app)
    except Exception:
        pass

    # Background maintenance for legacy geocode cache entries (v2.28.18).
    # Trickle-converts pre-short-format addresses at ~1 per 2 s, backing
    # off to 60 s when Nominatim returns 429. Fire-and-forget.
    try:
        from services.geocode_service import start_address_maintenance
        start_address_maintenance(app)
    except Exception:
        pass

    # Honor persisted request-log toggle
    try:
        with app.app_context():
            if AppConfig.get('log_show_requests', 'false') == 'true':
                _set_req_log(True)
    except Exception:
        pass

    # v3.0.1: post-boot Tailscale kick. After a VM reboot tailscaled
    # often comes up before the LAN/DHCP is fully stable, ends up
    # cached on a stale path (DERP-only or no UPnP), and only
    # re-evaluates 30 min later on its own schedule. We trigger an
    # explicit ``tailscale netcheck`` 3× in the first 90 s after the
    # service starts so paths get re-discovered now, not after lunch.
    # All errors are swallowed so non-Tailscale deploys (Synology /
    # bare Pi without tailscale installed) ignore this silently.
    def _tailscale_kick():
        import time as _t
        import subprocess as _sp
        prev = 0
        for at_seconds in (15, 45, 90):
            _t.sleep(at_seconds - prev)
            prev = at_seconds
            try:
                _sp.run(['tailscale', 'netcheck'],
                        capture_output=True, timeout=20)
            except Exception:
                return  # tailscale not installed / not running — bail
    try:
        import threading as _th
        _th.Thread(target=_tailscale_kick, daemon=True,
                   name='tailscale-kick').start()
    except Exception:
        pass

    # v3.0.40: dynamic HTML pages must never be cached by the browser.
    # iOS Safari was holding onto a stale /input render for hours,
    # making the "tote Ladung" banner reappear after Verwerfen because
    # the user was looking at a cached page where the fix wasn't loaded
    # yet. no-store on text/html keeps dynamic state fresh — static
    # CSS/JS/images are unaffected.
    @app.after_request
    def _no_cache_dynamic_html(response):
        try:
            ct = (response.content_type or '').lower()
            if ct.startswith('text/html'):
                response.headers['Cache-Control'] = 'no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
        except Exception:
            pass
        return response

    return app


def _get_pv_co2():
    """Calculate PV CO2 in g/kWh from settings.

    If the user has explicitly zeroed the inputs (yield=0 or lifetime=0
    means "treat my PV as carbon-neutral"), return 0 — not the legacy
    42 g/kWh fallback. The 42 only fires when the values are missing or
    unparseable (truly absent), so a fresh install still gets a sane
    estimate while a deliberate zero stays zero.
    """
    try:
        yield_kwp = float(AppConfig.get('pv_yield_per_kwp', '950'))
        lifetime = float(AppConfig.get('pv_lifetime', '25'))
        prod_co2 = float(AppConfig.get('pv_production_co2', '1000'))
        if yield_kwp > 0 and lifetime > 0:
            return int(round(prod_co2 / (yield_kwp * lifetime)))
        # Explicit zero on yield or lifetime → user wants 0, not 42.
        return 0
    except (ValueError, TypeError):
        return 42  # truly unparseable — fall back


def _get_vehicle_credentials():
    """Build credentials dict from AppConfig for vehicle API."""
    return {
        'username': AppConfig.get('vehicle_api_username', ''),
        'password': AppConfig.get('vehicle_api_password', ''),
        'pin': AppConfig.get('vehicle_api_pin', ''),
        'region': AppConfig.get('vehicle_api_region', 'EU'),
        'vin': AppConfig.get('vehicle_api_vin', ''),
    }


def _vehicle_attr(vehicle_id, attr, fallback):
    """Generic helper: read a Vehicle field, falling back to a value
    when the vehicle row has it as None / missing / archived only.

    Resolution order:
      1. Explicit ``vehicle_id`` (int) → that vehicle's attr
      2. First non-archived vehicle's attr
      3. Any vehicle's attr (covers a fully-archived install)
      4. ``fallback`` value

    Used by ``_get_battery_kwh``, ``_get_max_ac_kw`` etc. so each
    helper has the same lookup semantics. ``fallback`` is the
    final defensive default when no vehicle row carries the field.
    """
    from models.database import Vehicle
    if isinstance(vehicle_id, int):
        v = Vehicle.query.get(vehicle_id)
        if v is not None and getattr(v, attr) is not None:
            return getattr(v, attr)
    v = (Vehicle.query.filter_by(is_archived=False)
         .order_by(Vehicle.id.asc()).first())
    if v is not None and getattr(v, attr) is not None:
        return getattr(v, attr)
    v = Vehicle.query.order_by(Vehicle.id.asc()).first()
    if v is not None and getattr(v, attr) is not None:
        return getattr(v, attr)
    return fallback


def _get_battery_kwh(vehicle_id=None):
    """Return battery capacity in kWh for the given (or active) vehicle.

    v2.29: prefers the Vehicle row's value; falls back to legacy
    AppConfig.battery_kwh for installs that still write there, and
    finally to the hard-coded build-time default.
    """
    val = _vehicle_attr(vehicle_id, 'battery_kwh', None)
    if val:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    raw = AppConfig.get('battery_kwh')
    try:
        return float(raw) if raw else Config.BATTERY_CAPACITY_KWH
    except (ValueError, TypeError):
        return Config.BATTERY_CAPACITY_KWH


def _get_charge_efficiency(vehicle_id=None):
    """Self-calibrating AC/PV charge efficiency for a vehicle.

    v3.0.15: median of (net ÷ gross) over the vehicle's recent *real*
    AC/PV charges — i.e. ones where the user entered a genuine metered
    ``kwh_loaded`` so the recorded loss is real (loss_pct in a sane
    2–40 % band). Used as the fallback when a new charge's loss would
    otherwise collapse to a false zero. 0.88 (≈12 % loss) when there's
    not enough history yet — matches the observed fleet average.
    """
    from models.database import Charge
    q = Charge.query.filter(
        Charge.charge_type.in_(('AC', 'PV')),
        Charge.loss_pct.isnot(None),
        Charge.loss_pct >= 2.0,
        Charge.loss_pct <= 40.0,
        # Un-reviewed auto-detected charges carry an *estimated* loss
        # (= 1 − this very efficiency); feeding them back in would let the
        # estimate drift. Only learn from reviewed/manual charges.
        Charge.needs_review.isnot(True),
    )
    if vehicle_id is not None:
        q = q.filter(Charge.vehicle_id == vehicle_id)
    rows = (q.order_by(Charge.date.desc()).limit(30).all())
    effs = []
    for c in rows:
        if c.loss_pct is not None and 0 < c.loss_pct < 100:
            effs.append(1.0 - c.loss_pct / 100.0)
    if len(effs) < 3:
        return 0.88
    effs.sort()
    mid = len(effs) // 2
    median = (effs[mid] if len(effs) % 2 == 1
              else (effs[mid - 1] + effs[mid]) / 2.0)
    # Clamp to a physically sane band so one outlier can't poison it.
    return max(0.70, min(0.97, round(median, 4)))


# Built-in Anbieter/CPO list — the common German + European operators.
# Users can add custom entries via /api/providers/custom which are stored
# in AppConfig as a JSON list and merged in.
# Default charge-operator dropdown. Real brand names are culture-neutral
# and stay as-is in every language. The three generic "labels" at the tail
# ("Home / private", "Work", "Other") are translated via `set.op_*` keys
# so an English user sees "Home / private" instead of "Zuhause / privat".
# `Stadtwerke` is left as-is because it's used in Germany as a near-brand
# tag (any of the many municipal utilities); translating to "Municipal
# utility" would be less recognisable than the German original.
_DEFAULT_OPERATOR_BRANDS = [
    'IONITY', 'EnBW mobility+', 'Aral pulse', 'Tesla Supercharger',
    'Shell Recharge', 'Allego', 'Fastned', 'Elli (VW)', 'EWE Go',
    'Maingau EinfachStromLaden', 'Lidl', 'Kaufland', 'Aldi Süd',
    'REWE', 'Mer', 'Stadtwerke',
]
_DEFAULT_OPERATOR_GENERICS = ['op_home_private', 'op_work', 'op_other']


def get_default_operators():
    """Built-in charge-operator list, localised to the user's current
    language. Returned fresh on every call because the app language
    can change at runtime via the settings page."""
    from services.i18n import t as _t
    return (_DEFAULT_OPERATOR_BRANDS
            + [_t(f'set.{key}') for key in _DEFAULT_OPERATOR_GENERICS])


# Kept for backwards-compat in case any downstream code still expects a
# list constant. Callers that need the live-localised view should use
# `get_default_operators()` instead.
DEFAULT_OPERATORS = _DEFAULT_OPERATOR_BRANDS


def _get_custom_operators():
    """Parse the stored custom-operators JSON into a Python list. Returns
    an empty list on any kind of corruption so we never brick the UI."""
    import json as _json
    try:
        raw = AppConfig.get('custom_operators', '[]') or '[]'
        custom = _json.loads(raw)
        if not isinstance(custom, list):
            return []
        return [str(n).strip() for n in custom if str(n).strip()]
    except (ValueError, TypeError):
        return []


def _get_custom_operators_text():
    """Render the stored custom-operators list as newline-separated text
    for the settings textarea."""
    return '\n'.join(_get_custom_operators())


def _get_operator_prices():
    """Return the {operator_name: eur_per_kwh_float} map used by the
    charge-input form to auto-fill the price field when the user picks a
    known provider. Unset/blank prices are omitted so the UI knows not
    to override what the user typed."""
    import json as _json
    try:
        raw = AppConfig.get('operator_prices', '{}') or '{}'
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out = {}
        for name, price in data.items():
            try:
                p = float(price)
                if p > 0:
                    out[str(name).strip()] = round(p, 4)
            except (TypeError, ValueError):
                continue
        return out
    except (ValueError, TypeError):
        return {}


def _active_vehicle_id():
    """Read the user's "currently focused" vehicle from the Flask session.

    Returns:
      - 'all' (str) when the fleet-wide aggregate view is selected
      - int when a specific vehicle is selected
      - the first non-archived vehicle's id when nothing is set yet,
        falling back to vehicle_id=1 (which the v2.29 migration always
        creates), or None if no vehicle exists at all.

    The picker stores its choice in `session['vehicle_filter']` so it
    persists across requests on the same browser without forcing a
    server-wide setting (multi-user installs may want different views
    per user later).
    """
    from flask import session
    from models.database import Vehicle
    raw = session.get('vehicle_filter')
    if raw == 'all':
        return 'all'
    if raw is not None:
        try:
            vid = int(raw)
            if Vehicle.query.get(vid) is not None:
                return vid
        except (ValueError, TypeError):
            pass
    # No selection yet — default to the first non-archived vehicle.
    v = (Vehicle.query.filter_by(is_archived=False)
         .order_by(Vehicle.id.asc()).first())
    if v is not None:
        return v.id
    fallback = Vehicle.query.order_by(Vehicle.id.asc()).first()
    return fallback.id if fallback else None


def _resolved_vehicle_filter():
    """Returns (vehicle_id_or_None_for_all, vehicle_or_None).

    Helper for routes that need both the filter int and the actual
    Vehicle object (e.g. dashboard header showing "Robert's Niro").
    Returns (None, None) for fleet view; (id, Vehicle) for specific.
    """
    from models.database import Vehicle
    raw = _active_vehicle_id()
    if raw == 'all':
        return None, None
    if raw is None:
        return None, None
    return raw, Vehicle.query.get(raw)


def _thg_quotas_for_picker():
    """Return ThgQuota rows scoped to the currently picked vehicle.

    'all' fleet view → every quota across the fleet.
    Specific vehicle → only that vehicle's quotas.
    Single-vehicle install (no picker yet) → behaves like 'specific'
    on the only vehicle, so the table is never wrongly cross-mixed.
    """
    raw = _active_vehicle_id()
    q = ThgQuota.query.order_by(ThgQuota.year_from)
    if isinstance(raw, int):
        q = q.filter(ThgQuota.vehicle_id == raw)
    return q.all()


def _get_operator_monthly_fees():
    """Return {operator_name: monthly_eur} for the contract Grundgebühren
    column on the operator settings page. Same shape/lookup semantics
    as _get_operator_prices; unset/blank fees drop out."""
    import json as _json
    try:
        raw = AppConfig.get('operator_monthly_fees', '{}') or '{}'
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out = {}
        for name, fee in data.items():
            try:
                f = float(fee)
                if f > 0:
                    out[str(name).strip()] = round(f, 2)
            except (TypeError, ValueError):
                continue
        return out
    except (ValueError, TypeError):
        return {}


def _get_last_rollback():
    """Read LAST_ROLLBACK.json if present so the settings page can
    surface a banner explaining the auto-rollback to the user. Returns
    None when nothing happened (common case)."""
    try:
        from services.update_service import read_last_rollback
        return read_last_rollback()
    except Exception:
        return None


def _json_logical_field_labels_for_ui():
    """Return {logical_field: user-visible label} for the CSV-import
    preview UI dropdowns. Keys come from import_gsheet's FIELD_ALIASES."""
    from import_gsheet import ALL_LOGICAL_FIELDS
    return {f: t(f'set.field_{f}') for f in ALL_LOGICAL_FIELDS}


def _get_operator_list(selected=None):
    """Operators for the charge-form dropdown.

    v3.0.15 behaviour (per user request):
      * The localised home / work generics ("Zuhause", "Arbeit") are
        pinned to the TOP as favourites — they're the everyday choices.
      * Third-party branded operators are HIDDEN unless they have a
        configured €/kWh price in settings (you shouldn't be able to
        pick a provider whose price isn't maintained — it would silently
        record a €0 charge). The localised generics + "Sonstiges" are
        always kept (they're location categories, legitimately €0 e.g.
        PV at home). The currently-selected operator (edit of an old
        charge) is also always kept so it never silently vanishes.
    """
    from services.i18n import t as _t
    prices = _get_operator_prices()  # only entries with price > 0
    generics = [_t(f'set.{k}') for k in _DEFAULT_OPERATOR_GENERICS]
    generic_lc = {g.lower() for g in generics}
    fav_lc = {_t('set.op_home_private').lower(), _t('set.op_work').lower()}
    sel_lc = (selected or '').strip().lower()

    seen = set()
    favs, generics_rest, priced = [], [], []
    for name in get_default_operators() + _get_custom_operators():
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        is_generic = key in generic_lc
        keep = is_generic or (name in prices) or (sel_lc and key == sel_lc)
        if not keep:
            continue
        if key in fav_lc:
            favs.append(name)
        elif is_generic:
            generics_rest.append(name)
        else:
            priced.append(name)
    return favs + generics_rest + priced


def _calc_soh_percent(status, battery_kwh):
    """Compute SoH%. Prefer native value from API; otherwise derive from total
    consumed energy via the agreed formula: (consumed/10) / 1000 / battery_kwh * 100."""
    if status.battery_soh_percent is not None:
        try:
            return round(float(status.battery_soh_percent), 1)
        except (ValueError, TypeError):
            pass
    if status.total_power_consumed_kwh is not None and battery_kwh:
        try:
            consumed = float(status.total_power_consumed_kwh) / 10.0
            return round(consumed / 1000.0 / float(battery_kwh) * 100.0, 1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    return None


def _backfill_regen_cumulative():
    """Walk vehicle_syncs in timestamp order and (re)compute regen_cumulative_kwh.

    The raw total_regenerated_kwh is a **rolling 3-month window** (Kia/Hyundai).
    To get a monotonic lifetime-ish value we sum positive deltas between syncs
    and skip negative deltas (which mark the moment a month rolls off the window).
    First data point anchors the cumulative at 0 — so the result is
    'measured regen since tracking started', not lifetime regen.
    """
    rows = VehicleSync.query.order_by(VehicleSync.timestamp.asc()).all()
    cum = 0.0
    last_raw = None
    for r in rows:
        raw = r.total_regenerated_kwh
        if raw is None:
            # Preserve existing non-null cumulative (from previous rows)
            if last_raw is not None:
                r.regen_cumulative_kwh = round(cum, 2)
            continue
        if last_raw is not None and raw >= last_raw:
            cum += (raw - last_raw)
        # else: first reading → anchor, or rollover → skip
        last_raw = raw
        r.regen_cumulative_kwh = round(cum, 2)
    db.session.commit()


def _extract_location_last_updated(status, raw_json):
    """Return the ECU-side GPS timestamp as a naive local datetime, or None.

    Preferred source is ``status.location_last_updated_at`` which the SDK
    already parses (tz-aware or naive datetime). Falls back to the raw
    payload's ``data.vehicleLocation.time`` (``YYYYMMDDHHMMSS`` format,
    local time per the payload's ``offset`` field) when the SDK attribute
    is absent or unparsable. Kia UVO's response sometimes omits the field
    entirely when the car is deep-sleeping — we return None in that case
    and the parking-event hook treats the sync like "no usable GPS".
    """
    ts = getattr(status, 'location_last_updated_at', None)
    if isinstance(ts, datetime):
        # Strip tzinfo — all downstream datetimes in this app are naive local.
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    # Fallback: parse raw JSON
    try:
        import json as _json
        raw = _json.loads(raw_json) if raw_json else {}
        t = (((raw.get('data') or {}).get('vehicleLocation') or {}).get('time') or '')
        if len(t) >= 14:
            return datetime.strptime(t[:14], '%Y%m%d%H%M%S')
    except (ValueError, KeyError, AttributeError, TypeError):
        pass
    return None


def _build_vehicle_sync(status, battery_kwh, raw_json='', vehicle_id=None):
    """Build a VehicleSync row from a connector VehicleStatus.

    ``vehicle_id`` (v2.29) stamps the row so multi-vehicle fleets keep
    each car's syncs scoped — None for legacy single-car installs.
    """
    regen_kwh = None
    if status.total_power_regenerated_kwh is not None:
        try:
            # Raw API value (Kia/Hyundai) is in Wh for a rolling 3-month window.
            # Divide by 1000 to get kWh.
            regen_kwh = round(float(status.total_power_regenerated_kwh) / 1000.0, 2)
        except (ValueError, TypeError):
            pass
    cons_30d = None
    if status.consumption_30d_wh_per_km is not None:
        try:
            # API returns Wh/km in 0.1 units → divide by 10 → kWh/100km
            cons_30d = round(float(status.consumption_30d_wh_per_km) / 10.0, 1)
        except (ValueError, TypeError):
            pass
    return VehicleSync(
        vehicle_id=vehicle_id,
        soc_percent=status.soc_percent,
        odometer_km=status.odometer_km,
        is_charging=status.is_charging,
        charge_power_kw=status.charge_power_kw,
        estimated_range_km=status.estimated_range_km,
        battery_12v_percent=status.battery_12v_percent,
        battery_soh_percent=_calc_soh_percent(status, battery_kwh),
        total_regenerated_kwh=regen_kwh,
        consumption_30d_kwh_per_100km=cons_30d,
        location_lat=status.location_lat,
        location_lon=status.location_lon,
        location_last_updated_at=_extract_location_last_updated(status, raw_json),
        raw_json=raw_json,
    )


def _save_vehicle_sync(status, battery_kwh, raw_json='', vehicle_id=None):
    """Persist a VehicleSync row only if any tracked value differs from the
    most recent row. Returns the saved (or last existing) sync row.

    The parking-event hook is **always** invoked on the latest snapshot
    (whether freshly persisted or the existing one). This catches two
    important edge cases:
      1. A previous-session sync had GPS data but the parking hook wasn't
         installed yet (older versions of the app).
      2. A force-refresh delivers identical GPS coords to the existing row
         (car hasn't moved), which would otherwise skip the hook entirely.
    """
    new_sync = _build_vehicle_sync(status, battery_kwh, raw_json=raw_json,
                                   vehicle_id=vehicle_id)
    # v2.29: scope the "previous sync" lookup per-vehicle so the
    # regen-cumulative delta-walk (and the differs_from de-dup check)
    # don't mix data across cars in a fleet.
    _last_q = VehicleSync.query
    if vehicle_id is not None:
        _last_q = _last_q.filter(VehicleSync.vehicle_id == vehicle_id)
    last = _last_q.order_by(VehicleSync.timestamp.desc()).first()
    # v3.0.18: remember the previous charging state so we can detect a
    # charge-end transition (1→0) after the new row is persisted.
    _prev_is_charging = bool(last.is_charging) if last is not None else False

    # Compute monotonic cumulative regen from the rolling 3-month raw value
    if new_sync.total_regenerated_kwh is not None:
        if last is not None and last.total_regenerated_kwh is not None:
            prev_cum = last.regen_cumulative_kwh or 0.0
            prev_raw = last.total_regenerated_kwh
            new_raw = new_sync.total_regenerated_kwh
            delta = (new_raw - prev_raw) if new_raw >= prev_raw else 0.0
            new_sync.regen_cumulative_kwh = round(prev_cum + delta, 2)
        else:
            # First ever reading — anchor at 0
            new_sync.regen_cumulative_kwh = 0.0

    if new_sync.differs_from(last):
        db.session.add(new_sync)
        db.session.commit()
        result = new_sync
    else:
        result = last

    # Retroactive GPS backfill: when the sync we just saved carries a
    # GPS fix whose own timestamp (``location_last_updated_at``) is in
    # the past — common on Hyundai Bluelink after a sleep window where
    # the cloud stored the last-known-good position and replays it on
    # the next poll — stamp that position onto any earlier GPS-less
    # syncs within ± 5 min of the fix time. This fills in the
    # Fahrtenbuch for periods the cloud was too slow to report GPS
    # live. Tight window: we don't attribute a stationary fix to syncs
    # that fell during an in-progress drive.
    try:
        if result.location_lat is not None and result.location_last_updated_at is not None:
            from datetime import timedelta
            fix_ts = result.location_last_updated_at
            window = timedelta(minutes=5)
            # v2.29: per-vehicle backfill — never stamp one car's fix
            # onto another car's GPS-less syncs.
            _bf_q = (VehicleSync.query
                     .filter(VehicleSync.id != result.id)
                     .filter(VehicleSync.location_lat.is_(None))
                     .filter(VehicleSync.timestamp >= fix_ts - window)
                     .filter(VehicleSync.timestamp <= fix_ts + window))
            if result.vehicle_id is not None:
                _bf_q = _bf_q.filter(VehicleSync.vehicle_id == result.vehicle_id)
            affected = _bf_q.all()
            if affected:
                for r in affected:
                    r.location_lat = result.location_lat
                    r.location_lon = result.location_lon
                    r.location_last_updated_at = fix_ts
                db.session.commit()
                logger.info(
                    f"GPS backfill: stamped fix @ {fix_ts.isoformat()} onto "
                    f"{len(affected)} earlier GPS-less sync(s)"
                )
    except Exception as e:
        logger.warning(f"GPS backfill failed: {e}")

    # Always run parking detection on the latest snapshot.
    try:
        from services.trips_service import update_parking_from_sync
        update_parking_from_sync(result)
    except Exception as e:
        logger.warning(f"Failed to update parking event: {e}")

    # v3.0.18: auto-detect a charge the user forgot to log. When charging
    # just ended (is_charging 1→0) and SoC rose by the threshold over the
    # session, create a needs_review Charge from the is_charging window.
    try:
        if (result is not None and result is new_sync
                and _prev_is_charging and not result.is_charging):
            _detect_auto_charge(result)
    except Exception as e:
        logger.warning(f"Auto-charge detection failed: {e}")

    return result


# Minimum SoC gain over a charging session before we auto-create a
# charge. Below this it's almost always the car recalibrating its SoC
# estimate while parked (which never sets is_charging anyway, so this is
# a belt-and-braces second guard).
_AUTO_CHARGE_MIN_SOC_GAIN = 3
# A single physical charge often reports several is_charging 1→0 blips
# (the cloud briefly says "not charging" mid-session), each firing the
# detector with a fresh, non-overlapping SoC slice. We fold a new slice
# into a recent un-reviewed auto charge when its SoC range touches the
# existing one (within this %) AND it resumes within the time window —
# i.e. the same session continuing, not a different forgotten charge.
_AUTO_CHARGE_SOC_MERGE_GAP = 5        # %
_AUTO_CHARGE_MERGE_WINDOW_MIN = 90    # minutes

# Hard cap on a single Charge's kwh_loaded: battery_kwh × this. Covers a
# full 0→100 % charge plus worst-case AC losses (efficiency 0.80 → 1.25)
# with a small margin for meter variance. Anything above is a typo
# (e.g. 333 kWh on a 64 kWh car) and gets rejected on save.
_CHARGE_KWH_HARD_CAP_MULTIPLIER = 1.3


def _detect_auto_charge(end_sync):
    """Reconstruct a charge from the just-closed is_charging window and
    insert it flagged needs_review, unless the user already logged one
    that overlaps. Called only on a charge-end (1→0) transition."""
    vid = end_sync.vehicle_id
    # Walk back over the contiguous is_charging=1 run that just ended to
    # find where charging began (and the SoC just before it started).
    q = VehicleSync.query
    if vid is not None:
        q = q.filter(VehicleSync.vehicle_id == vid)
    recent = (q.filter(VehicleSync.timestamp <= end_sync.timestamp)
              .order_by(VehicleSync.timestamp.desc())
              .limit(200).all())
    # recent[0] == end_sync (is_charging False). Skip it, then collect the
    # leading block of is_charging=True rows.
    charging_rows = []
    pre_charge_row = None
    for r in recent[1:]:
        if r.is_charging:
            charging_rows.append(r)
        else:
            pre_charge_row = r  # first non-charging row before the run
            break
    if not charging_rows:
        return
    start_row = charging_rows[-1]   # first sync of the charging run
    # SoC at charge start: prefer the pre-charge row (true starting SoC),
    # else the first charging sample.
    soc_from = (pre_charge_row.soc_percent if pre_charge_row
                and pre_charge_row.soc_percent is not None
                else start_row.soc_percent)
    # v3.0.32: guard against the BlueLink/UVO cloud occasionally echoing
    # the charge-START SoC on the very first is_charging=0 response. Real
    # report from 2026-05-30: a 50→82 % home charge produced
    # end_sync.soc=50 instead of 82, the next stable sync arrived 8 min
    # later with soc=82 but no longer carried a 1→0 transition, so the
    # detector silently missed the whole charge. Floor soc_to at the
    # highest SoC seen during the charging run so an echo can't sink it
    # below the value the car already physically reached.
    charge_top_soc = max((r.soc_percent for r in charging_rows
                          if r.soc_percent is not None),
                         default=None)
    soc_to = end_sync.soc_percent
    if charge_top_soc is not None and (soc_to is None or charge_top_soc > soc_to):
        soc_to = charge_top_soc
    if soc_from is None or soc_to is None:
        return
    if (soc_to - soc_from) < _AUTO_CHARGE_MIN_SOC_GAIN:
        return

    start_ts = start_row.timestamp
    charge_date = start_ts.date()

    bk = _get_battery_kwh(vehicle_id=vid)
    eff = _get_charge_efficiency(vehicle_id=vid)

    # Charge type from observed power if available, else AC (home/dest).
    powers = [r.charge_power_kw for r in charging_rows
              if r.charge_power_kw is not None]
    max_kw = max(powers) if powers else None
    charge_type = 'DC' if (max_kw is not None and max_kw > 25) else 'AC'

    # Reconcile with existing same-day charges. A single physical charge
    # can fire this detector several times (is_charging blips), each with a
    # different, non-overlapping SoC slice — so plain overlap-dedup would
    # create one row per blip (the v3.0.18 triple-event bug). Instead:
    #   • overlap with a reviewed/manual charge → bail, the user owns it
    #   • SoC-contiguous + time-close to an un-reviewed auto charge
    #     → extend that one row rather than adding another
    existing = Charge.query.filter(
        Charge.vehicle_id == vid,
        Charge.date == charge_date,
    ).all()
    merge_target = None
    for c in existing:
        if c.soc_from is None or c.soc_to is None:
            # Same-day charge without SoC bounds — be conservative, skip.
            return
        overlaps = not (c.soc_to <= soc_from or c.soc_from >= soc_to)
        if not c.needs_review:
            # Reviewed/manually-logged charge wins — never touch it.
            if overlaps:
                return
            continue
        # Un-reviewed auto charge: same session resuming?
        soc_touch = (soc_from <= c.soc_to + _AUTO_CHARGE_SOC_MERGE_GAP and
                     soc_to >= c.soc_from - _AUTO_CHARGE_SOC_MERGE_GAP)
        gap_min = (abs((start_ts - c.created_at).total_seconds()) / 60.0
                   if c.created_at else 1e9)
        if soc_touch and gap_min <= _AUTO_CHARGE_MERGE_WINDOW_MIN:
            merge_target = c
            break

    # Location + operator/price from the start sync's GPS.
    lat = start_row.location_lat or end_sync.location_lat
    lon = start_row.location_lon or end_sync.location_lon
    op = None
    eur = None
    loc_name = None
    if lat is not None and lon is not None:
        from services.trips_service import _classify_location
        label, fav_name = _classify_location(lat, lon)
        prices = _get_operator_prices()
        if label == 'home':
            op = AppConfig.get('home_label', 'Home') or 'Home'
            loc_name = op
        elif label == 'work':
            op = AppConfig.get('work_label', 'Work') or 'Work'
            loc_name = op
        elif label == 'favorite' and fav_name:
            loc_name = fav_name
        # Map to a configured operator price where the name matches.
        from services.i18n import t as _t
        if label == 'home':
            op = _t('set.op_home_private')
        elif label == 'work':
            op = _t('set.op_work')
        if op and op in prices:
            eur = prices[op]

    # CO2 fallback to the vehicle's most recent non-PV charge.
    fb = (Charge.query.filter(Charge.co2_g_per_kwh.isnot(None),
                              Charge.charge_type != 'PV',
                              Charge.vehicle_id == vid)
          .order_by(Charge.date.desc()).first())
    co2 = fb.co2_g_per_kwh if fb else None

    def _gross_for(span_from, span_to, ctype):
        n = (span_to - span_from) / 100.0 * bk
        return round(n, 2) if ctype == 'DC' else round(n / eff, 2)

    if merge_target is not None:
        # Extend the session to the union of both SoC windows and recompute
        # everything from the full span. Reset loss_kwh so calculate_fields
        # re-derives it from the measured efficiency rather than freezing a
        # stale per-fragment value (the cause of the bogus 6 % loss).
        new_from = min(merge_target.soc_from, soc_from)
        new_to = max(merge_target.soc_to, soc_to)
        if charge_type == 'DC':
            merge_target.charge_type = 'DC'
        merge_target.soc_from = new_from
        merge_target.soc_to = new_to
        merge_target.kwh_loaded = _gross_for(new_from, new_to,
                                             merge_target.charge_type)
        merge_target.loss_kwh = None          # force re-derive over full span
        merge_target.odometer = end_sync.odometer_km
        merge_target.created_at = end_sync.timestamp   # advance merge anchor
        if loc_name and not merge_target.location_name:
            merge_target.location_name = loc_name
            merge_target.operator = merge_target.operator or op
            merge_target.eur_per_kwh = merge_target.eur_per_kwh or eur
        if co2 is not None and merge_target.co2_g_per_kwh is None:
            merge_target.co2_g_per_kwh = co2
        merge_target.calculate_fields(bk, eff)
        db.session.commit()
        logger.info(
            f"Auto-charge merged into id={merge_target.id}: now "
            f"{new_from}->{new_to}% {merge_target.kwh_loaded}kWh (needs_review)"
        )
        return

    gross = _gross_for(soc_from, soc_to, charge_type)
    c = Charge(
        vehicle_id=vid, date=charge_date, charge_hour=start_ts.hour,
        charge_type=charge_type, soc_from=soc_from, soc_to=soc_to,
        kwh_loaded=gross, eur_per_kwh=eur, operator=op,
        odometer=end_sync.odometer_km,
        location_lat=lat, location_lon=lon, location_name=loc_name,
        co2_g_per_kwh=co2, needs_review=True, created_at=end_sync.timestamp,
        notes='Automatisch erkannt (keine App-Session) — bitte prüfen',
    )
    c.calculate_fields(bk, eff)
    db.session.add(c)
    db.session.commit()
    logger.info(
        f"Auto-detected charge id={c.id}: {charge_type} {soc_from}->{soc_to}% "
        f"{gross}kWh @ {loc_name or 'unknown'} (needs_review)"
    )


def register_routes(app):

    # ── FIRST-RUN SETUP WIZARD (VM deployments) ───────────────
    # When ev-provision finishes on a fresh VM it drops /srv/ev-data/.setup_pending.
    # The hook below redirects non-setup requests to the /setup wizard until
    # the end user has changed the temporary LUKS passphrase — so they never
    # have to SSH in and run cryptsetup manually.
    _SETUP_ALLOWED_PREFIXES = ('/setup', '/api/setup/', '/static/', '/api/health')

    @app.before_request
    def _setup_guard():
        from services.setup_service import is_setup_pending
        if not is_setup_pending():
            return None
        path = request.path or '/'
        if any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
            return None
        # Browser navigation → redirect to wizard. Non-GET (API calls etc)
        # get a clean 503 JSON so they can handle it programmatically.
        if request.method == 'GET':
            return redirect(url_for('setup_wizard'))
        return jsonify({'error': 'setup_pending', 'redirect': '/setup'}), 503

    @app.route('/setup', methods=['GET'])
    def setup_wizard():
        from services.setup_service import is_setup_pending, get_luks_device, load_state
        if not is_setup_pending():
            return redirect(url_for('dashboard'))
        return render_template(
            'setup.html',
            luks_device=get_luks_device() or '(unknown)',
            setup_state=load_state(),
            app_version=Config.APP_VERSION,
        )

    @app.route('/api/setup/change_luks', methods=['POST'])
    def api_setup_change_luks():
        from services.setup_service import (
            is_setup_pending, change_luks_passphrase, mark_step_done,
        )
        if not is_setup_pending():
            return jsonify({'error': 'setup_not_pending'}), 400

        data = request.get_json(silent=True) or {}
        old_pass = (data.get('old_passphrase') or '').strip()
        new_pass = (data.get('new_passphrase') or '').strip()
        new_pass_confirm = (data.get('new_passphrase_confirm') or '').strip()

        if new_pass != new_pass_confirm:
            return jsonify({'error': t('err.passphrase_mismatch')}), 400

        ok, msg = change_luks_passphrase(old_pass, new_pass)
        if not ok:
            return jsonify({'error': msg}), 400

        mark_step_done('luks_done')
        return jsonify({'ok': True, 'message': msg})

    @app.route('/api/setup/create_web_login', methods=['POST'])
    def api_setup_create_web_login():
        """Wizard step 2: create the web-UI login and enable the auth guard.

        Replaces the pre-v2.14 step that shelled out to `chpasswd` to change
        the `ev-tracker` Unix password. That was the wrong shape of secret —
        end users never SSH into the VM anyway, and changing the Unix
        password locked the admin out of maintenance access. The web login
        is the credential the end user actually needs, and it lives in the
        app's own DB (hashed via Werkzeug).
        """
        from services.setup_service import (
            is_setup_pending, mark_step_done, load_state, complete_setup,
        )
        from services.auth_service import set_credentials, login_user
        if not is_setup_pending():
            return jsonify({'error': 'setup_not_pending'}), 400

        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        confirm = data.get('password_confirm') or ''

        if not username:
            return jsonify({'error': t('err.username_empty')}), 400
        if password != confirm:
            return jsonify({'error': t('err.new_password_mismatch')}), 400

        try:
            set_credentials(username, password)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        # Log the user in immediately so they don't land on the login page
        # right after finishing setup.
        login_user(username)

        mark_step_done('weblogin_done')

        # v2.29: a 3rd "Fahrzeuge anlegen" step now follows so the
        # user populates the fleet before the wizard exits. The
        # marker / state file is cleared by api_setup_save_vehicles
        # when the third step finishes; here we just return a hint
        # so the frontend advances to the next step instead of
        # navigating to /.
        return jsonify({'ok': True, 'message': t('msg.web_login_created'), 'next_step': 'vehicles'})

    @app.route('/api/setup/save_vehicles', methods=['POST'])
    def api_setup_save_vehicles():
        """Wizard step 3: register at least one vehicle.

        Body: JSON {"vehicles": [{name, brand, model, battery_kwh,
        max_ac_kw}, ...]}. Each entry must carry a non-empty name; the
        rest is optional and can be filled in later via Settings →
        Fahrzeuge. Empty payload is rejected so the wizard cannot be
        skipped.
        """
        from services.setup_service import (
            is_setup_pending, mark_step_done, load_state, complete_setup,
        )
        from models.database import Vehicle
        if not is_setup_pending():
            return jsonify({'error': 'setup_not_pending'}), 400

        data = request.get_json(silent=True) or {}
        rows = data.get('vehicles') or []
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = (row.get('name') or '').strip()[:64]
            if not name:
                continue
            cleaned.append({
                'name': name,
                'brand': (row.get('brand') or '').strip()[:32] or None,
                'model': (row.get('model') or '').strip()[:64] or None,
                'battery_kwh': _float(row.get('battery_kwh')),
                'max_ac_kw': _float(row.get('max_ac_kw')),
            })
        if not cleaned:
            return jsonify({'error': t('err.setup_no_vehicle')}), 400

        # Wipe the placeholder Vehicle#1 if it's still the migration
        # default and has no data attached — fresh install path. Keeps
        # the user-entered list as the only fleet members.
        from models.database import (
            Charge as _Charge, VehicleSync as _VehicleSync,
            ParkingEvent as _ParkingEvent, VehicleTrip as _VehicleTrip,
            MaintenanceEntry as _MaintenanceEntry,
        )
        placeholder = (Vehicle.query
                       .order_by(Vehicle.id.asc()).first())
        if placeholder is not None and not placeholder.battery_kwh and not placeholder.api_brand:
            in_use = (
                _Charge.query.filter_by(vehicle_id=placeholder.id).count()
                + _VehicleSync.query.filter_by(vehicle_id=placeholder.id).count()
                + _ParkingEvent.query.filter_by(vehicle_id=placeholder.id).count()
                + _VehicleTrip.query.filter_by(vehicle_id=placeholder.id).count()
                + _MaintenanceEntry.query.filter_by(vehicle_id=placeholder.id).count()
            )
            if in_use == 0:
                db.session.delete(placeholder)
                db.session.commit()

        for cv in cleaned:
            v = Vehicle(name=cv['name'], brand=cv['brand'], model=cv['model'],
                        battery_kwh=cv['battery_kwh'], max_ac_kw=cv['max_ac_kw'],
                        is_archived=False, auto_sync=True, color='#0d6efd')
            db.session.add(v)
        db.session.commit()
        # Mirror the FIRST vehicle's basics onto the legacy AppConfig
        # keys so the sync_service + stats helpers (which haven't been
        # vehicle-aware'd yet) see the right defaults out of the gate.
        first = (Vehicle.query.filter_by(is_archived=False)
                 .order_by(Vehicle.id.asc()).first())
        if first is not None:
            if first.battery_kwh:
                AppConfig.set('battery_kwh', str(first.battery_kwh))
            if first.max_ac_kw:
                AppConfig.set('max_ac_kw', str(first.max_ac_kw))
            if first.name:
                AppConfig.set('car_model', first.name)

        mark_step_done('vehicles_done')
        # All three steps done → exit the wizard.
        state = load_state()
        if (state.get('luks_done') and state.get('weblogin_done')
                and state.get('vehicles_done')):
            complete_setup()
        return jsonify({'ok': True, 'count': len(cleaned), 'redirect': '/'})

    @app.route('/api/health', methods=['GET'])
    def api_health():
        return jsonify({'ok': True, 'version': Config.APP_VERSION})

    @app.route('/api/sync/health', methods=['GET'])
    def api_sync_health():
        """Watchdog snapshot for the dashboard badge.

        Status calculation:
          green  — bg-loop ticked recently, no recent timeout
          yellow — bg-loop overdue (active window) or a recent timeout
          red    — last force_refresh hung past the 120 s threshold and
                   no successful tick since
        """
        from services.vehicle.sync_service import (
            get_bg_loop_health, is_12v_low, _latest_12v_percent,
            LOW_12V_THRESHOLD_PERCENT,
        )
        from datetime import datetime as _dt, timedelta as _td

        bg = get_bg_loop_health()
        # 12 V lockout signal for the active vehicle.
        _picker_h = _active_vehicle_id()
        _vid_h = _picker_h if isinstance(_picker_h, int) else None
        if _vid_h is None:
            from models.database import Vehicle as _Vh
            _vh = (_Vh.query.filter_by(is_archived=False)
                   .order_by(_Vh.id.asc()).first())
            _vid_h = _vh.id if _vh else None
        battery_12v = _latest_12v_percent(_vid_h) if _vid_h else None
        low_12v_lockout = bool(_vid_h and is_12v_low(_vid_h))
        try:
            from services.vehicle.connector_hyundai_kia import get_force_refresh_health
            fr = get_force_refresh_health()
        except Exception:
            fr = {'last_attempt_at': None, 'last_outcome': None,
                  'last_outcome_at': None, 'last_error': None,
                  'timeout_count_24h': 0, 'timeout_threshold_sec': 120}

        now = _dt.now()
        status = 'green'
        reason = ''
        last_tick = bg.get('last_tick_at')
        # Bg-loop tick freshness check — smart window samples every 10
        # min by default; we allow 30 min of grace before yellow.
        if last_tick is None:
            status = 'yellow'
            reason = 'bg_loop_not_yet_ticked'
        else:
            age_min = (now - last_tick).total_seconds() / 60
            if age_min > 60:
                status = 'red'
                reason = f'bg_loop_silent_for_{int(age_min)}min'
            elif age_min > 30:
                status = 'yellow'
                reason = f'bg_loop_overdue_{int(age_min)}min'

        # Recent timeout escalates the badge (overrides green).
        if fr.get('last_outcome') == 'timeout' and fr.get('last_outcome_at'):
            to_age = (now - fr['last_outcome_at']).total_seconds() / 60
            if to_age < 30 and status == 'green':
                status = 'yellow'
                reason = f'force_refresh_timed_out_{int(to_age)}min_ago'

        # 12 V lockout escalates to yellow (and overrides green) so the
        # user sees the dashboard chip change colour without having to
        # open the trips page.
        if low_12v_lockout and status == 'green':
            status = 'yellow'
            reason = f'low_12v_lockout_{battery_12v}pct'

        return jsonify({
            'status': status,
            'reason': reason,
            'bg_loop': {
                'running': bg.get('running'),
                'last_tick_at': last_tick.isoformat() if last_tick else None,
                'last_outcome': bg.get('last_outcome'),
                'age_minutes': (
                    int((now - last_tick).total_seconds() / 60)
                    if last_tick else None
                ),
            },
            'force_refresh': {
                'last_attempt_at': (
                    fr['last_attempt_at'].isoformat()
                    if fr.get('last_attempt_at') else None
                ),
                'last_outcome': fr.get('last_outcome'),
                'last_outcome_at': (
                    fr['last_outcome_at'].isoformat()
                    if fr.get('last_outcome_at') else None
                ),
                'last_error': fr.get('last_error'),
                'timeout_count_24h': fr.get('timeout_count_24h', 0),
                'timeout_threshold_sec': fr.get('timeout_threshold_sec', 120),
            },
            'battery_12v': battery_12v,
            'low_12v_lockout': low_12v_lockout,
            'low_12v_threshold': LOW_12V_THRESHOLD_PERCENT,
        })

    # ── LUKS auto-unlock (v3.0.3) ─────────────────────────────
    # Lets the user opt out of typing the LUKS passphrase on every
    # reboot. A small root-owned wrapper (/usr/local/bin/ev-luks-
    # autounlock) verifies the passphrase against the LUKS volume and
    # stores it at /etc/ev-tracker/luks-keyfile (mode 0400, root-only)
    # so ev-unlock-web can use it on the next boot. The trade-off is
    # explicit in the UI: anyone with disk access can read the
    # passphrase, so this is for trusted-network deployments only.

    def _luks_autounlock_call(action, passphrase=None):
        import subprocess as _sp
        cmd = ['sudo', '-n', '/usr/local/bin/ev-luks-autounlock', action]
        try:
            r = _sp.run(
                cmd,
                input=(passphrase.encode('utf-8') if passphrase else None),
                capture_output=True,
                timeout=20,
            )
        except FileNotFoundError:
            return False, 'helper_missing', ''
        except Exception as e:
            return False, str(e), ''
        out = (r.stdout or b'').decode(errors='replace').strip()
        err = (r.stderr or b'').decode(errors='replace').strip()
        return r.returncode == 0, err, out

    @app.route('/api/luks/auto-unlock/status', methods=['GET'])
    def api_luks_autounlock_status():
        ok, _err, out = _luks_autounlock_call('status')
        if not ok:
            return jsonify({'available': False, 'enabled': False})
        return jsonify({'available': True, 'enabled': out == 'enabled'})

    @app.route('/api/luks/auto-unlock/enable', methods=['POST'])
    def api_luks_autounlock_enable():
        data = request.get_json(silent=True) or {}
        passphrase = (data.get('passphrase') or '').strip()
        if not passphrase:
            return jsonify({'ok': False, 'error': t('err.passphrase_empty')}), 400
        ok, err, _out = _luks_autounlock_call('enable', passphrase=passphrase)
        if not ok:
            low = (err or '').lower()
            if 'rejected' in low or 'no key' in low:
                msg = t('err.current_password_wrong')
            elif 'helper_missing' in low:
                msg = t('err.luks_helper_missing')
            else:
                msg = err or 'unknown'
            return jsonify({'ok': False, 'error': msg}), 400
        return jsonify({'ok': True})

    @app.route('/api/luks/auto-unlock/disable', methods=['POST'])
    def api_luks_autounlock_disable():
        ok, err, _out = _luks_autounlock_call('disable')
        if not ok:
            return jsonify({'ok': False, 'error': err or 'unknown'}), 400
        return jsonify({'ok': True})

    @app.route('/api/luks/change-passphrase', methods=['POST'])
    def api_luks_change_passphrase():
        """Change the LUKS passphrase on the running data volume.

        If auto-unlock was enabled, the stored keyfile gets refreshed
        with the new passphrase after a successful change — otherwise
        the next reboot would fail to auto-unlock with the stale key.
        """
        from services.setup_service import change_luks_passphrase
        data = request.get_json(silent=True) or {}
        old_pass = (data.get('old_passphrase') or '').strip()
        new_pass = (data.get('new_passphrase') or '').strip()
        new_pass_confirm = (data.get('new_passphrase_confirm') or '').strip()
        if not old_pass or not new_pass:
            return jsonify({'ok': False, 'error': t('err.passphrase_empty')}), 400
        if new_pass != new_pass_confirm:
            return jsonify({'ok': False, 'error': t('err.passphrase_mismatch')}), 400
        if len(new_pass) < 6:
            return jsonify({'ok': False, 'error': t('err.passphrase_too_short')}), 400
        ok, msg = change_luks_passphrase(old_pass, new_pass)
        if not ok:
            return jsonify({'ok': False, 'error': msg}), 400
        # If auto-unlock was on, refresh the stored keyfile with the new
        # passphrase. Failure here is loud (would otherwise leave the
        # stored key stale → next boot can't auto-unlock).
        au_ok, _err, au_out = _luks_autounlock_call('status')
        if au_ok and au_out == 'enabled':
            re_ok, re_err, _ = _luks_autounlock_call('enable', passphrase=new_pass)
            if not re_ok:
                return jsonify({
                    'ok': True,
                    'warning': t('flash.luks_changed_autounlock_stale', error=re_err),
                })
        return jsonify({'ok': True, 'message': t('flash.luks_changed')})

    # ── AUTH GUARD (optional web-UI login) ────────────────────
    # Opt-in password gate in front of the app. Owner enables it in
    # Settings → Zugangsschutz; credentials live in AppConfig (hashed).
    _AUTH_ALLOWED_PREFIXES = ('/login', '/logout', '/static/', '/api/health',
                              '/setup', '/api/setup/')

    @app.before_request
    def _auth_guard():
        from services.auth_service import is_auth_enabled, is_logged_in
        if not is_auth_enabled():
            return None
        if is_logged_in():
            return None
        path = request.path or '/'
        if any(path.startswith(p) for p in _AUTH_ALLOWED_PREFIXES):
            return None
        if request.method == 'GET':
            return redirect(url_for('login_page', next=path))
        return jsonify({'error': 'auth_required'}), 401

    @app.route('/login', methods=['GET', 'POST'])
    def login_page():
        from services.auth_service import (
            is_auth_enabled, verify_credentials, login_user,
        )
        if not is_auth_enabled():
            return redirect(url_for('dashboard'))

        error = None
        if request.method == 'POST':
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''
            if verify_credentials(username, password):
                login_user(username)
                nxt = request.args.get('next') or request.form.get('next') or '/'
                # Prevent open-redirect: only allow relative paths
                if not nxt.startswith('/') or nxt.startswith('//'):
                    nxt = '/'
                return redirect(nxt)
            error = t('login.error')
        return render_template(
            'login.html',
            error=error,
            next_url=request.args.get('next', '/'),
        )

    @app.route('/logout', methods=['GET', 'POST'])
    def logout_page():
        from services.auth_service import logout_user
        logout_user()
        return redirect(url_for('login_page'))

    # ── AUTH ADMIN API (called from Settings page) ────────────
    @app.route('/api/auth/enable', methods=['POST'])
    def api_auth_enable():
        from services.auth_service import set_credentials, login_user
        data = request.get_json(silent=True) or request.form
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        confirm = data.get('password_confirm') or ''
        if password != confirm:
            return jsonify({'error': t('err.password_mismatch')}), 400
        try:
            set_credentials(username, password)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        # Log the owner in immediately so they don't lock themselves out.
        login_user(username)
        return jsonify({'ok': True, 'message': t('msg.auth_enabled')})

    @app.route('/api/auth/disable', methods=['POST'])
    def api_auth_disable():
        from services.auth_service import is_logged_in, disable_auth
        # Only an authenticated session may disable auth — this prevents a
        # drive-by POST on an exposed instance from turning the gate off.
        if not is_logged_in():
            return jsonify({'error': 'nicht eingeloggt'}), 401
        disable_auth()
        return jsonify({'ok': True, 'message': t('msg.auth_disabled')})

    @app.route('/api/auth/change_password', methods=['POST'])
    def api_auth_change_password():
        from services.auth_service import (
            is_logged_in, verify_credentials, get_username, set_credentials,
        )
        if not is_logged_in():
            return jsonify({'error': 'nicht eingeloggt'}), 401
        data = request.get_json(silent=True) or request.form
        current = data.get('current_password') or ''
        new_pw = data.get('new_password') or ''
        confirm = data.get('new_password_confirm') or ''
        if new_pw != confirm:
            return jsonify({'error': t('err.new_password_mismatch')}), 400
        username = get_username()
        if not verify_credentials(username, current):
            return jsonify({'error': t('err.current_password_wrong')}), 400
        try:
            set_credentials(username, new_pw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'ok': True, 'message': t('msg.password_changed')})

    # ── BACKUP & RESTORE (DB export / import) ────────────────
    # Exports and imports the SQLite DB at data/ev_tracker.db. Contains
    # everything: charges, syncs, settings, auth credentials, THG quotas,
    # maintenance entries, cached data — the full app state in one file.
    # Importing REPLACES the current DB and restarts the service so the
    # SQLAlchemy engine picks up the new file cleanly.
    @app.route('/api/backup/export', methods=['GET'])
    def api_backup_export():
        from datetime import datetime as _dt
        from sqlalchemy import text as _text
        db_path = Path(DATA_DIR) / 'ev_tracker.db'
        if not db_path.is_file():
            return jsonify({'error': t('err.db_file_not_found')}), 404
        # Force SQLite to flush any pending writes so the export file is
        # consistent. checkpoint is cheap for an idle DB.
        try:
            db.session.execute(_text('PRAGMA wal_checkpoint(TRUNCATE)'))
            db.session.commit()
        except Exception:
            pass
        ts = _dt.now().strftime('%Y%m%d-%H%M%S')
        return send_file(
            str(db_path),
            mimetype='application/x-sqlite3',
            as_attachment=True,
            download_name=f'ev-tracker-backup-{ts}.db',
        )

    @app.route('/api/backup/import', methods=['POST'])
    def api_backup_import():
        """Replace data/ev_tracker.db with an uploaded backup.

        Validates the upload as a real SQLite DB with the tables the app
        expects before touching anything live. On success, schedules a
        systemd-driven restart so SQLAlchemy picks up the new file from
        scratch — simpler than trying to dispose the engine at runtime.
        """
        import sqlite3 as _sqlite3
        import tempfile as _tempfile
        from datetime import datetime as _dt

        up = request.files.get('backup')
        if not up or not up.filename:
            return jsonify({'error': t('err.no_file_uploaded')}), 400

        tmp = _tempfile.NamedTemporaryFile(
            prefix='ev-tracker-import-', suffix='.db', delete=False
        )
        try:
            up.save(tmp.name)
            tmp.close()

            # Validate: must be a SQLite DB with our core tables.
            try:
                conn = _sqlite3.connect(tmp.name)
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {row[0] for row in cur.fetchall()}
                conn.close()
            except _sqlite3.DatabaseError:
                os.unlink(tmp.name)
                return jsonify({
                    'error': t('err.not_valid_sqlite')
                }), 400

            required = {'charges', 'app_config', 'vehicle_syncs'}
            missing = required - tables
            if missing:
                os.unlink(tmp.name)
                return jsonify({
                    'error': (
                        'Datenbank-Struktur passt nicht zu ev-charge-tracker. '
                        f'Fehlende Tabellen: {", ".join(sorted(missing))}'
                    )
                }), 400

            # Safety backup of the current DB before overwriting, so a bad
            # import can be manually reverted from the file system.
            db_path = Path(DATA_DIR) / 'ev_tracker.db'
            backup_dir = Path(DATA_DIR) / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            if db_path.is_file():
                backup_name = f'ev_tracker-pre-import-{_dt.now().strftime("%Y%m%d-%H%M%S")}.db'
                shutil.copy2(db_path, backup_dir / backup_name)

            # Close any open DB handles from SQLAlchemy before replacing
            # the file — on POSIX an open fd keeps the old inode alive so
            # the running process would still read from the old DB.
            try:
                db.session.close()
                db.engine.dispose()
            except Exception:
                pass

            shutil.copy2(tmp.name, db_path)
            os.unlink(tmp.name)

            # Schedule a systemd restart a few hundred ms into the future
            # so the HTTP response has time to flush.
            def _delayed_restart():
                import time as _t
                _t.sleep(0.5)
                try:
                    subprocess.run(
                        ['sudo', '-n', '/bin/systemctl', 'restart', 'ev-tracker.service'],
                        timeout=10,
                    )
                except Exception:
                    pass
            import threading as _th
            _th.Thread(target=_delayed_restart, daemon=True).start()

            return jsonify({
                'ok': True,
                'message': 'Backup erfolgreich importiert. App startet neu …',
            })
        except Exception as e:
            logger.error(f"Backup import failed: {e}")
            try:
                if Path(tmp.name).exists():
                    os.unlink(tmp.name)
            except Exception:
                pass
            return jsonify({'error': t('err.import_failed', error=str(e))}), 500

    # ── Notify settings (ntfy.sh reboot alerts) ──────────────────
    # Config lives outside the encrypted volume at /var/lib/ev-tracker/notify.json
    # so the unlock-web helper can read it before LUKS is opened.
    @app.route('/api/settings/notify', methods=['GET', 'POST'])
    def api_settings_notify():
        from services import notify_service
        if request.method == 'GET':
            cfg = notify_service.load()
            return jsonify({'ok': True, **cfg})
        data = request.get_json(silent=True) or {}
        try:
            path = notify_service.save(
                enabled=data.get('enabled', False),
                topic=data.get('topic', ''),
                server=data.get('server', ''),
            )
            return jsonify({'ok': True, 'path': str(path)})
        except Exception as e:
            logger.error(f"Notify save failed: {e}")
            return jsonify({'error': str(e)}), 500

    # ── System updates (unattended-upgrades security only) ─────
    @app.route('/api/system/updates/status', methods=['GET'])
    def api_system_updates_status():
        from services import system_update_service
        try:
            return jsonify({'ok': True, **system_update_service.get_status()})
        except Exception as e:
            logger.error(f"System update status failed: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/system/updates/apply', methods=['POST'])
    def api_system_updates_apply():
        from services import system_update_service
        if not system_update_service.unattended_upgrades_available():
            return jsonify({'error': 'unattended-upgrades ist auf diesem System nicht installiert.'}), 400
        started = system_update_service.start_apply()
        if not started:
            return jsonify({'error': t('err.update_job_running')}), 409
        return jsonify({'ok': True, 'message': t('msg.security_updates_started')})

    @app.route('/api/system/reboot', methods=['POST'])
    def api_system_reboot():
        from services import system_update_service
        ok, msg = system_update_service.schedule_reboot(delay_seconds=5)
        if not ok:
            return jsonify({'error': msg}), 500
        return jsonify({'ok': True, 'message': msg})

    @app.route('/api/settings/notify/test', methods=['POST'])
    def api_settings_notify_test():
        from services import notify_service
        data = request.get_json(silent=True) or {}
        topic = (data.get('topic') or '').strip()
        server = (data.get('server') or '').strip() or 'https://ntfy.sh'
        if not topic:
            return jsonify({'error': t('err.topic_missing')}), 400
        ok, info = notify_service.send(
            topic=topic,
            server=server,
            message='Test: EV Charge Tracker Benachrichtigung funktioniert.',
            title='EV Charge Tracker',
        )
        if ok:
            return jsonify({'ok': True})
        return jsonify({'error': info}), 502

    @app.context_processor
    def inject_globals():
        # v2.29 fleet picker — drives the navbar dropdown so the user
        # can flip between vehicles or the "Alle Flotten-Daten" view
        # from any page without leaving it.
        from models.database import Vehicle
        all_vehicles = Vehicle.query.order_by(
            Vehicle.is_archived.asc(), Vehicle.id.asc()
        ).all()
        active_vid = _active_vehicle_id()  # 'all' | int | None
        active_vehicle = None
        if isinstance(active_vid, int):
            active_vehicle = Vehicle.query.get(active_vid)

        # THG reminder: Jan 1 – Mar 31, scoped per-vehicle in v3.0. In
        # fleet view we warn if ANY non-archived vehicle is missing a
        # quota for the previous year; in single-vehicle view we only
        # check the picked vehicle. Reminder fires once and the alert
        # links to /settings#sec-thg where the user must pick the
        # correct vehicle anyway, so we don't carry a vehicle name in
        # the banner — keeps the message short.
        thg_reminder = None
        today = date.today()
        if today.month <= 3:
            prev_year = today.year - 1
            if active_vid == 'all':
                _check_vids = [v.id for v in all_vehicles if not v.is_archived]
            elif isinstance(active_vid, int):
                _check_vids = [active_vid]
            else:
                _check_vids = []
            for _vid in _check_vids:
                _existing = ThgQuota.query.filter(
                    ThgQuota.vehicle_id == _vid,
                    ThgQuota.year_from <= prev_year,
                    ThgQuota.year_to >= prev_year,
                ).first()
                if not _existing:
                    thg_reminder = prev_year
                    break
        return {
            'app_version': Config.APP_VERSION,
            'car_model': (
                active_vehicle.name if active_vehicle and active_vehicle.name
                else AppConfig.get('car_model', Config.CAR_MODEL)
            ),
            'current_year': today.year,
            'thg_reminder_year': thg_reminder,
            'fleet_vehicles': all_vehicles,
            'active_vehicle_filter': active_vid,  # 'all' or int or None
            'active_vehicle': active_vehicle,
        }

    # ── v2.29 Fleet CRUD ──────────────────────────────────────
    @app.route('/vehicles/save', methods=['POST'])
    def vehicles_save():
        """Create or update a Vehicle from the Settings → Fahrzeuge form.

        Hidden ``vehicle_id`` field decides: empty → new, otherwise
        update existing. Redirects back to the Fahrzeuge section so
        the user sees the new state.
        """
        from models.database import Vehicle
        vid = _int(request.form.get('vehicle_id'))
        if vid:
            v = Vehicle.query.get_or_404(vid)
        else:
            v = Vehicle()
        v.name = (request.form.get('name', '') or '').strip()[:64]
        if not v.name:
            flash(t('flash.vehicle_name_required'), 'danger')
            return redirect('/settings#sec-fleet')
        v.brand = (request.form.get('brand', '') or '').strip() or None
        v.model = (request.form.get('model', '') or '').strip() or None
        v.color = (request.form.get('color', '') or '').strip() or None
        v.battery_kwh = _float(request.form.get('battery_kwh'))
        v.battery_soh_baseline = _float(request.form.get('battery_soh_baseline'))
        v.battery_co2_per_kwh = _float(request.form.get('battery_co2_per_kwh'))
        v.max_ac_kw = _float(request.form.get('max_ac_kw'))
        v.fossil_co2_per_km = _float(request.form.get('fossil_co2_per_km'))
        v.recuperation_kwh_per_km = _float(request.form.get('recuperation_kwh_per_km'))
        v.api_brand = (request.form.get('api_brand', '') or '').strip().lower() or None
        v.api_username = (request.form.get('api_username', '') or '').strip() or None
        # Password: keep existing when blank (so editing other fields
        # doesn't clear it). New entry with empty password = None.
        new_pw = request.form.get('api_password', '')
        if new_pw:
            v.api_password = new_pw
        elif not vid:
            v.api_password = None
        v.api_pin = (request.form.get('api_pin', '') or '').strip() or None
        v.api_region = (request.form.get('api_region', '') or '').strip().upper() or None
        v.api_vin = (request.form.get('api_vin', '') or '').strip().upper() or None
        v.auto_sync = request.form.get('auto_sync') == '1'
        try:
            ad = request.form.get('acquired_at', '').strip()
            v.acquired_at = datetime.strptime(ad, '%Y-%m-%d').date() if ad else None
        except ValueError:
            v.acquired_at = None
        try:
            rd = request.form.get('retired_at', '').strip()
            v.retired_at = datetime.strptime(rd, '%Y-%m-%d').date() if rd else None
        except ValueError:
            v.retired_at = None
        v.notes = (request.form.get('notes', '') or '').strip() or None
        if not vid:
            db.session.add(v)
        db.session.commit()
        # v2.29 legacy mirror: stats services + sync_service still read
        # AppConfig.battery_kwh / battery_soh_baseline / etc. directly.
        # Mirror the primary vehicle (lowest id, non-archived) onto
        # AppConfig so single-vehicle installs continue to render the
        # same numbers after every save. Phase 2 will refactor the
        # readers to be vehicle-aware and we can drop this mirror.
        from models.database import Vehicle as _V
        primary = (_V.query.filter_by(is_archived=False)
                   .order_by(_V.id.asc()).first()
                   or _V.query.order_by(_V.id.asc()).first())
        if primary is not None and primary.id == v.id:
            def _mirror(key, val):
                AppConfig.set(key, '' if val is None else str(val))
            _mirror('car_model', v.name)
            _mirror('battery_kwh', v.battery_kwh)
            _mirror('battery_soh_baseline', v.battery_soh_baseline)
            _mirror('battery_co2_per_kwh', v.battery_co2_per_kwh)
            _mirror('max_ac_kw', v.max_ac_kw)
            _mirror('fossil_co2_per_km', v.fossil_co2_per_km)
            _mirror('recuperation_kwh_per_km', v.recuperation_kwh_per_km)
            # API creds also mirror so the legacy sync_service finds
            # them. Password stays only when explicitly entered.
            _mirror('vehicle_api_brand', v.api_brand or '')
            _mirror('vehicle_api_username', v.api_username or '')
            if v.api_password:
                _mirror('vehicle_api_password', v.api_password)
            _mirror('vehicle_api_pin', v.api_pin or '')
            _mirror('vehicle_api_region', v.api_region or '')
            _mirror('vehicle_api_vin', v.api_vin or '')
        flash(t('flash.vehicle_saved'), 'success')
        return redirect('/settings#sec-fleet')

    @app.route('/vehicles/<int:vid>/archive', methods=['POST'])
    def vehicles_archive(vid):
        """Toggle the ``is_archived`` flag. Archived vehicles drop out
        of the active picker but their history stays in fleet aggregates."""
        from models.database import Vehicle
        v = Vehicle.query.get_or_404(vid)
        v.is_archived = not v.is_archived
        if v.is_archived and not v.retired_at:
            v.retired_at = date.today()
        db.session.commit()
        flash(t('flash.vehicle_archived' if v.is_archived else 'flash.vehicle_restored'), 'success')
        return redirect('/settings#sec-fleet')

    @app.route('/vehicles/<int:vid>/test', methods=['POST'])
    def vehicles_test(vid):
        """Probe a vehicle's API credentials without persisting anything.
        Returns flash with success/error so the user can sanity-check
        their setup right from the Fahrzeuge table."""
        from models.database import Vehicle
        v = Vehicle.query.get_or_404(vid)
        if not (v.api_brand and v.api_username):
            flash(t('flash.vehicle_test_no_creds'), 'warning')
            return redirect('/settings#sec-fleet')
        try:
            from services.vehicle import get_connector
            creds = {
                'username': v.api_username or '',
                'password': v.api_password or '',
                'pin': v.api_pin or '',
                'region': v.api_region or 'EU',
                'vin': v.api_vin or '',
            }
            connector = get_connector(v.api_brand.lower(), creds)
            connector._ensure_auth()
            flash(t('flash.vehicle_test_ok', name=v.name), 'success')
        except Exception as e:
            flash(t('flash.vehicle_test_failed', name=v.name, error=str(e)), 'danger')
        return redirect('/settings#sec-fleet')

    @app.route('/vehicles/<int:vid>/sync', methods=['POST'])
    def vehicles_sync(vid):
        """Manual one-shot sync for a single vehicle. Same path as the
        background loop's _sync_one_vehicle, just on demand."""
        from models.database import Vehicle
        v = Vehicle.query.get_or_404(vid)
        if not (v.api_brand and v.api_username):
            flash(t('flash.vehicle_sync_no_creds'), 'warning')
            return redirect('/settings#sec-fleet')
        try:
            from services.vehicle.sync_service import _sync_one_vehicle
            sync = _sync_one_vehicle(app, v)
            if sync is None:
                flash(t('flash.vehicle_sync_no_data', name=v.name), 'warning')
            else:
                parts = []
                if sync.soc_percent is not None:
                    parts.append(f'SoC {sync.soc_percent}%')
                if sync.odometer_km is not None:
                    parts.append(f'{sync.odometer_km:,} km')
                summary = ' · '.join(parts) if parts else 'OK'
                flash(t('flash.vehicle_sync_ok', name=v.name, summary=summary), 'success')
        except Exception as e:
            flash(t('flash.vehicle_sync_failed', name=v.name, error=str(e)), 'danger')
        return redirect('/settings#sec-fleet')

    @app.route('/vehicles/<int:vid>/delete', methods=['POST'])
    def vehicles_delete(vid):
        """Hard-delete a vehicle. Refused when any Charge / VehicleSync
        / ParkingEvent / VehicleTrip / MaintenanceEntry still references
        it — those rows would orphan otherwise. The user must reassign
        or delete the data first (a future "merge" tool can automate
        that, but for now we keep it strict)."""
        from models.database import (
            Vehicle, Charge, VehicleSync, ParkingEvent, VehicleTrip, MaintenanceEntry,
        )
        v = Vehicle.query.get_or_404(vid)
        in_use = (
            Charge.query.filter_by(vehicle_id=vid).count()
            + VehicleSync.query.filter_by(vehicle_id=vid).count()
            + ParkingEvent.query.filter_by(vehicle_id=vid).count()
            + VehicleTrip.query.filter_by(vehicle_id=vid).count()
            + MaintenanceEntry.query.filter_by(vehicle_id=vid).count()
        )
        if in_use > 0:
            flash(t('flash.vehicle_has_data',
                    default=f'Fahrzeug "{v.name}" hat noch {in_use} Datensätze und kann nicht gelöscht werden — erst archivieren'),
                  'danger')
            return redirect('/settings#sec-fleet')
        db.session.delete(v)
        db.session.commit()
        flash(t('flash.vehicle_deleted'), 'warning')
        return redirect('/settings#sec-fleet')

    @app.route('/api/vehicles/select', methods=['POST'])
    def api_vehicles_select():
        """Persist the user's vehicle-picker choice in the Flask session.

        Body: JSON {"vehicle_id": "all" | int}. Returns the resolved
        choice so the navbar can update without a page reload.
        """
        from flask import session
        from models.database import Vehicle
        data = request.get_json(silent=True) or {}
        raw = data.get('vehicle_id')
        if raw == 'all':
            session['vehicle_filter'] = 'all'
            return jsonify({'ok': True, 'vehicle_id': 'all'})
        try:
            vid = int(raw)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid vehicle_id'}), 400
        if Vehicle.query.get(vid) is None:
            return jsonify({'error': 'vehicle not found'}), 404
        session['vehicle_filter'] = vid
        return jsonify({'ok': True, 'vehicle_id': vid})

    # ── DASHBOARD ──────────────────────────────────────────────
    @app.route('/')
    def dashboard():
        from services.stats_service import (
            get_summary_stats, get_chart_data, get_ac_dc_stats,
            get_yearly_stats, get_vehicle_history,
        )
        # v2.29 fleet picker: 'all' → fleet aggregate (no filter);
        # int → that vehicle. _active_vehicle_id() returns 'all' or
        # an int; map 'all' to None for the stats API.
        _picker = _active_vehicle_id()
        vid_filter = None if _picker == 'all' else _picker
        stats = get_summary_stats(vehicle_id=vid_filter)
        chart_data = get_chart_data(vehicle_id=vid_filter)
        acdc = get_ac_dc_stats(vehicle_id=vid_filter)
        yearly = get_yearly_stats(vehicle_id=vid_filter)
        vehicle_configured = bool(AppConfig.get('vehicle_api_brand', ''))
        # User's preferred default range for the vehicle-history plots
        # (0 = all). Stored in AppConfig so the choice persists across
        # sessions. Client-side AJAX can override for the current view.
        try:
            default_days = int(AppConfig.get('dash_history_days', '30') or '30')
        except (ValueError, TypeError):
            default_days = 30
        vehicle_history = get_vehicle_history(days=default_days or None, vehicle_id=vid_filter) if vehicle_configured else None
        # The map card only makes sense with GPS. Under Kia/Hyundai
        # "cached" mode the most recent sync often has no lat/lon — but
        # earlier syncs in the same window may. Pick the last row that
        # actually carries coords so the map appears for those users too.
        last_gps = None
        if vehicle_history:
            series = vehicle_history.get('series') or {}
            lats = series.get('lat') or []
            lons = series.get('lon') or []
            stamps = series.get('timestamps') or []
            last_idx = -1
            for i in range(len(lats) - 1, -1, -1):
                if lats[i] is not None and lons[i] is not None:
                    last_idx = i
                    break
            if last_idx >= 0:
                last_gps = {
                    'lat': lats[last_idx],
                    'lon': lons[last_idx],
                    # "stale" = GPS is not on the most recent sync, so
                    # the template can annotate the card with "last known"
                    'stale': last_idx < len(lats) - 1,
                    'at': stamps[last_idx] if last_idx < len(stamps) else None,
                }
        # Feature-matrix-aware tile gating: hide dashboard widgets for
        # fields the brand connector doesn't populate, instead of
        # showing a permanent "—" placeholder.
        from services.vehicle.feature_matrix import get_features
        vehicle_features = get_features(
            (AppConfig.get('vehicle_api_brand', '') or '').lower()
        )
        return render_template('dashboard.html',
                               stats=stats, chart_data=chart_data,
                               acdc=acdc, yearly=yearly,
                               vehicle_configured=vehicle_configured,
                               vehicle_features=vehicle_features,
                               vehicle_history=vehicle_history,
                               vehicle_history_days=default_days,
                               vehicle_history_last_gps=last_gps,
                               battery_kwh=_get_battery_kwh())

    # ── EINGABE ────────────────────────────────────────────────
    @app.route('/input', methods=['GET', 'POST'])
    def input_charge():
        if request.method == 'POST':
            try:
                # Intermediate-save handshake: when a charge session is still
                # active and the user clicks Speichern, the form carries the
                # previously-saved row's id so we update in place instead of
                # inserting a new row. Prevents duplicate rows per session
                # and keeps the running timer intact after the redirect.
                existing_id = _int(request.form.get('charge_id'))
                charge = Charge.query.get(existing_id) if existing_id else None
                is_update = charge is not None
                # v3.0.15: server-side double-submit guard. The Stop +
                # Save buttons had no debounce, so a 3× tap (the user's
                # report) inserted 2-3 near-identical rows before the
                # redirect landed. If the form carries no charge_id but
                # an almost-identical charge was created in the last 90 s
                # (same vehicle/date/SoC window, kWh within 0.5), fold
                # this submit into that row instead of inserting again.
                if not is_update:
                    from datetime import timedelta as _tdelta
                    _vid_g = _active_vehicle_id()
                    _vid_g = _vid_g if isinstance(_vid_g, int) else None
                    try:
                        _d_g = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
                        _sf_g = _int(request.form.get('soc_from'))
                        _st_g = _int(request.form.get('soc_to'))
                        _kwh_g = _float(request.form.get('kwh_loaded'))
                        _since = datetime.now() - _tdelta(seconds=90)
                        _dupe_q = Charge.query.filter(
                            Charge.date == _d_g,
                            Charge.soc_from == _sf_g,
                            Charge.soc_to == _st_g,
                            Charge.created_at >= _since,
                        )
                        if _vid_g is not None:
                            _dupe_q = _dupe_q.filter(Charge.vehicle_id == _vid_g)
                        for _cand in _dupe_q.order_by(Charge.id.desc()).all():
                            _ck = _cand.kwh_loaded
                            if (_kwh_g is None or _ck is None
                                    or abs((_ck or 0) - (_kwh_g or 0)) < 0.5):
                                charge = _cand
                                is_update = True
                                logger.info(
                                    f"Charge double-submit folded into "
                                    f"id={_cand.id} (≤90 s, same SoC window)"
                                )
                                break
                    except (ValueError, KeyError):
                        pass
                if not is_update:
                    charge = Charge()

                charge.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
                charge.charge_hour = _int(request.form.get('charge_hour'))
                charge.odometer = _int(request.form.get('odometer'))
                charge.eur_per_kwh = _float(request.form.get('eur_per_kwh'))
                charge.kwh_loaded = _float(request.form.get('kwh_loaded'))
                charge.charge_type = request.form.get('charge_type', 'AC').upper()
                charge.soc_from = _int(request.form.get('soc_from'))
                charge.soc_to = _int(request.form.get('soc_to'))
                charge.loss_kwh = _float(request.form.get('loss_kwh'))
                charge.co2_g_per_kwh = _int(request.form.get('co2_g_per_kwh'))
                charge.notes = request.form.get('notes', '').strip() or None
                charge.location_lat = _float(request.form.get('location_lat'))
                charge.location_lon = _float(request.form.get('location_lon'))
                charge.location_name = request.form.get('location_name', '').strip() or None
                charge.operator = request.form.get('operator', '').strip() or None
                charge.start_fee_eur = _float(request.form.get('start_fee_eur'))
                charge.blocking_fee_eur = _float(request.form.get('blocking_fee_eur'))
                # v2.29 multi-vehicle: stamp the active vehicle on every
                # NEW charge. Updates keep whatever vehicle_id was
                # already set so a stale picker doesn't accidentally
                # re-attribute an old row to a different car.
                if not is_update:
                    _picker = _active_vehicle_id()
                    if isinstance(_picker, int):
                        charge.vehicle_id = _picker
                    else:
                        from models.database import Vehicle
                        _v = (Vehicle.query.filter_by(is_archived=False)
                              .order_by(Vehicle.id.asc()).first()
                              or Vehicle.query.order_by(Vehicle.id.asc()).first())
                        if _v is not None:
                            charge.vehicle_id = _v.id
                _bk = _get_battery_kwh(vehicle_id=charge.vehicle_id)
                _eff = _get_charge_efficiency(vehicle_id=charge.vehicle_id)
                # Hard cap: a single charge can't exceed battery_kwh × the
                # multiplier (= capacity + worst-case AC losses + margin).
                # Catches obvious typos that would otherwise wreck stats
                # and the efficiency baseline.
                if charge.kwh_loaded is not None and _bk:
                    _cap = round(_bk * _CHARGE_KWH_HARD_CAP_MULTIPLIER, 1)
                    if charge.kwh_loaded > _cap:
                        flash(t('flash.kwh_exceeds_cap',
                                kwh=charge.kwh_loaded, cap=_cap), 'danger')
                        return redirect(url_for('input_charge'))
                charge.calculate_fields(_bk, _eff)

                # If no CO2 provided, set automatically
                if charge.co2_g_per_kwh is None:
                    if charge.charge_type == 'PV':
                        charge.co2_g_per_kwh = _get_pv_co2()
                        charge.calculate_fields(_bk, _eff)
                        flash(t('flash.pv_co2_set', value=charge.co2_g_per_kwh), 'info')
                    else:
                        api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
                        co2 = None
                        if api_key:
                            from services.entsoe_service import get_co2_intensity
                            co2 = get_co2_intensity(api_key, datetime.combine(charge.date, datetime.min.time()), hour=charge.charge_hour)
                        if co2:
                            charge.co2_g_per_kwh = co2
                            charge.calculate_fields(_bk, _eff)
                            hour_label = f" ({charge.charge_hour}:00 Uhr)" if charge.charge_hour is not None else ""
                            flash(t('flash.co2_fetched', value=co2, hour=hour_label), 'info')
                        else:
                            # v3.0.15: ENTSO-E often has no intensity for
                            # the current hour yet during an active /
                            # just-finished charge — that's why CO2 was
                            # "sometimes missing". Fall back to the most
                            # recent charge that does have a CO2 value
                            # (same vehicle preferred) so the field is
                            # never left empty; the nightly job can still
                            # refine it later.
                            _fb_q = Charge.query.filter(
                                Charge.co2_g_per_kwh.isnot(None),
                                Charge.charge_type != 'PV',
                            )
                            if charge.vehicle_id is not None:
                                _fb_q = _fb_q.filter(
                                    Charge.vehicle_id == charge.vehicle_id)
                            _fb = _fb_q.order_by(Charge.date.desc()).first()
                            if _fb is not None and _fb.co2_g_per_kwh:
                                charge.co2_g_per_kwh = _fb.co2_g_per_kwh
                                charge.calculate_fields(_bk, _eff)
                                flash(t('flash.co2_estimated',
                                        value=_fb.co2_g_per_kwh), 'info')

                if not is_update:
                    db.session.add(charge)
                db.session.commit()
                cost_str = f'€{charge.total_cost:.2f}' if charge.total_cost is not None else '€—'
                session_active = request.form.get('session_active') == '1'
                if session_active:
                    flash(t('flash.charge_intermediate_saved'), 'info')
                    return redirect(url_for('input_charge', saved_id=charge.id, active=1))
                flash(t('flash.charge_saved', date=charge.date.strftime("%d.%m.%Y"), kwh=charge.kwh_loaded or 0, cost=cost_str), 'success')
                return redirect(url_for('input_charge'))

            except Exception as e:
                logger.error(f"Error saving charge: {e}")
                flash(t('flash.save_error', error=e), 'danger')

        # Pre-fill date with today
        last_charge = Charge.query.order_by(Charge.date.desc()).first()
        vehicle_configured = bool(AppConfig.get('vehicle_api_brand', ''))
        pre_charge = None
        active_session = False
        active_saved_id = None
        saved_id = _int(request.args.get('saved_id'))
        if saved_id:
            pre_charge = Charge.query.get(saved_id)
            active_session = request.args.get('active') == '1'
            # v3.0.39: snapshot the saved_id at render time so the
            # banner's Discard handler can read it via a data attribute
            # even after the client-side ingestSavedId IIFE has
            # stripped it from the URL.
            if active_session:
                active_saved_id = saved_id
        # v3.0.40: server-side dismissal — once the user clicks Verwerfen
        # the saved_id lands in AppConfig.dismissed_charge_session_ids
        # and the banner stays suppressed even when the URL still
        # carries the params (iOS Safari likes to keep them in history).
        if active_session and active_saved_id is not None:
            try:
                _dlist = json.loads(
                    AppConfig.get('dismissed_charge_session_ids', '[]') or '[]')
                if isinstance(_dlist, list) and str(active_saved_id) in [str(x) for x in _dlist]:
                    active_session = False
                    active_saved_id = None
            except (ValueError, TypeError):
                pass
        # v3.0.21: prefill location/operator from the latest sync GPS for
        # a fresh form (no pre_charge round-trip). Mirrors the resolution
        # that _detect_auto_charge does on charge-end so a manually-started
        # charge at home/work/a favorite arrives with the right operator,
        # location name, and configured price already filled in.
        auto_loc = None
        if pre_charge is None:
            try:
                _vid_pf = _active_vehicle_id()
                _vid_pf = _vid_pf if isinstance(_vid_pf, int) else None
                _q_pf = VehicleSync.query.order_by(VehicleSync.timestamp.desc())
                if _vid_pf is not None:
                    _q_pf = _q_pf.filter(VehicleSync.vehicle_id == _vid_pf)
                _last_sync = _q_pf.first()
                if (_last_sync is not None
                        and _last_sync.location_lat is not None
                        and _last_sync.location_lon is not None):
                    from services.trips_service import _classify_location
                    from services.i18n import t as _ti
                    _lbl, _fav = _classify_location(
                        _last_sync.location_lat, _last_sync.location_lon)
                    _op_name = None
                    _loc_name = None
                    if _lbl == 'home':
                        _op_name = _ti('set.op_home_private')
                        _loc_name = (AppConfig.get('home_label', 'Home')
                                     or 'Home')
                    elif _lbl == 'work':
                        _op_name = _ti('set.op_work')
                        _loc_name = (AppConfig.get('work_label', 'Work')
                                     or 'Work')
                    elif _lbl == 'favorite' and _fav:
                        _loc_name = _fav
                    _prices_pf = _get_operator_prices()
                    _eur = (_prices_pf.get(_op_name)
                            if _op_name and _op_name in _prices_pf else None)
                    auto_loc = {
                        'lat': _last_sync.location_lat,
                        'lon': _last_sync.location_lon,
                        'location_name': _loc_name or '',
                        # Only fill operator when it maps to a configured
                        # price — otherwise the dropdown filter would hide
                        # it (operators without a price are non-selectable
                        # since v3.0.14).
                        'operator': _op_name if _eur is not None else '',
                        'eur_per_kwh': _eur,
                    }
            except Exception as _e:
                logger.debug(f"auto-loc prefill failed: {_e}")

        # Embedded history table — same filter / pagination semantics as
        # /history. The partial template is included after the input form
        # so the user lands on /input and sees recent charges in one view
        # (Fahrtenbuch-style: edit + new entry on the same page).
        charges, charge_type, year_filter, years, per_page_eff = \
            _build_charges_query(request.args)
        return render_template('input.html',
                               today=date.today().isoformat(),
                               charge_efficiency=_get_charge_efficiency(
                                   _active_vehicle_id() if isinstance(_active_vehicle_id(), int) else None),
                               last_charge=last_charge,
                               pre_charge=pre_charge,
                               auto_loc=auto_loc,
                               active_session=active_session,
                               active_saved_id=active_saved_id,
                               pv_co2=_get_pv_co2(),
                               pv_price=AppConfig.get('pv_price_eur_per_kwh', '0.00'),
                               max_ac_kw=AppConfig.get('max_ac_kw', '11'),
                               battery_kwh=_get_battery_kwh(),
                               vehicle_configured=vehicle_configured,
                               home_lat=AppConfig.get('home_lat', ''),
                               home_lon=AppConfig.get('home_lon', ''),
                               home_label=AppConfig.get('home_label', ''),
                               work_lat=AppConfig.get('work_lat', ''),
                               work_lon=AppConfig.get('work_lon', ''),
                               work_label=AppConfig.get('work_label', ''),
                               operators=_get_operator_list(
                                   selected=((pre_charge.operator if pre_charge else None)
                                             or (auto_loc.get('operator') if auto_loc else None))),
                               operator_prices=_get_operator_prices(),
                               charges=charges,
                               charge_type=charge_type,
                               year=year_filter,
                               years=years,
                               per_page=per_page_eff)

    def _build_charges_query(args):
        """Shared query/pagination builder for /history and /input.

        Returns (charges, charge_type, year, years, per_page_raw). Both
        endpoints render the same `_history_section.html` partial so they
        consume identical filter/pagination data.
        """
        page = args.get('page', 1, type=int)
        per_page_raw = (args.get('per_page', '50') or '50').strip().lower()
        charge_type = args.get('type', '')
        year = args.get('year', '', type=str)

        query = Charge.query
        # v2.29 fleet picker — restrict to active vehicle unless the
        # user picked "Alle" in the navbar dropdown.
        _picker = _active_vehicle_id()
        if isinstance(_picker, int):
            query = query.filter_by(vehicle_id=_picker)
        if charge_type in ('AC', 'DC', 'PV'):
            query = query.filter_by(charge_type=charge_type)
        if year and year.isdigit():
            from sqlalchemy import extract
            query = query.filter(extract('year', Charge.date) == int(year))

        # "all" → single page containing every match; a sentinel size of
        # max(total, 1) keeps the Pagination object's math (has_prev/next,
        # iter_pages) consistent with the rest of the page.
        if per_page_raw == 'all':
            total = query.count()
            effective_per_page = max(total, 1)
        else:
            try:
                effective_per_page = int(per_page_raw)
            except ValueError:
                effective_per_page = 50
            if effective_per_page not in (50, 100, 200):
                effective_per_page = 50

        charges = query.order_by(Charge.date.desc()).paginate(
            page=page, per_page=effective_per_page, error_out=False)

        years = db.session.query(
            db.func.distinct(db.func.strftime('%Y', Charge.date))
        ).order_by(db.func.strftime('%Y', Charge.date).desc()).all()
        years = [y[0] for y in years if y[0]]

        return charges, charge_type, year, years, per_page_raw

    # ── HISTORY ────────────────────────────────────────────────
    @app.route('/history')
    def history():
        charges, charge_type, year, years, per_page_raw = \
            _build_charges_query(request.args)
        # v3.0.24: the embedded edit-modal in _history_section.html needs
        # the operator dropdown + price map + battery capacity (for the
        # kWh hard cap). /input already passes these; mirror them here so
        # the partial works in both contexts.
        return render_template('history.html', charges=charges,
                               charge_type=charge_type, year=year, years=years,
                               per_page=per_page_raw,
                               operators=_get_operator_list(),
                               operator_prices=_get_operator_prices(),
                               battery_kwh=_get_battery_kwh())

    # ── EDIT / DELETE ──────────────────────────────────────────
    @app.route('/edit/<int:charge_id>', methods=['GET', 'POST'])
    def edit_charge(charge_id):
        charge = Charge.query.get_or_404(charge_id)

        if request.method == 'POST':
            try:
                charge.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
                charge.charge_hour = _int(request.form.get('charge_hour'))
                charge.odometer = _int(request.form.get('odometer'))
                charge.eur_per_kwh = _float(request.form.get('eur_per_kwh'))
                charge.kwh_loaded = _float(request.form.get('kwh_loaded'))
                charge.charge_type = request.form.get('charge_type', 'AC').upper()
                charge.soc_from = _int(request.form.get('soc_from'))
                charge.soc_to = _int(request.form.get('soc_to'))
                charge.loss_kwh = _float(request.form.get('loss_kwh'))
                charge.co2_g_per_kwh = _int(request.form.get('co2_g_per_kwh'))
                charge.notes = request.form.get('notes', '').strip() or None
                # Location + operator — previously missing from the edit
                # route, so users couldn't correct a Ladeort typo after
                # the fact. Same nullability semantics as /input.
                charge.location_name = request.form.get('location_name', '').strip() or None
                charge.location_lat = _float(request.form.get('location_lat'))
                charge.location_lon = _float(request.form.get('location_lon'))
                charge.operator = request.form.get('operator', '').strip() or None
                charge.start_fee_eur = _float(request.form.get('start_fee_eur'))
                charge.blocking_fee_eur = _float(request.form.get('blocking_fee_eur'))
                # v3.0.18: editing+saving an auto-detected charge confirms
                # it — clear the red needs_review flag.
                charge.needs_review = False
                charge.calculate_fields(
                    _get_battery_kwh(vehicle_id=charge.vehicle_id),
                    _get_charge_efficiency(vehicle_id=charge.vehicle_id),
                )
                db.session.commit()
                flash(t('flash.entry_updated'), 'success')
                # v3.0.24: when the edit was submitted from the in-page
                # modal (on /input or /history), bounce back to where the
                # user was instead of always landing on /history.
                _ref = request.referrer or ''
                if _ref and request.host in _ref:
                    return redirect(_ref)
                return redirect(url_for('history'))
            except Exception as e:
                flash(t('flash.error', error=e), 'danger')

        return render_template('edit.html', charge=charge,
                               operators=_get_operator_list(
                                   selected=charge.operator),
                               operator_prices=_get_operator_prices(),
                               home_lat=AppConfig.get('home_lat', ''),
                               home_lon=AppConfig.get('home_lon', ''),
                               home_label=AppConfig.get('home_label', ''),
                               work_lat=AppConfig.get('work_lat', ''),
                               work_lon=AppConfig.get('work_lon', ''),
                               work_label=AppConfig.get('work_label', ''))

    @app.route('/api/charges/dismiss_session', methods=['POST'])
    def api_dismiss_charge_session():
        """v3.0.40: server-side persistent dismissal of an active charge
        session. The /input route checks this list before rendering
        ``active_session=True``, so once the user has clicked Verwerfen
        the banner stays suppressed regardless of what URL params their
        browser keeps reloading."""
        data = request.get_json(silent=True) or {}
        sid = data.get('saved_id')
        if sid is None or str(sid) == '':
            return jsonify({'error': 'missing_saved_id'}), 400
        try:
            current = json.loads(
                AppConfig.get('dismissed_charge_session_ids', '[]') or '[]')
            if not isinstance(current, list):
                current = []
        except (ValueError, TypeError):
            current = []
        sid_s = str(sid)
        if sid_s not in [str(x) for x in current]:
            current.append(sid_s)
            # Cap at 200 ids so the list can't grow unbounded.
            if len(current) > 200:
                current = current[-200:]
            AppConfig.set('dismissed_charge_session_ids', json.dumps(current))
        return jsonify({'ok': True, 'dismissed_count': len(current)})

    @app.route('/delete/<int:charge_id>', methods=['POST'])
    def delete_charge(charge_id):
        charge = Charge.query.get_or_404(charge_id)
        db.session.delete(charge)
        db.session.commit()
        flash(t('flash.entry_deleted'), 'warning')
        _ref = request.referrer or ''
        if _ref and request.host in _ref:
            return redirect(_ref)
        return redirect(url_for('history'))

    # ── SETTINGS ───────────────────────────────────────────────
    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        # Preserve the section the user was in when they hit "save" — the
        # hidden `return_section` field is injected by settings.html JS.
        # Only whitelisted `sec-*` ids make it into the redirect to keep
        # response-splitting/open-redirect attacks off the table.
        def _settings_url_with_section():
            sec = (request.form.get('return_section') or '').strip()
            base = url_for('settings')
            if sec.startswith('sec-') and all(c.isalnum() or c in '-_' for c in sec):
                return f'{base}#{sec}'
            return base

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'save_entsoe':
                key = request.form.get('entsoe_key', '').strip()
                AppConfig.set('entsoe_api_key', key)
                flash(t('flash.entsoe_saved'), 'success')

            elif action == 'test_entsoe':
                key = AppConfig.get('entsoe_api_key', '')
                if key:
                    from services.entsoe_service import test_api_key
                    if test_api_key(key):
                        flash(t('flash.entsoe_valid'), 'success')
                    else:
                        flash(t('flash.entsoe_invalid'), 'danger')
                else:
                    flash(t('flash.entsoe_missing_key'), 'warning')

            elif action == 'save_language':
                lang = request.form.get('app_language', 'de')
                AppConfig.set('app_language', lang)
                from services.i18n import set_language
                set_language(lang)
                return redirect(_settings_url_with_section())

            elif action == 'save_car':
                # v2.29: legacy single-vehicle form is gone (replaced by
                # the per-vehicle Fahrzeuge section). Stub kept so any
                # cached client retry doesn't 404 — silently no-ops and
                # nudges the user to the new section.
                flash(t('flash.vehicle_legacy_save_redirect'), 'info')
                return redirect('/settings#sec-fleet')

            elif action == 'save_operators':
                import json as _json
                # The settings form submits parallel arrays: one row per
                # operator, each with a name (may be blank for empty row),
                # a price (may be blank), and an is_builtin flag so we
                # know not to re-add built-ins to the custom list.
                names      = request.form.getlist('op_name')
                prices     = request.form.getlist('op_price')
                # Monthly base fee per operator — added v2.28.59 for users
                # with a contract / Grundgebühr that doesn't show up in
                # the per-kWh price. Stored as a parallel JSON dict so
                # the field is optional per row and unset rows drop out.
                monthly    = request.form.getlist('op_monthly_fee')
                builtins   = request.form.getlist('op_builtin')  # '1'/'0' per row
                # Pad the builtin flag in case the form was tampered with —
                # safer than crashing on IndexError.
                while len(builtins) < len(names):
                    builtins.append('0')

                custom_list = []
                price_map = {}
                monthly_map = {}
                for i, raw_name in enumerate(names):
                    name = (raw_name or '').strip()
                    if not name:
                        continue
                    if builtins[i] != '1' and name not in custom_list:
                        # Skip names that collide with built-ins to avoid
                        # duplicate dropdown entries. Compare against the
                        # language-agnostic brand list AND the currently
                        # localised generic labels so switching languages
                        # later doesn't resurrect an already-hidden entry.
                        if name not in _DEFAULT_OPERATOR_BRANDS and name not in get_default_operators():
                            custom_list.append(name)
                    raw_price = (prices[i] if i < len(prices) else '') or ''
                    raw_price = raw_price.replace(',', '.').strip()
                    if raw_price:
                        try:
                            p = float(raw_price)
                            if p > 0:
                                price_map[name] = round(p, 4)
                        except ValueError:
                            pass
                    raw_monthly = (monthly[i] if i < len(monthly) else '') or ''
                    raw_monthly = raw_monthly.replace(',', '.').strip()
                    if raw_monthly:
                        try:
                            m = float(raw_monthly)
                            if m > 0:
                                monthly_map[name] = round(m, 2)
                        except ValueError:
                            pass

                AppConfig.set('custom_operators', _json.dumps(custom_list))
                AppConfig.set('operator_prices', _json.dumps(price_map))
                AppConfig.set('operator_monthly_fees', _json.dumps(monthly_map))
                flash(t('flash.operators_saved'), 'success')

            elif action == 'save_pv':
                AppConfig.set('pv_kwp', request.form.get('pv_kwp', ''))
                AppConfig.set('pv_yield_per_kwp', request.form.get('pv_yield_per_kwp', ''))
                AppConfig.set('pv_lifetime', request.form.get('pv_lifetime', ''))
                AppConfig.set('pv_production_co2', request.form.get('pv_production_co2', ''))
                AppConfig.set('pv_price_eur_per_kwh', request.form.get('pv_price_eur_per_kwh', ''))
                # v2.28.64: cascade the new PV-Strompreis + PV-CO2 onto
                # every existing PV charge so the report's avg-€/kWh and
                # cost charts reflect the current setting. Without this,
                # changing the price from 0.30 to 0 leaves old PV rows
                # at their saved-time price and the report keeps showing
                # the stale value.
                try:
                    raw_price = (request.form.get('pv_price_eur_per_kwh', '') or '0').replace(',', '.')
                    new_price = float(raw_price) if raw_price.strip() else 0.0
                except ValueError:
                    new_price = 0.0
                new_co2 = _get_pv_co2()
                bk = _get_battery_kwh()
                pv_charges = Charge.query.filter_by(charge_type='PV').all()
                for c in pv_charges:
                    c.eur_per_kwh = new_price
                    c.co2_g_per_kwh = new_co2
                    c.calculate_fields(bk)
                if pv_charges:
                    db.session.commit()
                flash(t('flash.pv_saved'), 'success')

            elif action == 'add_thg':
                try:
                    # v3.0: bind the quota to a specific vehicle. The
                    # form submits ``thg_vehicle_id`` either from the
                    # explicit dropdown (fleet view) or a hidden input
                    # carrying the picker-active vehicle.
                    raw_vid = request.form.get('thg_vehicle_id', '').strip()
                    veh_id = None
                    if raw_vid:
                        try:
                            veh_id = int(raw_vid)
                        except ValueError:
                            veh_id = None
                    if veh_id is None:
                        from models.database import Vehicle as _V
                        _fallback = (_V.query.filter_by(is_archived=False)
                                     .order_by(_V.id.asc()).first())
                        veh_id = _fallback.id if _fallback else None
                    thg = ThgQuota(
                        vehicle_id=veh_id,
                        year_from=int(request.form['thg_year_from']),
                        year_to=int(request.form['thg_year_to']),
                        amount_eur=float(request.form['thg_amount'].replace(',', '.')),
                    )
                    db.session.add(thg)
                    db.session.commit()
                    flash(t('flash.thg_added', period=f'{thg.year_from}/{thg.year_to}'), 'success')
                except Exception as e:
                    flash(t('flash.error', error=e), 'danger')

            elif action == 'delete_thg':
                thg = ThgQuota.query.get(request.form.get('thg_id'))
                if thg:
                    db.session.delete(thg)
                    db.session.commit()
                    flash(t('flash.thg_deleted'), 'warning')

            elif action == 'import_csv':
                file = request.files.get('csv_file')
                if file and file.filename:
                    try:
                        import io, json as _json
                        from import_gsheet import import_csv_data, VALID_MODES, ALL_LOGICAL_FIELDS
                        mode = request.form.get('import_mode', 'skip')
                        if mode not in VALID_MODES:
                            mode = 'skip'
                        # Optional column_override from the preview UI: a
                        # JSON object {logical_field: col_index|null} that
                        # patches the auto-detected mapping. None/empty
                        # unmaps. Silently ignored if malformed.
                        override_raw = request.form.get('column_override', '').strip()
                        column_override = None
                        if override_raw:
                            try:
                                parsed = _json.loads(override_raw)
                                if isinstance(parsed, dict):
                                    column_override = {
                                        k: (None if v in (None, '', 'null') else int(v))
                                        for k, v in parsed.items()
                                        if k in ALL_LOGICAL_FIELDS
                                    }
                            except (ValueError, TypeError):
                                column_override = None
                        # Replace mode requires an explicit confirmation
                        # checkbox in addition to the mode select, to
                        # prevent accidental data loss on production DBs.
                        if mode == 'replace' and 'replace_confirm' not in request.form:
                            flash(t('flash.import_replace_needs_confirm'), 'warning')
                        else:
                            stream = io.StringIO(file.stream.read().decode('utf-8'))
                            result = import_csv_data(stream, mode=mode,
                                                     column_override=column_override)
                            parts = [t('flash.import_success', count=result['imported'])]
                            if result.get('updated'):
                                parts.append(t('flash.import_updated', count=result['updated']))
                            if result.get('skipped_dup'):
                                parts.append(t('flash.import_skipped_dup', count=result['skipped_dup']))
                            if result['errors']:
                                parts.append(t('flash.import_errors', count=len(result['errors'])))
                            if result.get('backup'):
                                parts.append(t('flash.import_backup_made'))
                            flash(' '.join(parts), 'success')
                            # Auto-start CO2 backfill
                            from services.co2_backfill import start_backfill
                            if start_backfill(app):
                                flash(t('flash.co2_backfill_started'), 'info')
                    except Exception as e:
                        flash(t('flash.import_error', error=e), 'danger')
                else:
                    flash(t('flash.no_file'), 'warning')

            elif action == 'backfill_co2':
                from services.co2_backfill import start_backfill
                if start_backfill(app):
                    flash(t('flash.co2_loading'), 'info')
                else:
                    flash(t('flash.backfill_running'), 'warning')

            elif action == 'save_sync_settings':
                # v2.29: per-vehicle credentials moved to /vehicles/save —
                # this section is now ONLY the global sync schedule
                # (interval / mode / smart-window). Picks up the new
                # values by stopping + restarting the sync thread.
                enabled = 'true' if 'vehicle_sync_enabled' in request.form else 'false'
                AppConfig.set('vehicle_sync_enabled', enabled)
                AppConfig.set('vehicle_sync_interval_hours', request.form.get('vehicle_sync_interval', '4'))
                AppConfig.set('vehicle_sync_mode', request.form.get('vehicle_sync_mode', 'cached'))
                AppConfig.set('smart_active_start_hour', request.form.get('smart_active_start_hour', '6'))
                AppConfig.set('smart_active_end_hour', request.form.get('smart_active_end_hour', '22'))
                AppConfig.set('smart_active_interval_min', request.form.get('smart_active_interval_min', '10'))
                from services.vehicle.sync_service import stop_sync, start_sync
                stop_sync()
                import time as _time
                _time.sleep(0.5)
                if enabled == 'true':
                    start_sync(app)
                flash(t('flash.sync_settings_saved'), 'success')

            elif action == 'save_vehicle_sync':
                enabled = 'true' if 'vehicle_sync_enabled' in request.form else 'false'
                AppConfig.set('vehicle_sync_enabled', enabled)
                AppConfig.set('vehicle_sync_interval_hours', request.form.get('vehicle_sync_interval', '4'))
                AppConfig.set('vehicle_sync_mode', request.form.get('vehicle_sync_mode', 'cached'))
                AppConfig.set('smart_active_start_hour', request.form.get('smart_active_start_hour', '6'))
                AppConfig.set('smart_active_end_hour', request.form.get('smart_active_end_hour', '22'))
                AppConfig.set('smart_active_interval_min', request.form.get('smart_active_interval_min', '10'))
                from services.vehicle.sync_service import stop_sync, start_sync
                stop_sync()  # Always stop first to pick up new settings
                import time as _time
                _time.sleep(0.5)  # Wait for thread to finish
                if enabled == 'true':
                    if start_sync(app):
                        flash(t('flash.sync_started'), 'success')
                    else:
                        flash(t('flash.sync_failed'), 'warning')
                else:
                    flash(t('flash.sync_disabled'), 'warning')

            # v2.29: legacy actions save_vehicle_api / test_vehicle_api /
            # delete_vehicle_api / sync_vehicle_now / sync_vehicle_force
            # are gone — credentials live in /vehicles/save, test+sync
            # are per-vehicle in the Fahrzeuge table. Stale form
            # submissions just no-op-redirect to the new section.
            return redirect(_settings_url_with_section())

        # Vehicle API brands (only those with installed dependencies)
        try:
            from services.vehicle import get_available_brands
            vehicle_brands = get_available_brands()
        except Exception:
            vehicle_brands = []
        installed_brand_keys = [b['key'] for b in vehicle_brands]

        # Last vehicle sync
        last_sync = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()

        # Measured recup rate (from last 90 days of vehicle syncs)
        from services.stats_service import get_recup_rate_kwh_per_km
        measured_recup, measured_recup_source = get_recup_rate_kwh_per_km()

        # Hide the HTTPS card when the client is reaching us via Tailscale.
        # Tailscale CGNAT range is 100.64.0.0/10 — seeing a remote_addr in there
        # means the request came over WireGuard and already has transport
        # encryption; a self-signed HTTPS layer on top is just noise.
        hide_ssl_card = False
        try:
            from ipaddress import ip_address, ip_network
            ts_net = ip_network('100.64.0.0/10')
            client_ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
            if client_ip and ip_address(client_ip) in ts_net:
                hide_ssl_card = True
        except (ValueError, TypeError):
            pass

        # v2.29: pre-fill the legacy "API" section from the picker-active
        # Vehicle row so the form is always editing THAT car's creds.
        # Falls back to AppConfig for backward-compat with single-car
        # installs that haven't fully transitioned yet.
        from models.database import Vehicle as _SetV
        _picker_v = None
        _api_active_vid = _active_vehicle_id()
        if isinstance(_api_active_vid, int):
            _picker_v = _SetV.query.get(_api_active_vid)
        if _picker_v is None:
            _picker_v = (_SetV.query.filter_by(is_archived=False)
                         .order_by(_SetV.id.asc()).first())

        def _api_field(attr, key, default=''):
            if _picker_v is not None and getattr(_picker_v, attr):
                return getattr(_picker_v, attr)
            return AppConfig.get(key, default) or default

        return render_template('settings.html',
                               entsoe_key=AppConfig.get('entsoe_api_key', ''),
                               car_model_val=AppConfig.get('car_model', Config.CAR_MODEL),
                               vehicle_brands=vehicle_brands,
                               installed_brand_keys=installed_brand_keys,
                               api_target_vehicle=_picker_v,
                               vehicle_api_brand=_api_field('api_brand', 'vehicle_api_brand'),
                               vehicle_api_username=_api_field('api_username', 'vehicle_api_username'),
                               vehicle_api_password=_api_field('api_password', 'vehicle_api_password'),
                               vehicle_api_pin=_api_field('api_pin', 'vehicle_api_pin'),
                               vehicle_api_region=_api_field('api_region', 'vehicle_api_region', 'EU'),
                               vehicle_api_vin=_api_field('api_vin', 'vehicle_api_vin'),
                               vehicle_sync_enabled=AppConfig.get('vehicle_sync_enabled', 'false'),
                               vehicle_sync_interval=AppConfig.get('vehicle_sync_interval_hours', '4'),
                               vehicle_sync_mode=AppConfig.get('vehicle_sync_mode', 'cached'),
                               smart_active_start_hour=AppConfig.get('smart_active_start_hour', '6'),
                               smart_active_end_hour=AppConfig.get('smart_active_end_hour', '22'),
                               smart_active_interval_min=AppConfig.get('smart_active_interval_min', '10'),
                               last_vehicle_sync=last_sync,
                               battery_kwh=AppConfig.get('battery_kwh', str(Config.BATTERY_CAPACITY_KWH)),
                               battery_soh_baseline=AppConfig.get('battery_soh_baseline', '100'),
                               max_ac_kw=AppConfig.get('max_ac_kw', ''),
                               battery_co2_per_kwh=AppConfig.get('battery_co2_per_kwh', '100'),
                               fossil_co2_per_km=AppConfig.get('fossil_co2_per_km', '164'),
                               recuperation_kwh_per_km=AppConfig.get('recuperation_kwh_per_km', '0.086'),
                               measured_recup_rate=measured_recup,
                               measured_recup_source=measured_recup_source,
                               pv_kwp=AppConfig.get('pv_kwp', ''),
                               pv_yield_per_kwp=AppConfig.get('pv_yield_per_kwp', '950'),
                               pv_lifetime=AppConfig.get('pv_lifetime', '25'),
                               pv_production_co2=AppConfig.get('pv_production_co2', '1000'),
                               pv_price_eur_per_kwh=AppConfig.get('pv_price_eur_per_kwh', '0.00'),
                               home_lat=AppConfig.get('home_lat', ''),
                               home_lon=AppConfig.get('home_lon', ''),
                               home_label=AppConfig.get('home_label', 'Home'),
                               work_lat=AppConfig.get('work_lat', ''),
                               work_lon=AppConfig.get('work_lon', ''),
                               work_label=AppConfig.get('work_label', 'Work'),
                               thg_quotas=_thg_quotas_for_picker(),
                               total_charges=Charge.query.count(),
                               co2_missing=Charge.query.filter(Charge.co2_g_per_kwh.is_(None), Charge.charge_type != 'PV').count(),
                               auth_enabled=(AppConfig.get('auth_enabled', 'false') == 'true'),
                               auth_username=AppConfig.get('auth_username', ''),
                               hide_ssl_card=hide_ssl_card,
                               custom_operators_text=_get_custom_operators_text(),
                               operators_builtin=get_default_operators(),
                               operators_custom=_get_custom_operators(),
                               operator_prices=_get_operator_prices(),
                               operator_monthly_fees=_get_operator_monthly_fees(),
                               _json_logical_field_labels=_json_logical_field_labels_for_ui(),
                               last_rollback=_get_last_rollback(),
                               app_version=Config.APP_VERSION)

    # ── VEHICLE RAW-DATA VIEWER ────────────────────────────────
    @app.route('/vehicle/raw')
    def vehicle_raw_list():
        """List the most recent vehicle syncs. Each row links to the
        detail view where the full raw API dump is pretty-printed."""
        limit = request.args.get('limit', 50, type=int)
        limit = max(1, min(limit, 500))
        syncs = (VehicleSync.query
                 .order_by(VehicleSync.timestamp.desc())
                 .limit(limit).all())
        brand_key = AppConfig.get('vehicle_api_brand', '')
        return render_template('vehicle_raw.html',
                               syncs=syncs,
                               brand_key=brand_key,
                               limit=limit,
                               detail=None)

    @app.route('/vehicle/raw/<int:sync_id>')
    def vehicle_raw_detail(sync_id):
        """Show the full raw API payload and normalized fields for one
        VehicleSync row. Kia/Hyundai SoH >100% is annotated with a short
        explanation of the manufacturer's reserve-capacity quirk."""
        import json as _json
        from services.stats_service import scale_soh, get_soh_baseline
        sync = VehicleSync.query.get_or_404(sync_id)
        raw_parsed = None
        raw_error = None
        if sync.raw_json:
            try:
                raw_parsed = _json.loads(sync.raw_json)
            except (ValueError, TypeError) as e:
                raw_error = str(e)
        pretty = _json.dumps(raw_parsed, indent=2, ensure_ascii=False, default=str) \
            if raw_parsed is not None else (sync.raw_json or '')
        brand_key = AppConfig.get('vehicle_api_brand', '')
        # Normalized fields we already store in VehicleSync (for the
        # info box above the JSON dump). battery_soh_percent is the
        # scaled (user-facing) value; the raw BMS reading is kept in
        # battery_soh_raw for reference.
        normalized = {
            'timestamp': sync.timestamp.isoformat(),
            'soc_percent': sync.soc_percent,
            'odometer_km': sync.odometer_km,
            'is_charging': sync.is_charging,
            'charge_power_kw': sync.charge_power_kw,
            'estimated_range_km': sync.estimated_range_km,
            'battery_12v_percent': sync.battery_12v_percent,
            'battery_soh_percent': scale_soh(sync.battery_soh_percent),
            'battery_soh_raw': sync.battery_soh_percent,
            'battery_soh_baseline': get_soh_baseline(),
            'total_regenerated_kwh': sync.total_regenerated_kwh,
            'regen_cumulative_kwh': sync.regen_cumulative_kwh,
            'consumption_30d_kwh_per_100km': sync.consumption_30d_kwh_per_100km,
            'location_lat': sync.location_lat,
            'location_lon': sync.location_lon,
        }
        soh_note = None
        if (sync.battery_soh_percent is not None and sync.battery_soh_percent > 100
                and get_soh_baseline() == 100.0
                and brand_key in ('kia', 'hyundai')):
            soh_note = 'kia_soh_over_100'
        return render_template('vehicle_raw.html',
                               syncs=None,
                               brand_key=brand_key,
                               detail={
                                   'sync': sync,
                                   'normalized': normalized,
                                   'raw_pretty': pretty,
                                   'raw_error': raw_error,
                                   'soh_note': soh_note,
                               })

    # ── REPORT ─────────────────────────────────────────────────
    @app.route('/report')
    def report():
        """Interactive report page. The old /report always generated a
        PDF directly; that flow now lives at /report/export.pdf so the
        user can pick a range in the UI first."""
        has_any_data = bool(Charge.query.first()) or bool(db.session.query(
            db.func.count(db.text('1'))).select_from(db.text('vehicle_trips')).scalar())
        return render_template('report.html',
                               has_any_data=has_any_data,
                               car_model=AppConfig.get('car_model', Config.CAR_MODEL))

    @app.route('/api/report')
    def api_report():
        """Range-bounded JSON feed for the /report page's Chart.js plots."""
        from services.report_range import resolve_range, build_report
        preset = request.args.get('preset', 'month')
        start = request.args.get('start')
        end = request.args.get('end')
        lang = AppConfig.get('app_language', 'de')
        s, e, label = resolve_range(preset, start, end, lang=lang)
        # v2.29 fleet picker
        _picker = _active_vehicle_id()
        _vid = _picker if isinstance(_picker, int) else None
        data = build_report(s, e, lang=lang, vehicle_id=_vid)
        data['label'] = label
        data['preset'] = preset
        return jsonify(data)

    @app.route('/report/export.pdf')
    def report_export_pdf():
        """Legacy PDF export, kept for users who want a printable copy.
        Respects the current preset via query params."""
        from services.report_service import generate_report
        from flask import send_file
        pdf_bytes = generate_report()
        if not pdf_bytes:
            flash(t('flash.no_report_data'), 'warning')
            return redirect(url_for('report'))
        car = AppConfig.get('car_model', 'EV')
        filename = f'EV_Report_{car}_{date.today().strftime("%Y%m%d")}.pdf'
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename,
        )

    # ── API ENDPOINTS ──────────────────────────────────────────
    @app.route('/api/co2/<date_str>')
    def api_get_co2(date_str):
        """Fetch CO2 intensity for a date (and optional hour) via ENTSO-E."""
        try:
            target = datetime.strptime(date_str, '%Y-%m-%d')
            hour = request.args.get('hour', type=int)
            api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
            if not api_key:
                return jsonify({'error': 'No ENTSO-E API key configured'}), 400

            from services.entsoe_service import get_co2_intensity
            co2 = get_co2_intensity(api_key, target, hour=hour)
            if co2:
                result = {'co2_g_per_kwh': co2, 'date': date_str}
                if hour is not None:
                    result['hour'] = hour
                return jsonify(result)
            return jsonify({'error': 'No data available'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/co2/range')
    def api_get_co2_range():
        """Fetch average CO2 intensity for a time range (start_hour to end_hour on a date)."""
        try:
            date_str = request.args.get('date')
            start_hour = request.args.get('start_hour', type=int)
            end_hour = request.args.get('end_hour', type=int)
            if not date_str or start_hour is None or end_hour is None:
                return jsonify({'error': 'date, start_hour, end_hour required'}), 400

            api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
            if not api_key:
                return jsonify({'error': 'No ENTSO-E API key configured'}), 400

            from services.entsoe_service import get_co2_intensity
            target = datetime.strptime(date_str, '%Y-%m-%d')

            # Collect hourly CO2 values for the range
            values = []
            for h in range(start_hour, min(end_hour + 1, 24)):
                co2 = get_co2_intensity(api_key, target, hour=h)
                if co2:
                    values.append(co2)

            if values:
                avg = round(sum(values) / len(values))
                return jsonify({
                    'co2_g_per_kwh': avg,
                    'hours_covered': len(values),
                    'hours_total': end_hour - start_hour + 1,
                })
            return jsonify({'error': 'No data available'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/charge/<int:charge_id>/odometer', methods=['POST'])
    def api_update_odometer(charge_id):
        charge = Charge.query.get_or_404(charge_id)
        data = request.get_json()
        charge.odometer = int(data['odometer']) if data.get('odometer') else None
        db.session.commit()
        return jsonify({'ok': True, 'odometer': charge.odometer})

    @app.route('/api/charges/<int:charge_id>', methods=['GET'])
    def api_get_charge(charge_id):
        """Return a single Charge's fields as JSON for the in-place edit
        modal in the history list (v3.0.24). Mirrors Charge.to_dict() and
        adds the needs_review flag the modal uses to mark the form."""
        charge = Charge.query.get_or_404(charge_id)
        d = charge.to_dict()
        d['needs_review'] = bool(charge.needs_review)
        d['vehicle_id'] = charge.vehicle_id
        return jsonify(d)

    @app.route('/api/charges/bulk_type', methods=['POST'])
    def api_charges_bulk_type():
        """Change charge_type on multiple rows in one request. CO2 is
        adjusted when crossing the PV boundary: switching to PV overwrites
        ``co2_g_per_kwh`` with the configured PV intensity; switching from
        PV to AC/DC clears it so the ENTSO-E backfill loop refills it with
        the correct grid value. AC↔DC transitions keep the existing CO2
        (both use the grid, so the value is unchanged)."""
        data = request.get_json(silent=True) or {}
        ids = data.get('ids') or []
        new_type = (data.get('charge_type') or '').upper()
        if new_type not in ('AC', 'DC', 'PV'):
            return jsonify({'error': 'invalid charge_type'}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({'error': 'no ids provided'}), 400
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return jsonify({'error': 'ids must be integers'}), 400

        rows = Charge.query.filter(Charge.id.in_(ids)).all()
        pv_co2 = _get_pv_co2()
        battery_kwh = _get_battery_kwh()
        updated = 0
        for c in rows:
            old = c.charge_type
            if old == new_type:
                continue
            c.charge_type = new_type
            if new_type == 'PV':
                c.co2_g_per_kwh = pv_co2
            elif old == 'PV':
                # Leaving PV → clear CO2 so the ENTSO-E backfill re-populates
                # with the proper grid intensity for the charge's date/hour.
                c.co2_g_per_kwh = None
                c.co2_kg = None
            c.calculate_fields(battery_kwh)
            updated += 1
        db.session.commit()

        # Kick off CO2 backfill for any rows whose CO2 was just cleared.
        if updated:
            try:
                from services.co2_backfill import start_backfill
                start_backfill(app)
            except Exception:
                pass

        return jsonify({'ok': True, 'updated': updated})

    @app.route('/api/co2/backfill/status')
    def api_backfill_status():
        from services.co2_backfill import is_running, get_missing_count
        return jsonify({
            'running': is_running(),
            'missing': get_missing_count(app),
        })

    @app.route('/api/vehicle/token/start', methods=['POST'])
    def api_vehicle_token_start():
        """Start browser-based token fetch with mobile user-agent."""
        data = request.get_json() or {}
        brand = data.get('brand') or AppConfig.get('vehicle_api_brand', '')
        if brand not in ('kia', 'hyundai'):
            return jsonify({'error': t('err.kia_hyundai_only')}), 400
        AppConfig.set('vehicle_api_brand', brand)
        from services.vehicle.token_fetch import start_fetch
        if start_fetch(brand):
            return jsonify({'success': True})
        return jsonify({'error': 'Läuft bereits'}), 409

    @app.route('/api/vehicle/token/status')
    def api_vehicle_token_status():
        """Poll token fetch status. v2.29: when ``vehicle_id`` query
        param is set, write the captured refresh token onto that
        vehicle's row instead of the legacy AppConfig key."""
        from services.vehicle.token_fetch import get_state
        from models.database import Vehicle
        state = get_state()
        if state.get('token'):
            vid_arg = request.args.get('vehicle_id')
            if vid_arg:
                try:
                    v = Vehicle.query.get(int(vid_arg))
                    if v is not None:
                        v.api_password = state['token']
                        db.session.commit()
                except (ValueError, TypeError):
                    pass
            else:
                AppConfig.set('vehicle_api_password', state['token'])
        return jsonify(state)

    @app.route('/api/vehicle/token/cancel', methods=['POST'])
    def api_vehicle_token_cancel():
        from services.vehicle.token_fetch import cancel_fetch
        cancel_fetch()
        return jsonify({'success': True})

    @app.route('/api/vehicle/token/manual', methods=['POST'])
    def api_vehicle_token_manual():
        """Manual fallback: user pastes the URL with ?code=... from their own
        browser, we extract the code and exchange for a refresh_token.

        v2.29: optional ``vehicle_id`` writes the resulting token onto
        that Vehicle row's api_password (and updates api_brand). When
        omitted, falls back to the legacy AppConfig keys.
        """
        from models.database import Vehicle
        data = request.get_json() or {}
        brand = data.get('brand') or AppConfig.get('vehicle_api_brand', '')
        url = data.get('url') or ''
        vid = data.get('vehicle_id')
        if brand not in ('kia', 'hyundai'):
            return jsonify({'error': t('err.kia_hyundai_only')}), 400
        from services.vehicle.token_fetch import exchange_manual_url
        ok, msg, token = exchange_manual_url(brand, url)
        if ok and token:
            if vid:
                try:
                    v = Vehicle.query.get(int(vid))
                    if v is not None:
                        v.api_brand = brand
                        v.api_password = token
                        db.session.commit()
                        return jsonify({'success': True, 'message': msg, 'vehicle_id': v.id})
                except (ValueError, TypeError):
                    pass
            AppConfig.set('vehicle_api_brand', brand)
            AppConfig.set('vehicle_api_password', token)
            return jsonify({'success': True, 'message': msg})
        return jsonify({'error': msg}), 400

    @app.route('/api/vehicle/token/manual/step_urls')
    def api_vehicle_token_manual_step_urls():
        """Returns the two URLs the user needs to complete the manual flow
        (step 1 = login URL, step 2 = CCSP authorize that produces the final
        ?code=... URL). Frontend uses these as clickable links."""
        brand = request.args.get('brand', '')
        if brand not in ('kia', 'hyundai'):
            return jsonify({'error': t('err.kia_hyundai_only')}), 400
        from services.vehicle.token_fetch import (
            BRAND_CONFIG, _build_login_url, get_manual_step2_url,
        )
        cfg = BRAND_CONFIG.get(brand)
        if not cfg:
            return jsonify({'error': 'Marke nicht gefunden'}), 404
        return jsonify({
            'step1_login_url': _build_login_url(cfg),
            'step2_ccsp_url': get_manual_step2_url(brand),
            'expected_prefix': cfg['redirect_final'].split('?')[0],
        })

    @app.route('/api/vehicle/install', methods=['POST'])
    def api_vehicle_install():
        """Install vehicle API packages via pip."""
        import subprocess
        import sys

        PACKAGES = {
            'hyundai-kia': ['hyundai-kia-connect-api', 'selenium', 'webdriver-manager'],
            'vw': ['carconnectivity', 'carconnectivity-connector-volkswagen'],
            'skoda': ['carconnectivity', 'carconnectivity-connector-skoda'],
            'seatcupra': ['carconnectivity', 'carconnectivity-connector-seatcupra'],
            'tesla': ['teslapy'],
            'renault': ['renault-api', 'aiohttp'],
            'polestar': ['pypolestar'],
            'mg': ['saic-ismart-client-ng'],
            'smart': ['pySmartHashtag'],
            'porsche': ['pyporscheconnectapi'],
        }

        data = request.get_json() or {}
        pkg_key = data.get('package', '')
        packages = PACKAGES.get(pkg_key)
        if not packages:
            return jsonify({'success': False, 'error': f'Unbekanntes Paket: {pkg_key}'}), 400

        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install'] + packages,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                error = result.stderr.strip().split('\n')[-1] if result.stderr else 'pip install fehlgeschlagen'
                return jsonify({'success': False, 'error': error}), 500

            # v3.0: schedule a clean systemd restart instead of reloading
            # modules in-place. The previous approach called
            # ``importlib.reload(services.vehicle.registry)`` which wiped
            # the brand registry to an empty dict and then could NOT
            # repopulate it (the connector modules were already imported
            # so their module-level ``register(...)`` calls didn't fire
            # again). Result: every subsequent vehicle sync raised
            # "Unknown vehicle brand: kia" until the service was
            # restarted out-of-band. A clean restart picks up the newly
            # pip-installed connector via fresh imports — reliable.
            def _delayed_restart():
                import time as _t
                _t.sleep(1.0)  # let the HTTP response flush
                try:
                    subprocess.run(
                        ['sudo', '-n', '/bin/systemctl', 'restart', 'ev-tracker.service'],
                        timeout=10,
                    )
                except Exception as _e:
                    logger.warning(f"Post-install restart failed: {_e}")
            import threading as _th
            _th.Thread(target=_delayed_restart, daemon=True).start()
            return jsonify({
                'success': True,
                'installed': packages,
                'restarting': True,
            })
        except subprocess.TimeoutExpired:
            return jsonify({'success': False, 'error': 'Timeout — Installation dauert zu lange'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/vehicle/history')
    def api_vehicle_history():
        """Time-series history of tracked vehicle metrics, filtered to
        the last ``days`` days. ``days=0`` returns the full history.

        Used by the dashboard when the user switches the range dropdown —
        the initial page load is rendered server-side with the stored
        default so the charts draw on first paint.
        """
        try:
            days = int(request.args.get('days', '30') or '0')
        except (ValueError, TypeError):
            days = 30
        days = max(0, min(days, 3650))  # sanity clamp: 0..10 years

        # Persist the user's choice for the next page load
        if request.args.get('persist') == '1':
            AppConfig.set('dash_history_days', str(days))

        from services.stats_service import get_vehicle_history
        data = get_vehicle_history(days=days or None)
        if data is None:
            return jsonify({'series': None, 'summary': {'count': 0}, 'days': days})
        data['days'] = days
        return jsonify(data)

    @app.route('/api/vehicle/last_gps')
    def api_vehicle_last_gps():
        """Most recent VehicleSync row that carries coords.

        Used by the charge-input form's "Mein Standort" button so the user
        can fill the location fields with the car's last known position
        instead of the browser's GPS (which requires HTTPS and isn't useful
        for charge sessions that happened before opening the phone).
        """
        row = (VehicleSync.query
               .filter(VehicleSync.location_lat.isnot(None),
                       VehicleSync.location_lon.isnot(None))
               .order_by(VehicleSync.timestamp.desc())
               .first())
        if not row:
            return jsonify({'error': 'no_gps'}), 404
        latest = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()
        return jsonify({
            'lat': row.location_lat,
            'lon': row.location_lon,
            'at': row.timestamp.strftime('%d.%m.%Y %H:%M'),
            'stale': latest is not None and latest.id != row.id,
        })

    @app.route('/api/vehicle/sync/status')
    def api_vehicle_sync_status():
        """Return vehicle sync service status."""
        from services.vehicle.sync_service import is_running
        last_sync = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()
        return jsonify({
            'running': is_running(),
            'last_sync': last_sync.timestamp.strftime('%d.%m.%Y %H:%M') if last_sync else None,
            'last_soc': last_sync.soc_percent if last_sync else None,
            'last_odometer': last_sync.odometer_km if last_sync else None,
            'is_charging': last_sync.is_charging if last_sync else None,
        })

    @app.route('/api/vehicle/last_sync')
    def api_vehicle_last_sync():
        """Lightweight DB-only read of the latest VehicleSync for the
        active vehicle. Used by the charge form's auto-stop poll so it
        can poll on a short interval (~60 s) without burning API quota
        on cached cloud calls — the bg sync loop already updates the DB
        every 10 min on a quota-friendly cached read, so a frontend that
        reads the DB sees the same is_charging flip within the same
        window, just for free."""
        _picker = _active_vehicle_id()
        _vid = _picker if isinstance(_picker, int) else None
        q = VehicleSync.query.order_by(VehicleSync.timestamp.desc())
        if _vid is not None:
            q = q.filter(VehicleSync.vehicle_id == _vid)
        s = q.first()
        if s is None:
            return jsonify({'soc': None, 'is_charging': None})
        age = (datetime.now() - s.timestamp).total_seconds()
        return jsonify({
            'soc': s.soc_percent,
            'is_charging': bool(s.is_charging) if s.is_charging is not None else None,
            'charge_power_kw': s.charge_power_kw,
            'odometer': s.odometer_km,
            'timestamp': s.timestamp.isoformat(),
            'age_seconds': age,
        })

    @app.route('/api/vehicle/status')
    def api_vehicle_status():
        """Fetch current vehicle status with full details."""
        brand = AppConfig.get('vehicle_api_brand', '')
        if not brand:
            return jsonify({'error': 'not_configured'}), 400

        force = request.args.get('force', '0') == '1'
        confirm_low_12v = request.args.get('confirm_low_12v', '0') == '1'

        # Validate token format for Kia/Hyundai
        if brand in ('kia', 'hyundai'):
            import re as _re
            token = AppConfig.get('vehicle_api_password', '')
            if not _re.match(r'^[A-Z0-9]{48}$', token):
                return jsonify({'error': 'Ungültiger Token. Bitte unter Einstellungen → Token holen.'}), 400

        # v3.0.12: 12 V lockout — block manual force-refresh when the
        # last reading is below threshold, unless the caller has
        # acknowledged the warning (confirm_low_12v=1). The frontend
        # surfaces a modal that supplies the flag on user confirm.
        if force and not confirm_low_12v:
            from services.vehicle.sync_service import (
                is_12v_low, _latest_12v_percent, LOW_12V_THRESHOLD_PERCENT
            )
            _picker_low = _active_vehicle_id()
            _vid_low = _picker_low if isinstance(_picker_low, int) else None
            if _vid_low is None:
                from models.database import Vehicle as _Vlow
                _vlow = (_Vlow.query.filter_by(is_archived=False)
                         .order_by(_Vlow.id.asc()).first())
                _vid_low = _vlow.id if _vlow else None
            if _vid_low and is_12v_low(_vid_low):
                return jsonify({
                    'error': 'low_12v',
                    'battery_12v': _latest_12v_percent(_vid_low),
                    'threshold': LOW_12V_THRESHOLD_PERCENT,
                    'message': 'low_12v_confirmation_required',
                }), 423

        # Rate limiter: max 200 calls/day (Kia EU), track usage
        today_str = date.today().isoformat()
        counter_date = AppConfig.get('vehicle_api_counter_date', '')
        if counter_date != today_str:
            AppConfig.set('vehicle_api_counter_date', today_str)
            AppConfig.set('vehicle_api_counter', '0')
        api_count = int(AppConfig.get('vehicle_api_counter', '0'))
        if api_count >= 190:  # leave 10 buffer for other apps
            return jsonify({'error': f'Tageslimit erreicht ({api_count}/200). Reset um Mitternacht.'}), 429
        AppConfig.set('vehicle_api_counter', str(api_count + 1))

        try:
            from services.vehicle import get_connector
            from services.vehicle.sync_service import log_sync_result
            from services.stats_service import scale_soh
            import json as _json
            creds = _get_vehicle_credentials()
            connector = get_connector(brand, creds)
            s = connector.get_status(force=force)
            _picker_d = _active_vehicle_id()
            _stamp_vid_d = _picker_d if isinstance(_picker_d, int) else None
            if _stamp_vid_d is None:
                from models.database import Vehicle as _Vd
                _vd = (_Vd.query.filter_by(is_archived=False)
                       .order_by(_Vd.id.asc()).first()
                       or _Vd.query.order_by(_Vd.id.asc()).first())
                _stamp_vid_d = _vd.id if _vd else None
            sync = _save_vehicle_sync(s, _get_battery_kwh(vehicle_id=_stamp_vid_d),
                                      raw_json=_json.dumps(s.raw_data, default=str),
                                      vehicle_id=_stamp_vid_d)
            log_sync_result(s,
                            mode_label='force' if force else 'cached',
                            source='dashboard')
            # Hyundai/Kia only include battery_soh in force-refresh responses,
            # not in cached ones. Fall back to the most recent DB row that
            # has a non-null SoH so the dashboard never has to display "—"
            # just because the current sync didn't happen to include it.
            soh_raw = s.battery_soh_percent
            if soh_raw is None:
                from models.database import VehicleSync as _VS
                _prev = (_VS.query
                         .filter(_VS.battery_soh_percent.isnot(None))
                         .order_by(_VS.timestamp.desc())
                         .first())
                if _prev is not None:
                    soh_raw = _prev.battery_soh_percent

            return jsonify({
                'soc': s.soc_percent,
                'odometer': s.odometer_km,
                'is_charging': s.is_charging,
                'is_plugged_in': s.is_plugged_in,
                'is_locked': s.is_locked,
                'range_km': s.estimated_range_km,
                'battery_12v': s.battery_12v_percent,
                'battery_soh': scale_soh(soh_raw),
                'charge_limit_ac': s.charge_limit_ac,
                'charge_limit_dc': s.charge_limit_dc,
                'est_charge_min': s.est_charge_duration_min,
                'est_fast_charge_min': s.est_fast_charge_duration_min,
                'climate_temp': s.climate_temp,
                'climate_on': s.climate_on,
                'total_consumed_kwh': s.total_power_consumed_kwh,
                # Scaled kWh for the rolling 3-month window (raw / 100)
                'total_regenerated_kwh': sync.total_regenerated_kwh,
                'regen_cumulative_kwh': sync.regen_cumulative_kwh,
                'location_lat': s.location_lat,
                'location_lon': s.location_lon,
                'last_updated': s.last_updated,
                'vehicle_name': s.vehicle_name,
                'vehicle_model': s.vehicle_model,
                'doors': {
                    'fl': s.front_left_door_open, 'fr': s.front_right_door_open,
                    'bl': s.back_left_door_open, 'br': s.back_right_door_open,
                    'trunk': s.trunk_open, 'hood': s.hood_open,
                },
                'tires': {
                    'warn': s.tire_warn_all,
                    'fl': s.tire_warn_fl, 'fr': s.tire_warn_fr,
                    'rl': s.tire_warn_rl, 'rr': s.tire_warn_rr,
                },
                'steering_heater': s.steering_wheel_heater,
                'rear_window_heater': s.rear_window_heater,
                'defrost': s.defrost,
                'consumption_30d': s.consumption_30d_wh_per_km,
                'est_portable_charge_min': s.est_portable_charge_min,
                'registration_date': s.registration_date,
                'timestamp': sync.timestamp.strftime('%H:%M'),
                'api_calls_today': api_count + 1,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/stats')
    def api_stats():
        from services.stats_service import get_summary_stats
        return jsonify(get_summary_stats())

    # ── BRAND FEATURE MATRIX ──────────────────────────────────
    @app.route('/api/vehicle/features/<brand>')
    def api_vehicle_features(brand):
        from services.vehicle.feature_matrix import get_features, FEATURE_KEYS
        return jsonify({'brand': brand, 'keys': FEATURE_KEYS, 'features': get_features(brand)})

    # ── HTTPS / SSL ────────────────────────────────────────────
    @app.route('/api/ssl/info')
    def api_ssl_info():
        from services.ssl_service import get_cert_info, _local_ip_guess
        from pathlib import Path
        cert_dir = Path(Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')).parent / 'ssl'
        info = get_cert_info(cert_dir / 'server.crt')
        return jsonify({
            'mode': AppConfig.get('ssl_mode', 'off'),
            'custom_cert': AppConfig.get('ssl_custom_cert', ''),
            'custom_key': AppConfig.get('ssl_custom_key', ''),
            'cert_info': info,
            'cert_path': str(cert_dir / 'server.crt'),
            'lan_ip': _local_ip_guess(),
            'request_scheme': request.scheme,
        })

    @app.route('/api/ssl/save', methods=['POST'])
    def api_ssl_save():
        data = request.get_json() or {}
        mode = data.get('mode', 'off')
        if mode not in ('off', 'auto', 'custom'):
            return jsonify({'error': 'invalid_mode'}), 400
        AppConfig.set('ssl_mode', mode)
        if mode == 'custom':
            AppConfig.set('ssl_custom_cert', data.get('cert', ''))
            AppConfig.set('ssl_custom_key', data.get('key', ''))
        return jsonify({'ok': True, 'restart_required': True})

    @app.route('/api/ssl/generate', methods=['POST'])
    def api_ssl_generate():
        """Force-generate a new self-signed cert (deletes existing one)."""
        from services.ssl_service import _ensure_self_signed_cert, get_cert_info
        from pathlib import Path
        cert_dir = Path(Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')).parent / 'ssl'
        # Delete existing files so the function regenerates
        for name in ('server.crt', 'server.key'):
            p = cert_dir / name
            if p.exists():
                p.unlink()
        try:
            cert_path, _ = _ensure_self_signed_cert(cert_dir)
            return jsonify({'ok': True, 'cert_info': get_cert_info(cert_path), 'restart_required': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/ssl/download/cert')
    def api_ssl_download_cert():
        """Serve the public cert so the user can install it on their phone."""
        from pathlib import Path
        cert_dir = Path(Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')).parent / 'ssl'
        cert = cert_dir / 'server.crt'
        if not cert.exists():
            return jsonify({'error': 'no_cert'}), 404
        return send_file(str(cert), as_attachment=True,
                         download_name='ev-charge-tracker.crt',
                         mimetype='application/x-x509-ca-cert')

    # ── UPDATE ENDPOINTS ───────────────────────────────────────
    @app.route('/api/update/last-rollback', methods=['GET', 'DELETE'])
    def api_update_last_rollback():
        """GET: return the most recent auto-rollback record (or {}).
        DELETE: acknowledge the rollback — clears LAST_ROLLBACK.json so
        the dashboard stops showing the banner on the next page load."""
        from services.update_service import read_last_rollback, clear_last_rollback
        if request.method == 'DELETE':
            clear_last_rollback()
            return jsonify({'ok': True})
        data = read_last_rollback() or {}
        return jsonify(data)

    @app.route('/api/update/check')
    def api_update_check():
        """Check GitHub for a strictly newer release."""
        from updater import check_for_update
        new_version, zip_url = check_for_update()
        return jsonify({
            'current': Config.APP_VERSION,
            'latest': new_version,
            'update_available': bool(new_version),
            'zip_url': zip_url,
            'release_url': f"https://github.com/{Config.GITHUB_REPO}/releases/tag/v{new_version}" if new_version else None,
        })

    @app.route('/api/restart', methods=['POST'])
    def api_restart():
        """Spawn updater_helper in restart-only mode and exit gracefully.

        Used to apply settings that require a fresh process — most importantly
        switching the HTTPS mode after generating a new certificate.
        """
        try:
            from pathlib import Path
            import subprocess as _sp
            app_dir = Path(__file__).resolve().parent
            helper = [
                sys.executable, str(app_dir / 'updater_helper.py'),
                '--app-dir', str(app_dir),
                '--wait-pid', str(os.getpid()),
                '--update-deps', '0',
                '--restart', '1',
            ]
            log_path = app_dir / 'updates' / 'restart.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, 'a', buffering=1)
            log_fh.write(f"\n[restart-button] launching helper at {datetime.now()}\n")

            if os.name == 'nt':
                creationflags = (
                    getattr(_sp, 'DETACHED_PROCESS', 0)
                    | getattr(_sp, 'CREATE_NEW_PROCESS_GROUP', 0)
                )
                _sp.Popen(helper, cwd=str(app_dir),
                          stdin=_sp.DEVNULL, stdout=log_fh, stderr=log_fh,
                          creationflags=creationflags, close_fds=False)
            else:
                _sp.Popen(helper, cwd=str(app_dir),
                          stdin=_sp.DEVNULL, stdout=log_fh, stderr=log_fh,
                          start_new_session=True, close_fds=True)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        # Schedule a delayed graceful shutdown so the JSON response can flush
        import threading
        def _shutdown():
            import time as _t
            _t.sleep(1.5)
            os._exit(0)
        threading.Thread(target=_shutdown, daemon=True).start()
        return jsonify({'ok': True})

    @app.route('/api/factory-reset', methods=['POST'])
    def api_factory_reset():
        """Wipe all app data and restart so the next boot looks like a
        fresh install (no password, no DB, no settings, no credentials).

        Requires the current password — even when auth is disabled, we
        demand a type-to-confirm string so a single accidental POST can
        never nuke the DB. Keeps the Settings-page entry harmless.
        """
        from services.auth_service import (
            is_auth_enabled, is_logged_in, verify_credentials, get_username,
        )

        # Gate 1: must be logged in if auth is on. (The auth guard
        # already enforces this before we reach here, but being
        # explicit protects against future refactors.)
        if is_auth_enabled() and not is_logged_in():
            return jsonify({'error': 'auth_required'}), 401

        data = request.get_json(silent=True) or {}
        confirmation = (data.get('confirmation') or '').strip()
        password = data.get('password') or ''

        # Gate 2: literal typed confirmation — prevents CSRF-style one-click
        # damage and aligns with the UI's "type RESET to confirm" field.
        if confirmation != 'RESET':
            return jsonify({'error': 'confirmation_mismatch'}), 400

        # Gate 3: if auth is configured, verify the password again. If
        # auth is not configured we still accept the request — an install
        # with no password has already opted out of that protection layer.
        if is_auth_enabled():
            if not verify_credentials(get_username(), password):
                return jsonify({'error': 'wrong_password'}), 401

        db_path = Path(DATA_DIR) / 'ev_tracker.db'
        notify_path = Path(DATA_DIR) / 'notify.json'
        safety_backup = None
        try:
            # Dispose SQLAlchemy before touching the file — open fds keep
            # the old inode alive on POSIX, so the restarted process
            # would come up on the stale DB without this.
            try:
                db.session.close()
                db.engine.dispose()
            except Exception:
                pass

            # Last-resort safety backup so a panicked user can restore
            # manually from the filesystem. Not surfaced in the UI — this
            # really is supposed to be a wipe.
            if db_path.is_file():
                backup_dir = Path(DATA_DIR) / 'backups'
                backup_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime('%Y%m%d-%H%M%S')
                safety_backup = backup_dir / f'ev_tracker-pre-factory-reset-{ts}.db'
                shutil.copy2(db_path, safety_backup)
                db_path.unlink()
            if notify_path.is_file():
                notify_path.unlink()
        except Exception as e:
            logger.error(f"Factory reset: file wipe failed: {e}")
            return jsonify({'error': f'wipe_failed: {e}'}), 500

        logger.warning(
            f"FACTORY RESET triggered — DB wiped, safety backup at {safety_backup}. "
            "Restarting service."
        )

        # Restart via systemd in a short-delayed background thread so
        # this response flushes to the browser first. Same pattern as
        # /api/backup/import.
        def _delayed_restart():
            import time as _t
            _t.sleep(0.7)
            try:
                subprocess.run(
                    ['sudo', '-n', '/bin/systemctl', 'restart', 'ev-tracker.service'],
                    timeout=10,
                )
            except Exception:
                pass
            # Belt and suspenders — if sudo restart didn't take (e.g. no
            # systemd on this host, or the sudoers rule is missing), fall
            # back to os._exit so a supervisor still restarts us.
            os._exit(0)

        import threading as _th
        _th.Thread(target=_delayed_restart, daemon=True).start()

        return jsonify({
            'ok': True,
            'safety_backup': str(safety_backup) if safety_backup else None,
        })

    @app.route('/api/update/install', methods=['POST'])
    def api_update_install():
        """Stage an update and trigger a graceful shutdown so the helper
        can swap files and restart the app.

        Safety gate: refuses while the vehicle is actively charging
        (latest VehicleSync.is_charging=True) unless the caller passes
        ``?force=1``. Restarting mid-charge interrupts the background
        sync loop for a few seconds and can miss the charge-end
        transition the app otherwise logs automatically.
        """
        from updater import check_for_update, apply_update
        from models.database import VehicleSync

        force = request.args.get('force') in ('1', 'true', 'yes')
        if not force:
            last_sync = (VehicleSync.query
                         .order_by(VehicleSync.timestamp.desc())
                         .first())
            if last_sync is not None and last_sync.is_charging:
                return jsonify({
                    'error': 'vehicle_charging',
                    'message': (
                        'Das Fahrzeug lädt gerade — Update abgebrochen. '
                        'Nach Ende des Ladevorgangs erneut versuchen '
                        'oder mit „Trotzdem installieren" erzwingen.'
                    ),
                    'last_sync_at': last_sync.timestamp.isoformat(),
                }), 409

        new_version, zip_url = check_for_update()
        if not new_version or not zip_url:
            return jsonify({'error': 'no_update_available'}), 400

        # Pass force through so apply_update's own charging gate doesn't
        # second-guess the endpoint. The endpoint already decided it's
        # okay to proceed (either vehicle not charging, or user clicked
        # "Trotzdem installieren").
        ok = apply_update(zip_url, new_version, force=True)
        if not ok:
            return jsonify({'error': 'update_failed', 'version': new_version}), 500

        # Schedule a delayed graceful shutdown so this response can flush.
        # The helper is already detached and is waiting on our PID with a
        # ~30s budget; 1.5s gives the JSON response time to reach the browser.
        import threading
        def _shutdown():
            import time as _t
            _t.sleep(1.5)
            os._exit(0)
        threading.Thread(target=_shutdown, daemon=True).start()

        return jsonify({
            'staged': True,
            'version': new_version,
            'message': 'Update wird installiert. Die App startet in wenigen Sekunden neu.',
        })

    # ── TRIPS / FAHRTENBUCH ────────────────────────────────────
    @app.route('/trips')
    def trips_page():
        from services.trips_service import (
            get_trips, get_parking_events, get_trip_summary, _load_locations,
            is_brand_supports_location,
        )

        # v2.28.31: the /trips page used to kick off a background
        # ``get_status(force=True)`` when the last GPS sync was > 2 h
        # old. That worked out to 5+ car-wakeup events per day on
        # ev-robert alone (user opens Fahrtenbuch repeatedly from the
        # phone, each visit >2 h after the previous GPS fix in the
        # morning), draining the 12 V aux battery for no real benefit
        # — Fahrtenbuch is a history view, not a live view. Removed:
        # fresh data arrives naturally through the background sync
        # loop (smart mode wakes the car at most once per
        # ``smart_force_max_hours`` window), and the "Jetzt
        # synchronisieren" button remains for on-demand pulls.

        # Kick off a background worker that fills in missing addresses for
        # parking events. Uses Nominatim (1 req/s, permanent DB cache), so
        # after the first full pass this becomes a no-op.
        try:
            from services.trips_service import ParkingEvent as _PE
            missing = (_PE.query.filter(_PE.address.is_(None))
                       .count())
            if missing:
                def _bg_geocode(captured_app):
                    with captured_app.app_context():
                        try:
                            from services.trips_service import geocode_missing_events
                            n = geocode_missing_events(limit=50)
                            logger.info(f"trips_page: geocoded {n} events")
                        except Exception as e:
                            logger.warning(f"trips_page geocode failed: {e}")
                import threading as _th2
                _th2.Thread(target=_bg_geocode, args=(app,), daemon=True).start()
        except Exception as e:
            logger.warning(f"trips_page geocode dispatch failed: {e}")

        # v2.29 fleet picker
        _picker = _active_vehicle_id()
        _vid = _picker if isinstance(_picker, int) else None
        trips = get_trips(limit=200, vehicle_id=_vid)
        events = get_parking_events(limit=200)
        if _vid is not None:
            events = [e for e in events if e.vehicle_id == _vid]
        summary = get_trip_summary(vehicle_id=_vid)
        locations = _load_locations()

        # Freshness info for the UI
        last_with_gps = (VehicleSync.query
                         .filter(VehicleSync.location_lat.isnot(None))
                         .order_by(VehicleSync.timestamp.desc())
                         .first())
        gps_freshness = None
        if last_with_gps:
            gps_freshness = {
                'timestamp': last_with_gps.timestamp.isoformat(),
                'minutes_ago': int((datetime.now() - last_with_gps.timestamp).total_seconds() / 60),
            }

        return render_template('trips.html',
                               gps_freshness=gps_freshness,
                               vehicle_brand=AppConfig.get('vehicle_api_brand', ''),
                               trips=trips, events=[
                                   {'id': e.id, 'arrived_at': e.arrived_at.isoformat(),
                                    'departed_at': e.departed_at.isoformat() if e.departed_at else None,
                                    'lat': e.lat, 'lon': e.lon, 'label': e.label,
                                    'name': e.favorite_name, 'address': e.address,
                                    'odo_in': e.odometer_arrived, 'odo_out': e.odometer_departed,
                                    'soc_in': e.soc_arrived, 'soc_out': e.soc_departed}
                                   for e in events
                                   if e.label != 'unknown'
                               ], summary=summary, locations=locations)

    @app.route('/api/trips/export.csv')
    def trips_export_csv():
        from services.trips_service import get_trips
        import csv, io as _io
        trips = get_trips()
        out = _io.StringIO()
        writer = csv.writer(out, delimiter=';')
        writer.writerow(['Datum', 'Von', 'Nach', 'km', 'SoC %'])
        for t in trips:
            from_label = (t['from'].get('address')
                          or t['from'].get('name')
                          or t['from'].get('label') or '')
            to_label = (t['to'].get('address')
                        or t['to'].get('name')
                        or t['to'].get('label') or '')
            writer.writerow([
                (t['from']['departed_at'] or '')[:10],
                from_label, to_label,
                t['km'] or '', t['soc_used'] or '',
            ])
        from flask import Response
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment;filename=fahrtenbuch.csv'})

    @app.route('/api/trips/export.gpx')
    def trips_export_gpx():
        from services.trips_service import get_parking_events
        events = [e for e in reversed(get_parking_events())
                  if e.label != 'unknown']  # chronological, drop sentinel 0,0
        from flask import Response
        from xml.sax.saxutils import escape
        out = ['<?xml version="1.0" encoding="UTF-8"?>']
        out.append('<gpx version="1.1" creator="EV Charge Tracker" xmlns="http://www.topografix.com/GPX/1/1">')
        for e in events:
            name = escape(e.favorite_name or e.label or 'Park')
            time = e.arrived_at.strftime('%Y-%m-%dT%H:%M:%SZ')
            out.append(f'  <wpt lat="{e.lat:.6f}" lon="{e.lon:.6f}">')
            out.append(f'    <name>{name}</name>')
            out.append(f'    <time>{time}</time>')
            out.append('  </wpt>')
        # Track connecting consecutive events
        out.append('  <trk><name>Fahrtenbuch</name><trkseg>')
        for e in events:
            time = e.arrived_at.strftime('%Y-%m-%dT%H:%M:%SZ')
            out.append(f'    <trkpt lat="{e.lat:.6f}" lon="{e.lon:.6f}"><time>{time}</time></trkpt>')
        out.append('  </trkseg></trk>')
        out.append('</gpx>')
        return Response('\n'.join(out), mimetype='application/gpx+xml',
                        headers={'Content-Disposition': 'attachment;filename=fahrtenbuch.gpx'})

    @app.route('/api/parking_event/<int:event_id>', methods=['GET', 'POST'])
    def api_parking_event(event_id):
        """Read or update a parking event.

        Every column that represents user-facing trip data is editable:
        label, favorite_name, address, coordinates, arrival/departure
        timestamps, odometer and SoC at arrival/departure. Trip km, SoC
        delta and regen-kWh are re-derived from these on the next page
        render — no stored override.

        Old entries (>7 days) require an explicit ``confirm_old`` flag
        in the POST body so a casual click can't silently rewrite a
        week's worth of history.
        """
        from models.database import ParkingEvent
        evt = ParkingEvent.query.get_or_404(event_id)

        def _iso(dt):
            return dt.isoformat() if dt else None

        if request.method == 'GET':
            return jsonify({
                'id': evt.id,
                'label': evt.label,
                'favorite_name': evt.favorite_name,
                'address': evt.address,
                'lat': evt.lat, 'lon': evt.lon,
                'arrived_at': _iso(evt.arrived_at),
                'departed_at': _iso(evt.departed_at),
                'odometer_arrived': evt.odometer_arrived,
                'odometer_departed': evt.odometer_departed,
                'soc_arrived': evt.soc_arrived,
                'soc_departed': evt.soc_departed,
                'age_days': (datetime.now() - evt.arrived_at).days if evt.arrived_at else None,
            })

        data = request.get_json() or {}
        age_days = (datetime.now() - evt.arrived_at).days if evt.arrived_at else 0
        if age_days > 7 and not data.get('confirm_old'):
            return jsonify({'error': 'old_entry_requires_confirm', 'age_days': age_days}), 409

        def _parse_dt(s):
            """Accept ``YYYY-MM-DDTHH:MM`` and full ISO with seconds."""
            if s is None:
                return None
            if s == '':
                return False  # sentinel for "clear"
            try:
                return datetime.fromisoformat(s.replace('Z', '+00:00').split('+')[0])
            except ValueError:
                return 'INVALID'

        def _parse_num(raw, kind='float'):
            if raw is None or raw == '':
                return None if raw == '' else '_skip_'
            try:
                return float(raw) if kind == 'float' else int(raw)
            except (TypeError, ValueError):
                return 'INVALID'

        new_label = data.get('label')
        if new_label is not None:
            if new_label in ('home', 'work', 'favorite', 'other', ''):
                evt.label = new_label or 'other'
            else:
                return jsonify({'error': 'invalid_label'}), 400
        if 'favorite_name' in data:
            name = (data.get('favorite_name') or '').strip()
            evt.favorite_name = name[:120] if name else None
        if 'address' in data:
            addr = (data.get('address') or '').strip()
            evt.address = addr if addr else None
        if 'lat' in data and data['lat'] not in (None, ''):
            try:
                evt.lat = float(data['lat'])
            except (TypeError, ValueError):
                return jsonify({'error': 'invalid_lat'}), 400
        if 'lon' in data and data['lon'] not in (None, ''):
            try:
                evt.lon = float(data['lon'])
            except (TypeError, ValueError):
                return jsonify({'error': 'invalid_lon'}), 400

        # Times — HTML datetime-local sends "YYYY-MM-DDTHH:MM". Empty
        # string clears the column (only meaningful for departed_at).
        for col in ('arrived_at', 'departed_at'):
            if col in data:
                parsed = _parse_dt(data[col])
                if parsed == 'INVALID':
                    return jsonify({'error': f'invalid_{col}'}), 400
                if parsed is False:
                    # clear only allowed for departed_at
                    if col == 'arrived_at':
                        return jsonify({'error': 'arrived_at_required'}), 400
                    setattr(evt, col, None)
                elif parsed is not None:
                    setattr(evt, col, parsed)

        for col, kind in (('odometer_arrived', 'int'), ('odometer_departed', 'int'),
                          ('soc_arrived', 'int'), ('soc_departed', 'int')):
            if col in data:
                parsed = _parse_num(data[col], kind=kind)
                if parsed == 'INVALID':
                    return jsonify({'error': f'invalid_{col}'}), 400
                if parsed == '_skip_':
                    # key sent but missing — leave alone
                    continue
                setattr(evt, col, parsed)  # None clears, number sets

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500
        return jsonify({
            'ok': True,
            'id': evt.id,
            'label': evt.label,
            'favorite_name': evt.favorite_name,
            'address': evt.address,
            'lat': evt.lat, 'lon': evt.lon,
            'arrived_at': _iso(evt.arrived_at),
            'departed_at': _iso(evt.departed_at),
        })

    @app.route('/api/trips/<int:from_id>/<int:to_id>/split_data', methods=['GET'])
    def api_trip_split_data(from_id, to_id):
        """Return everything the trip-split modal needs.

        Validates that ``from_id`` and ``to_id`` are real ParkingEvents
        belonging to the same vehicle with a valid trip window
        (``from.departed_at < to.arrived_at``), then returns the syncs
        inside that window plus a list of detected stationary
        "candidate stops" (the same heuristic the user-facing UX is
        about to ask them to pick from)."""
        from models.database import ParkingEvent, VehicleSync
        from services.trips_service import (
            _classify_location, _haversine_m, _load_locations,
        )

        ev_from = ParkingEvent.query.get_or_404(from_id)
        ev_to = ParkingEvent.query.get_or_404(to_id)
        if ev_from.vehicle_id != ev_to.vehicle_id:
            return jsonify({'error': 'vehicle_mismatch'}), 400
        if not ev_from.departed_at or not ev_to.arrived_at:
            return jsonify({'error': 'incomplete_trip'}), 400
        if ev_from.departed_at > ev_to.arrived_at:
            return jsonify({'error': 'invalid_trip_window'}), 400
        # v3.0.40: a non-trivially-splittable trip needs at least ~5 s
        # of window so the inserted stop has somewhere strictly inside
        # both endpoints to live. Zero-duration legacy trips (departed
        # and arrived on the exact same sync) get a clean 422 with the
        # actual endpoint timestamps so the UI can explain what's
        # wrong instead of dropping the user into a broken modal.
        trip_seconds = (ev_to.arrived_at - ev_from.departed_at).total_seconds()
        if trip_seconds < 5:
            return jsonify({
                'error': 'trip_too_short',
                'trip_seconds': trip_seconds,
                'from_departed_at': ev_from.departed_at.isoformat(),
                'to_arrived_at': ev_to.arrived_at.isoformat(),
            }), 422

        syncs_q = (VehicleSync.query
                   .filter(VehicleSync.vehicle_id == ev_from.vehicle_id)
                   .filter(VehicleSync.timestamp > ev_from.departed_at)
                   .filter(VehicleSync.timestamp < ev_to.arrived_at)
                   .filter(VehicleSync.location_lat.isnot(None))
                   .filter(VehicleSync.location_lon.isnot(None))
                   .order_by(VehicleSync.timestamp.asc()))
        syncs = syncs_q.all()

        # Stationary candidate detection: walk the syncs, group runs of
        # consecutive rows whose location stays within ~150 m of the
        # cluster's anchor. Any cluster spanning ≥ 5 min becomes a
        # candidate stop. 150 m is generous on purpose — GPS drift on
        # parked Korean cars is wider than the 100 m used elsewhere in
        # the codebase, and a stop is only interesting if it's already
        # the user's intent (intentional 5 min+ wait, not a red light).
        STATIONARY_RADIUS_M = 150.0
        STATIONARY_MIN_MIN = 5
        candidates = []
        cluster = []  # list of (idx, sync)
        for i, s in enumerate(syncs):
            if not cluster:
                cluster.append((i, s))
                continue
            anchor = cluster[0][1]
            if _haversine_m(anchor.location_lat, anchor.location_lon,
                            s.location_lat, s.location_lon) <= STATIONARY_RADIUS_M:
                cluster.append((i, s))
            else:
                if len(cluster) >= 2:
                    dur = (cluster[-1][1].timestamp
                           - cluster[0][1].timestamp).total_seconds() / 60
                    if dur >= STATIONARY_MIN_MIN:
                        candidates.append(cluster)
                cluster = [(i, s)]
        if len(cluster) >= 2:
            dur = (cluster[-1][1].timestamp
                   - cluster[0][1].timestamp).total_seconds() / 60
            if dur >= STATIONARY_MIN_MIN:
                candidates.append(cluster)

        cand_out = []
        locs = _load_locations()
        for grp in candidates:
            first = grp[0][1]
            last = grp[-1][1]
            # Cluster centroid (mean lat/lon) for a stable label point.
            mean_lat = sum(s.location_lat for _, s in grp) / len(grp)
            mean_lon = sum(s.location_lon for _, s in grp) / len(grp)
            lbl, fav = _classify_location(mean_lat, mean_lon, locs)
            cand_out.append({
                'arrived_at': first.timestamp.isoformat(),
                'departed_at': last.timestamp.isoformat(),
                'lat': round(mean_lat, 6),
                'lon': round(mean_lon, 6),
                'duration_min': int((last.timestamp - first.timestamp).total_seconds() / 60),
                'label_hint': lbl,
                'favorite_name': fav,
                'odometer_arrived': first.odometer_km,
                'odometer_departed': last.odometer_km,
                'soc_arrived': first.soc_percent,
                'soc_departed': last.soc_percent,
            })

        return jsonify({
            'from': {
                'id': ev_from.id, 'lat': ev_from.lat, 'lon': ev_from.lon,
                'departed_at': ev_from.departed_at.isoformat(),
                'label': ev_from.label, 'name': ev_from.favorite_name,
            },
            'to': {
                'id': ev_to.id, 'lat': ev_to.lat, 'lon': ev_to.lon,
                'arrived_at': ev_to.arrived_at.isoformat(),
                'label': ev_to.label, 'name': ev_to.favorite_name,
            },
            'syncs': [{
                'timestamp': s.timestamp.isoformat(),
                'lat': s.location_lat, 'lon': s.location_lon,
                'odometer': s.odometer_km, 'soc': s.soc_percent,
            } for s in syncs],
            'candidates': cand_out,
        })

    @app.route('/api/trips/<int:from_id>/<int:to_id>/split', methods=['POST'])
    def api_trip_split(from_id, to_id):
        """Insert one or more new ParkingEvents between two PEs,
        splitting the trip into N+1 segments.

        Body accepts EITHER the legacy single form
        ``{at_arrived, at_departed?, lat, lon}`` OR the multi form
        ``{stops: [{at_arrived, at_departed?, lat, lon}, …]}`` — the UI
        sends ``stops`` so the user can drop several pins in one go.
        Each stop's odometer + SoC are sourced from the VehicleSync
        row nearest its arrival/departure timestamps. Label is
        auto-classified against home / work / favorites for every stop.
        Stops are inserted in arrival-order; the response returns the
        list of new event ids."""
        from models.database import ParkingEvent, VehicleSync
        from services.trips_service import _classify_location

        ev_from = ParkingEvent.query.get_or_404(from_id)
        ev_to = ParkingEvent.query.get_or_404(to_id)
        if ev_from.vehicle_id != ev_to.vehicle_id:
            return jsonify({'error': 'vehicle_mismatch'}), 400
        if not ev_from.departed_at or not ev_to.arrived_at:
            return jsonify({'error': 'incomplete_trip'}), 400

        data = request.get_json() or {}
        raw_stops = data.get('stops')
        if not isinstance(raw_stops, list):
            raw_stops = [data]  # legacy single-stop form

        # Pull every sync inside the trip window once — the multi-stop
        # case would otherwise re-query for every pin.
        in_range_syncs = (VehicleSync.query
                          .filter(VehicleSync.vehicle_id == ev_from.vehicle_id)
                          .filter(VehicleSync.timestamp >= ev_from.departed_at)
                          .filter(VehicleSync.timestamp <= ev_to.arrived_at)
                          .all())

        def _nearest_sync(ts):
            if not in_range_syncs:
                return None
            return min(in_range_syncs,
                       key=lambda r: abs((r.timestamp - ts).total_seconds()))

        parsed = []
        for raw in raw_stops:
            try:
                at_arr = datetime.fromisoformat(
                    raw['at_arrived'].replace('Z', '+00:00').split('+')[0])
                at_dep_raw = raw.get('at_departed') or raw['at_arrived']
                at_dep = datetime.fromisoformat(
                    at_dep_raw.replace('Z', '+00:00').split('+')[0])
                lat = float(raw['lat'])
                lon = float(raw['lon'])
            except (KeyError, ValueError, TypeError, AttributeError):
                return jsonify({'error': 'invalid_payload'}), 400
            if not (ev_from.departed_at < at_arr <= at_dep < ev_to.arrived_at):
                return jsonify({'error': 'split_outside_window',
                                'at_arrived': at_arr.isoformat()}), 400
            parsed.append({'at_arrived': at_arr, 'at_departed': at_dep,
                           'lat': lat, 'lon': lon})

        parsed.sort(key=lambda s: s['at_arrived'])
        # Reject overlapping stops — keeps the trip-rendering invariant
        # (each PE pair must be chronologically clean).
        for a, b in zip(parsed, parsed[1:]):
            if a['at_departed'] >= b['at_arrived']:
                return jsonify({'error': 'stops_overlap'}), 400

        new_ids = []
        try:
            for stop in parsed:
                s_arr = _nearest_sync(stop['at_arrived'])
                s_dep = (_nearest_sync(stop['at_departed'])
                         if stop['at_departed'] != stop['at_arrived']
                         else s_arr)
                lbl, fav = _classify_location(stop['lat'], stop['lon'])
                new_evt = ParkingEvent(
                    vehicle_id=ev_from.vehicle_id,
                    arrived_at=stop['at_arrived'],
                    departed_at=stop['at_departed'],
                    last_seen_at=s_dep.timestamp if s_dep else stop['at_departed'],
                    lat=stop['lat'], lon=stop['lon'],
                    label=lbl or 'other',
                    favorite_name=fav,
                    odometer_arrived=(s_arr.odometer_km if s_arr else None),
                    odometer_departed=(s_dep.odometer_km if s_dep else None),
                    soc_arrived=(s_arr.soc_percent if s_arr else None),
                    soc_departed=(s_dep.soc_percent if s_dep else None),
                )
                db.session.add(new_evt)
                db.session.flush()
                new_ids.append(new_evt.id)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500
        return jsonify({'ok': True, 'new_event_ids': new_ids})

    @app.route('/api/trips/geocode_missing', methods=['POST'])
    def api_trips_geocode_missing():
        """Resolve addresses for parking events without one (manual trigger)."""
        from services.trips_service import geocode_missing_events
        try:
            n = geocode_missing_events(limit=100)
            return jsonify({'ok': True, 'filled': n})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trips/backfill', methods=['POST'])
    def api_trips_backfill():
        """Replay all vehicle syncs through the parking-event hook.

        Body: {wipe: bool} — if true, deletes existing events first.
        """
        from services.trips_service import backfill_parking_events
        data = request.get_json(silent=True) or {}
        wipe = bool(data.get('wipe', False))
        try:
            summary = backfill_parking_events(wipe_existing=wipe)
            return jsonify({'ok': True, **summary})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trips/repair_soc', methods=['POST'])
    def api_trips_repair_soc():
        """Realign every PE's ``soc_arrived`` / ``soc_departed`` with the
        VehicleSync-derived values the trip row already displays.

        Fixes the Kia/Hyundai "first post-drive sync carries pre-drive
        SoC" echo that leaves PE fields out of sync with the Fahrtenbuch
        delta. Safe to run multiple times; only writes when a stored
        value differs from the recomputed one."""
        from services.trips_service import repair_all_pe_soc
        try:
            return jsonify({'ok': True, **repair_all_pe_soc()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trips/sdk_backfill', methods=['POST'])
    def api_trips_sdk_backfill():
        """Pull per-trip data from the Kia/Hyundai server for the last N
        days and store them as VehicleTrip rows. Server-side call only
        (no car wake-up, no 12V drain); counts one per day against the
        200/vehicle daily API budget."""
        brand = AppConfig.get('vehicle_api_brand', '')
        if brand not in ('kia', 'hyundai'):
            return jsonify({'error': 'brand_not_supported',
                            'hint': 'SDK trip log only for Kia/Hyundai'}), 400

        data = request.get_json(silent=True) or {}
        try:
            days = int(data.get('days', 30))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 90))  # cap at 90 days to avoid burning the quota

        from services.vehicle.trip_log_fetch import backfill
        try:
            summary = backfill(days=days)
            return jsonify({'ok': True, **summary})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trips/sync_now', methods=['POST'])
    def api_trips_sync_now():
        """Trigger a force vehicle sync from the trips page."""
        brand = AppConfig.get('vehicle_api_brand', '')
        if not brand:
            return jsonify({'error': 'no_brand'}), 400

        confirm_low_12v_t = (
            request.args.get('confirm_low_12v', '0') == '1'
            or request.form.get('confirm_low_12v', '0') == '1'
        )
        if not confirm_low_12v_t:
            from services.vehicle.sync_service import (
                is_12v_low, _latest_12v_percent, LOW_12V_THRESHOLD_PERCENT
            )
            _picker_t = _active_vehicle_id()
            _vid_t = _picker_t if isinstance(_picker_t, int) else None
            if _vid_t is None:
                from models.database import Vehicle as _Vt
                _vt = (_Vt.query.filter_by(is_archived=False)
                       .order_by(_Vt.id.asc()).first())
                _vid_t = _vt.id if _vt else None
            if _vid_t and is_12v_low(_vid_t):
                return jsonify({
                    'error': 'low_12v',
                    'battery_12v': _latest_12v_percent(_vid_t),
                    'threshold': LOW_12V_THRESHOLD_PERCENT,
                    'message': 'low_12v_confirmation_required',
                }), 423

        # Rate limiter check (Kia EU 200/day)
        today_str = date.today().isoformat()
        counter_date = AppConfig.get('vehicle_api_counter_date', '')
        if counter_date != today_str:
            AppConfig.set('vehicle_api_counter_date', today_str)
            AppConfig.set('vehicle_api_counter', '0')
        api_count = int(AppConfig.get('vehicle_api_counter', '0'))
        if api_count >= 190:
            return jsonify({'error': f'rate_limit ({api_count}/200)'}), 429
        AppConfig.set('vehicle_api_counter', str(api_count + 1))

        try:
            from services.vehicle import get_connector
            from services.vehicle.sync_service import log_sync_result
            import json as _json
            creds = _get_vehicle_credentials()
            connector = get_connector(brand, creds)
            status = connector.get_status(force=True)
            # v2.29: stamp the picker's active (or first non-archived)
            # vehicle so the row is correctly attributed in fleets.
            _picker = _active_vehicle_id()
            _stamp_vid = _picker if isinstance(_picker, int) else None
            if _stamp_vid is None:
                from models.database import Vehicle as _V
                _v = (_V.query.filter_by(is_archived=False)
                      .order_by(_V.id.asc()).first()
                      or _V.query.order_by(_V.id.asc()).first())
                _stamp_vid = _v.id if _v else None
            sync = _save_vehicle_sync(status, _get_battery_kwh(vehicle_id=_stamp_vid),
                                      raw_json=_json.dumps(status.raw_data, default=str),
                                      vehicle_id=_stamp_vid)
            log_sync_result(status, mode_label='force', source='manual')
            return jsonify({
                'ok': True,
                'has_location': sync.location_lat is not None,
                'lat': sync.location_lat,
                'lon': sync.location_lon,
                'soc': sync.soc_percent,
                'odometer': sync.odometer_km,
                'timestamp': sync.timestamp.isoformat() if sync.timestamp else None,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/locations/save', methods=['POST'])
    def api_locations_save():
        """Save home or work location from JSON {kind, lat, lon, label}."""
        data = request.get_json() or {}
        kind = data.get('kind')
        if kind not in ('home', 'work'):
            return jsonify({'error': 'invalid_kind'}), 400
        try:
            lat = float(data.get('lat'))
            lon = float(data.get('lon'))
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_coords'}), 400
        AppConfig.set(f'{kind}_lat', lat)
        AppConfig.set(f'{kind}_lon', lon)
        if data.get('label'):
            AppConfig.set(f'{kind}_label', data['label'])

        # Reclassify all events with the new locations
        from services.trips_service import reclassify_all_events
        n = reclassify_all_events()
        return jsonify({'ok': True, 'reclassified': n})

    @app.route('/api/locations/favorites', methods=['GET', 'POST', 'PUT', 'DELETE'])
    def api_locations_favorites():
        import json as _json
        favs_raw = AppConfig.get('favorite_locations', '[]')
        try:
            favs = _json.loads(favs_raw)
            if not isinstance(favs, list):
                favs = []
        except _json.JSONDecodeError:
            favs = []

        if request.method == 'GET':
            return jsonify({'favorites': favs})

        if request.method == 'POST':
            data = request.get_json() or {}
            try:
                fav = {'name': str(data.get('name', 'Favorit'))[:60],
                       'lat': float(data['lat']), 'lon': float(data['lon'])}
            except (TypeError, ValueError, KeyError):
                return jsonify({'error': 'invalid'}), 400
            favs.append(fav)
            AppConfig.set('favorite_locations', _json.dumps(favs))
            from services.trips_service import reclassify_all_events
            reclassify_all_events()
            return jsonify({'ok': True, 'favorites': favs})

        if request.method == 'PUT':
            # Update an existing favorite by index. Any of name/lat/lon
            # can be changed; missing keys keep the current value so the
            # UI can do name-only or coord-only patches.
            data = request.get_json() or {}
            try:
                idx = int(data.get('index'))
                fav = favs[idx]
            except (TypeError, ValueError, IndexError, KeyError):
                return jsonify({'error': 'invalid_index'}), 400
            if 'name' in data:
                fav['name'] = str(data['name'])[:60].strip() or fav.get('name', 'Favorit')
            if 'lat' in data:
                try:
                    fav['lat'] = float(data['lat'])
                except (TypeError, ValueError):
                    return jsonify({'error': 'invalid_lat'}), 400
            if 'lon' in data:
                try:
                    fav['lon'] = float(data['lon'])
                except (TypeError, ValueError):
                    return jsonify({'error': 'invalid_lon'}), 400
            favs[idx] = fav
            AppConfig.set('favorite_locations', _json.dumps(favs))
            from services.trips_service import reclassify_all_events
            reclassify_all_events()
            return jsonify({'ok': True, 'favorites': favs})

        if request.method == 'DELETE':
            data = request.get_json() or {}
            idx = data.get('index')
            try:
                favs.pop(int(idx))
            except (TypeError, ValueError, IndexError):
                return jsonify({'error': 'invalid_index'}), 400
            AppConfig.set('favorite_locations', _json.dumps(favs))
            from services.trips_service import reclassify_all_events
            reclassify_all_events()
            return jsonify({'ok': True, 'favorites': favs})

    @app.route('/api/locations/reverse')
    def api_locations_reverse():
        try:
            lat = float(request.args.get('lat'))
            lon = float(request.args.get('lon'))
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_coords'}), 400
        from services.geocode_service import reverse
        addr = reverse(lat, lon, language=AppConfig.get('app_language', 'de'))
        return jsonify({'address': addr})

    # ── LOG VIEWER ─────────────────────────────────────────────
    @app.route('/logs')
    def logs_page():
        req_log_enabled = AppConfig.get('log_show_requests', 'false') == 'true'
        return render_template('logs.html', req_log_enabled=req_log_enabled)

    @app.route('/api/logs')
    def api_logs():
        from services.log_service import get_entries
        try:
            after = int(request.args.get('after', '0'))
        except (TypeError, ValueError):
            after = 0
        level = request.args.get('level') or None
        include_req = request.args.get('include_requests', '1') != '0'
        try:
            limit = int(request.args.get('limit', '500'))
        except (TypeError, ValueError):
            limit = 500
        entries = get_entries(after_id=after, level=level,
                              include_requests=include_req, limit=limit)
        return jsonify({'entries': entries})

    @app.route('/api/logs/clear', methods=['POST'])
    def api_logs_clear():
        from services.log_service import clear
        clear()
        return jsonify({'ok': True})

    @app.route('/api/logs/requests', methods=['POST'])
    def api_logs_requests():
        """Toggle werkzeug HTTP access logging on/off."""
        from services.log_service import set_request_logging
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get('enabled', False))
        set_request_logging(enabled)
        AppConfig.set('log_show_requests', 'true' if enabled else 'false')
        return jsonify({'ok': True, 'enabled': enabled})

    # ── MAINTENANCE / WARTUNG ──────────────────────────────────
    @app.route('/maintenance', methods=['GET', 'POST'])
    def maintenance_page():
        from services.maintenance_service import (
            list_entries, get_due_items, get_summary, add_entry, DEFAULT_INTERVALS,
        )

        # v2.29 picker: filter list/summary/due by active vehicle.
        _picker = _active_vehicle_id()
        _vid = _picker if isinstance(_picker, int) else None

        if request.method == 'POST':
            try:
                date_str = request.form.get('date')
                # Stamp the new entry with the picker's vehicle (or
                # the form-submitted one if "Alle" was active and
                # the user explicitly chose a target vehicle there).
                form_vid = _int(request.form.get('vehicle_id'))
                stamp_vid = form_vid if form_vid else _vid
                if stamp_vid is None:
                    from models.database import Vehicle
                    _v = (Vehicle.query.filter_by(is_archived=False)
                          .order_by(Vehicle.id.asc()).first()
                          or Vehicle.query.order_by(Vehicle.id.asc()).first())
                    stamp_vid = _v.id if _v else None
                add_entry(
                    date_=datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today(),
                    item_type=request.form.get('item_type', 'other'),
                    title=request.form.get('title', '').strip() or None,
                    odometer_km=_int(request.form.get('odometer_km')),
                    cost_eur=_float(request.form.get('cost_eur')),
                    notes=request.form.get('notes', '').strip() or None,
                    next_due_km=_int(request.form.get('next_due_km')),
                    next_due_date=(datetime.strptime(request.form.get('next_due_date'), '%Y-%m-%d').date()
                                    if request.form.get('next_due_date') else None),
                    vehicle_id=stamp_vid,
                )
                flash(t('flash.maintenance_saved'), 'success')
            except Exception as e:
                flash(t('flash.error', error=e), 'danger')
            return redirect(url_for('maintenance_page'))

        # Determine current odometer (latest charge or vehicle sync) —
        # scoped to the picker so a multi-car install shows the right
        # mileage per vehicle.
        cq = Charge.query.filter(Charge.odometer.isnot(None))
        if _vid is not None:
            cq = cq.filter_by(vehicle_id=_vid)
        last_charge = cq.order_by(Charge.date.desc()).first()
        current_odo = last_charge.odometer if last_charge else None
        sq = VehicleSync.query.filter(VehicleSync.odometer_km.isnot(None))
        if _vid is not None:
            sq = sq.filter_by(vehicle_id=_vid)
        last_sync = sq.order_by(VehicleSync.timestamp.desc()).first()
        if last_sync and (current_odo is None or (last_sync.odometer_km or 0) > current_odo):
            current_odo = last_sync.odometer_km

        return render_template('maintenance.html',
                               entries=list_entries(vehicle_id=_vid),
                               due_items=get_due_items(current_odo, vehicle_id=_vid),
                               summary=get_summary(vehicle_id=_vid),
                               current_odo=current_odo,
                               today=date.today().isoformat(),
                               default_intervals=DEFAULT_INTERVALS)

    @app.route('/maintenance/edit/<int:entry_id>', methods=['POST'])
    def maintenance_edit(entry_id):
        """v3.0.26: in-place edit endpoint for the maintenance modal.
        Same field shape as the /maintenance create POST; only updates
        the targeted entry instead of inserting a new one."""
        from services.maintenance_service import update_entry
        try:
            date_str = request.form.get('date')
            next_date_str = request.form.get('next_due_date')
            updated = update_entry(
                entry_id,
                date=datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else None,
                item_type=request.form.get('item_type', 'other'),
                title=request.form.get('title', '').strip() or None,
                odometer_km=_int(request.form.get('odometer_km')),
                cost_eur=_float(request.form.get('cost_eur')),
                notes=request.form.get('notes', '').strip() or None,
                next_due_km=_int(request.form.get('next_due_km')),
                next_due_date=(datetime.strptime(next_date_str, '%Y-%m-%d').date()
                               if next_date_str else None),
            )
            if updated is not None:
                flash(t('flash.entry_updated'), 'success')
        except Exception as e:
            flash(t('flash.error', error=e), 'danger')
        return redirect(url_for('maintenance_page'))

    @app.route('/api/maintenance/<int:entry_id>', methods=['GET'])
    def api_get_maintenance(entry_id):
        """Return a single MaintenanceEntry as JSON for the edit modal
        to populate from. The MaintenanceEntry model has no to_dict()
        helper, so the field shape is built here once."""
        from models.database import MaintenanceEntry
        e = MaintenanceEntry.query.get_or_404(entry_id)
        return jsonify({
            'id': e.id,
            'date': e.date.isoformat() if e.date else None,
            'item_type': e.item_type,
            'title': e.title,
            'odometer_km': e.odometer_km,
            'cost_eur': e.cost_eur,
            'notes': e.notes,
            'next_due_km': e.next_due_km,
            'next_due_date': e.next_due_date.isoformat() if e.next_due_date else None,
            'vehicle_id': e.vehicle_id,
        })

    @app.route('/maintenance/delete/<int:entry_id>', methods=['POST'])
    def maintenance_delete(entry_id):
        from services.maintenance_service import delete_entry
        if delete_entry(entry_id):
            flash(t('flash.maintenance_deleted'), 'warning')
        return redirect(url_for('maintenance_page'))

    # ── HIGHLIGHTS / RANGE / WEATHER ───────────────────────────
    @app.route('/api/highlights')
    def api_highlights():
        from services.highlights_service import get_highlights, get_charging_stations
        return jsonify({
            'highlights': get_highlights(),
            'stations': get_charging_stations(limit=10),
        })

    @app.route('/api/range')
    def api_range():
        """Realistic range estimate based on SoC + recent consumption + temp."""
        from services.highlights_service import calculate_range
        from services.weather_service import _get_home_coords, fetch_temp_for_date
        try:
            soc = float(request.args.get('soc')) if request.args.get('soc') else None
        except (TypeError, ValueError):
            soc = None
        if soc is None:
            last = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()
            if last and last.soc_percent:
                soc = last.soc_percent
        if soc is None:
            return jsonify({'error': 'no_soc'}), 400

        battery = _get_battery_kwh()
        # Recent consumption: prefer 30d API value, fallback to total avg
        cons = None
        last = VehicleSync.query.filter(VehicleSync.consumption_30d_kwh_per_100km.isnot(None)) \
                                .order_by(VehicleSync.timestamp.desc()).first()
        if last and last.consumption_30d_kwh_per_100km:
            cons = float(last.consumption_30d_kwh_per_100km)
        else:
            from services.stats_service import get_summary_stats
            stats = get_summary_stats() or {}
            cons = stats.get('consumption_with_recup') or None
        if not cons:
            return jsonify({'error': 'no_consumption'}), 400

        # Current temp from Open-Meteo at home coords
        temp_c = None
        home = _get_home_coords()
        if home:
            try:
                temp_c = fetch_temp_for_date(date.today(), home[0], home[1])
            except Exception:
                pass

        result = calculate_range(soc, battery, cons, temp_c=temp_c)
        if not result:
            return jsonify({'error': 'calc_failed'}), 400
        result['soc'] = soc
        result['battery_kwh'] = battery
        result['consumption'] = round(cons, 1)
        result['temp_c'] = round(temp_c, 1) if temp_c is not None else None
        return jsonify(result)

    @app.route('/api/weather/correlation')
    def api_weather_correlation():
        from services.weather_service import get_consumption_temperature_correlation
        data = get_consumption_temperature_correlation(months=12)
        if not data:
            return jsonify({'error': 'no_data'}), 404
        return jsonify({'months': data})

    @app.route('/api/import/preview', methods=['POST'])
    def api_import_preview():
        """Dry-run an import and return what would happen. The CSV is
        parsed, columns mapped, each row classified (insert/skip-dup/
        update/skip-empty/error) — all without touching the DB.
        The UI calls this before the user confirms the actual import."""
        import io, json as _json
        from import_gsheet import preview_csv_data, VALID_MODES, ALL_LOGICAL_FIELDS
        file = request.files.get('csv_file')
        if not file or not file.filename:
            return jsonify({'error': 'no_file'}), 400

        mode = request.form.get('import_mode', 'skip')
        if mode not in VALID_MODES:
            mode = 'skip'

        override_raw = request.form.get('column_override', '').strip()
        column_override = None
        if override_raw:
            try:
                parsed = _json.loads(override_raw)
                if isinstance(parsed, dict):
                    column_override = {
                        k: (None if v in (None, '', 'null') else int(v))
                        for k, v in parsed.items()
                        if k in ALL_LOGICAL_FIELDS
                    }
            except (ValueError, TypeError):
                return jsonify({'error': 'invalid_column_override'}), 400

        try:
            stream = io.StringIO(file.stream.read().decode('utf-8', errors='replace'))
            preview = preview_csv_data(stream, mode=mode,
                                       column_override=column_override,
                                       max_rows=20)
        except Exception as e:
            logger.exception('Import preview failed')
            return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

        # Translate the class labels into visible, i18n-friendly labels
        action_labels = {
            'insert':      t('set.import_action_insert'),
            'update':      t('set.import_action_update'),
            'skip_dup':    t('set.import_action_skip_dup'),
            'skip_dup_in_file': t('set.import_action_skip_dup_in_file'),
            'skip_empty':  t('set.import_action_skip_empty'),
            'error':       t('set.import_action_error'),
        }
        preview['action_labels'] = action_labels
        preview['logical_field_labels'] = {
            f: t(f'set.field_{f}') for f in ALL_LOGICAL_FIELDS
        }
        return jsonify(preview)

    @app.route('/api/export/csv')
    def export_csv():
        """Export all charges as CSV."""
        import csv
        import io
        charges = Charge.query.order_by(Charge.date).all()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        # Header names must match the FIELD_ALIASES in import_gsheet.py so
        # that exporting and re-importing is a lossless round-trip.
        writer.writerow([
            'Datum', 'Uhrzeit', 'km-Stand', 'EUR/kWh', 'kWh', 'Kosten',
            'Typ', 'Von%', 'Bis%', 'Geladen%',
            'Verlust_kWh', 'Verlust%', 'CO2_g/kWh', 'CO2_kg',
            'Anbieter', 'Ort', 'Lat', 'Lon', 'Notizen',
        ])
        for c in charges:
            writer.writerow([
                c.date.strftime('%d.%m.%Y') if c.date else '',
                f"{c.charge_hour:02d}:00" if c.charge_hour is not None else '',
                c.odometer if c.odometer is not None else '',
                f"{c.eur_per_kwh:.2f}" if c.eur_per_kwh is not None else '',
                f"{c.kwh_loaded:.3f}" if c.kwh_loaded is not None else '',
                f"{c.total_cost:.2f}" if c.total_cost is not None else '',
                c.charge_type or '',
                c.soc_from if c.soc_from is not None else '',
                c.soc_to if c.soc_to is not None else '',
                c.soc_charged if c.soc_charged is not None else '',
                f"{c.loss_kwh:.3f}" if c.loss_kwh is not None else '',
                f"{c.loss_pct:.2f}" if c.loss_pct is not None else '',
                c.co2_g_per_kwh if c.co2_g_per_kwh is not None else '',
                f"{c.co2_kg:.2f}" if c.co2_kg is not None else '',
                c.operator or '',
                c.location_name or '',
                f"{c.location_lat:.6f}" if c.location_lat is not None else '',
                f"{c.location_lon:.6f}" if c.location_lon is not None else '',
                c.notes or '',
            ])
        from flask import Response
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment;filename=ladevorgaenge_export.csv'}
        )


def _float(val):
    if val is None or val == '':
        return None
    try:
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return None


def _int(val):
    if val is None or val == '':
        return None
    try:
        return int(float(str(val).replace(',', '.')))
    except (ValueError, TypeError):
        return None


if __name__ == '__main__':
    app = create_app()

    # ── HTTPS setup ────────────────────────────────────────
    ssl_context = None
    scheme = 'http'
    with app.app_context():
        ssl_mode = AppConfig.get('ssl_mode', 'off')
        if ssl_mode != 'off':
            try:
                from services.ssl_service import build_ssl_context
                from pathlib import Path
                cert_dir = Path(Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')).parent / 'ssl'
                ssl_context = build_ssl_context(
                    ssl_mode,
                    cert_dir,
                    custom_cert=AppConfig.get('ssl_custom_cert', ''),
                    custom_key=AppConfig.get('ssl_custom_key', ''),
                )
                if ssl_context is not None:
                    scheme = 'https'
            except Exception as e:
                logger.warning(f"HTTPS setup failed: {e} — falling back to HTTP")
                ssl_context = None

    print(f"\n⚡ EV Charge Tracker v{Config.APP_VERSION}")
    print(f"🚗 {Config.CAR_MODEL}")
    print(f"🌐 {scheme}://localhost:{Config.APP_PORT}")
    print(f"📱 Vom Smartphone: {scheme}://<deine-ip>:{Config.APP_PORT}\n")
    # debug=False — the auto-reloader passes a listening socket via the
    # WERKZEUG_SERVER_FD env var, which gets propagated through the updater
    # chain and crashes the freshly-spawned Flask with EBADF on restart.
    # For a self-hosted app, debug mode is the wrong default anyway.
    app.run(host=Config.APP_HOST, port=Config.APP_PORT,
            ssl_context=ssl_context, debug=False)
