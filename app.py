"""EV Charge Tracker - Main Flask Application."""
import io
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

        # Migrate: add last_seen_at to parking_events
        try:
            parking_columns = [c['name'] for c in inspector.get_columns('parking_events')]
            if 'last_seen_at' not in parking_columns:
                db.session.execute(text('ALTER TABLE parking_events ADD COLUMN last_seen_at DATETIME'))
        except Exception:
            pass  # table might not exist yet on a fresh install — create_all() will handle it

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

    # Honor persisted request-log toggle
    try:
        with app.app_context():
            if AppConfig.get('log_show_requests', 'false') == 'true':
                _set_req_log(True)
    except Exception:
        pass

    return app


def _get_pv_co2():
    """Calculate PV CO2 in g/kWh from settings."""
    try:
        yield_kwp = float(AppConfig.get('pv_yield_per_kwp', '950'))
        lifetime = float(AppConfig.get('pv_lifetime', '25'))
        prod_co2 = float(AppConfig.get('pv_production_co2', '1000'))
        if yield_kwp > 0 and lifetime > 0:
            return int(round(prod_co2 / (yield_kwp * lifetime)))
    except (ValueError, TypeError):
        pass
    return 42  # fallback


def _get_vehicle_credentials():
    """Build credentials dict from AppConfig for vehicle API."""
    return {
        'username': AppConfig.get('vehicle_api_username', ''),
        'password': AppConfig.get('vehicle_api_password', ''),
        'pin': AppConfig.get('vehicle_api_pin', ''),
        'region': AppConfig.get('vehicle_api_region', 'EU'),
        'vin': AppConfig.get('vehicle_api_vin', ''),
    }


def _get_battery_kwh():
    val = AppConfig.get('battery_kwh')
    try:
        return float(val) if val else Config.BATTERY_CAPACITY_KWH
    except (ValueError, TypeError):
        return Config.BATTERY_CAPACITY_KWH


# Built-in Anbieter/CPO list — the common German + European operators.
# Users can add custom entries via /api/providers/custom which are stored
# in AppConfig as a JSON list and merged in.
DEFAULT_OPERATORS = [
    'IONITY', 'EnBW mobility+', 'Aral pulse', 'Tesla Supercharger',
    'Shell Recharge', 'Allego', 'Fastned', 'Elli (VW)', 'EWE Go',
    'Maingau EinfachStromLaden', 'Lidl', 'Kaufland', 'Aldi Süd',
    'REWE', 'Mer', 'Stadtwerke', 'Zuhause / privat', 'Arbeit', 'Sonstiges',
]


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


def _get_operator_list():
    """Return the deduplicated union of built-in and user-defined Anbieter
    names for the dropdown. Built-in order is preserved so the common
    names show first."""
    seen = set()
    out = []
    for name in list(DEFAULT_OPERATORS) + _get_custom_operators():
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


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


def _build_vehicle_sync(status, battery_kwh, raw_json=''):
    """Build a VehicleSync row from a connector VehicleStatus."""
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
        raw_json=raw_json,
    )


def _save_vehicle_sync(status, battery_kwh, raw_json=''):
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
    new_sync = _build_vehicle_sync(status, battery_kwh, raw_json=raw_json)
    last = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()

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

    # Always run parking detection on the latest snapshot.
    try:
        from services.trips_service import update_parking_from_sync
        update_parking_from_sync(result)
    except Exception as e:
        logger.warning(f"Failed to update parking event: {e}")

    return result


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
            return jsonify({'error': 'Neue Passphrase und Bestätigung stimmen nicht überein.'}), 400

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
            return jsonify({'error': 'Benutzername darf nicht leer sein.'}), 400
        if password != confirm:
            return jsonify({'error': 'Neues Passwort und Bestätigung stimmen nicht überein.'}), 400

        try:
            set_credentials(username, password)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        # Log the user in immediately so they don't land on the login page
        # right after finishing setup.
        login_user(username)

        mark_step_done('weblogin_done')

        # If both wizard steps are done, clear the marker and state file.
        state = load_state()
        if state.get('luks_done') and state.get('weblogin_done'):
            complete_setup()

        return jsonify({'ok': True, 'message': 'Web-Login angelegt.', 'redirect': '/'})

    @app.route('/api/health', methods=['GET'])
    def api_health():
        return jsonify({'ok': True, 'version': Config.APP_VERSION})

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
            return jsonify({'error': 'Passwörter stimmen nicht überein.'}), 400
        try:
            set_credentials(username, password)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        # Log the owner in immediately so they don't lock themselves out.
        login_user(username)
        return jsonify({'ok': True, 'message': 'Zugangsschutz aktiviert.'})

    @app.route('/api/auth/disable', methods=['POST'])
    def api_auth_disable():
        from services.auth_service import is_logged_in, disable_auth
        # Only an authenticated session may disable auth — this prevents a
        # drive-by POST on an exposed instance from turning the gate off.
        if not is_logged_in():
            return jsonify({'error': 'nicht eingeloggt'}), 401
        disable_auth()
        return jsonify({'ok': True, 'message': 'Zugangsschutz deaktiviert.'})

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
            return jsonify({'error': 'Neue Passwörter stimmen nicht überein.'}), 400
        username = get_username()
        if not verify_credentials(username, current):
            return jsonify({'error': 'Aktuelles Passwort falsch.'}), 400
        try:
            set_credentials(username, new_pw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'ok': True, 'message': 'Passwort geändert.'})

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
            return jsonify({'error': 'Datenbank-Datei nicht gefunden.'}), 404
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
            return jsonify({'error': 'Keine Datei hochgeladen.'}), 400

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
                    'error': 'Datei ist keine gültige SQLite-Datenbank.'
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
            return jsonify({'error': f'Import fehlgeschlagen: {e}'}), 500

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
            return jsonify({'error': 'Ein Update-Job läuft bereits.'}), 409
        return jsonify({'ok': True, 'message': 'Security-Updates werden im Hintergrund installiert …'})

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
            return jsonify({'error': 'Topic fehlt'}), 400
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
        # THG reminder: between Jan 1 and Mar 31, warn if previous year has no quota
        thg_reminder = None
        today = date.today()
        if today.month <= 3:
            prev_year = today.year - 1
            existing = ThgQuota.query.filter(
                ThgQuota.year_from <= prev_year,
                ThgQuota.year_to >= prev_year,
            ).first()
            if not existing:
                thg_reminder = prev_year
        return {
            'app_version': Config.APP_VERSION,
            'car_model': AppConfig.get('car_model', Config.CAR_MODEL),
            'current_year': today.year,
            'thg_reminder_year': thg_reminder,
        }

    # ── DASHBOARD ──────────────────────────────────────────────
    @app.route('/')
    def dashboard():
        from services.stats_service import (
            get_summary_stats, get_chart_data, get_ac_dc_stats,
            get_yearly_stats, get_vehicle_history,
        )
        stats = get_summary_stats()
        chart_data = get_chart_data()
        acdc = get_ac_dc_stats()
        yearly = get_yearly_stats()
        vehicle_configured = bool(AppConfig.get('vehicle_api_brand', ''))
        # User's preferred default range for the vehicle-history plots
        # (0 = all). Stored in AppConfig so the choice persists across
        # sessions. Client-side AJAX can override for the current view.
        try:
            default_days = int(AppConfig.get('dash_history_days', '30') or '30')
        except (ValueError, TypeError):
            default_days = 30
        vehicle_history = get_vehicle_history(days=default_days or None) if vehicle_configured else None
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
        return render_template('dashboard.html',
                               stats=stats, chart_data=chart_data,
                               acdc=acdc, yearly=yearly,
                               vehicle_configured=vehicle_configured,
                               vehicle_history=vehicle_history,
                               vehicle_history_days=default_days,
                               vehicle_history_last_gps=last_gps,
                               battery_kwh=_get_battery_kwh())

    # ── EINGABE ────────────────────────────────────────────────
    @app.route('/input', methods=['GET', 'POST'])
    def input_charge():
        if request.method == 'POST':
            try:
                charge = Charge(
                    date=datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
                    charge_hour=_int(request.form.get('charge_hour')),
                    odometer=_int(request.form.get('odometer')),
                    eur_per_kwh=_float(request.form.get('eur_per_kwh')),
                    kwh_loaded=_float(request.form.get('kwh_loaded')),
                    charge_type=request.form.get('charge_type', 'AC').upper(),
                    soc_from=_int(request.form.get('soc_from')),
                    soc_to=_int(request.form.get('soc_to')),
                    loss_kwh=_float(request.form.get('loss_kwh')),
                    co2_g_per_kwh=_int(request.form.get('co2_g_per_kwh')),
                    notes=request.form.get('notes', '').strip() or None,
                    location_lat=_float(request.form.get('location_lat')),
                    location_lon=_float(request.form.get('location_lon')),
                    location_name=request.form.get('location_name', '').strip() or None,
                    operator=request.form.get('operator', '').strip() or None,
                )
                charge.calculate_fields(_get_battery_kwh())

                # If no CO2 provided, set automatically
                if charge.co2_g_per_kwh is None:
                    if charge.charge_type == 'PV':
                        charge.co2_g_per_kwh = _get_pv_co2()
                        charge.calculate_fields(_get_battery_kwh())
                        flash(t('flash.pv_co2_set', value=charge.co2_g_per_kwh), 'info')
                    else:
                        api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
                        if api_key:
                            from services.entsoe_service import get_co2_intensity
                            co2 = get_co2_intensity(api_key, datetime.combine(charge.date, datetime.min.time()), hour=charge.charge_hour)
                            if co2:
                                charge.co2_g_per_kwh = co2
                                charge.calculate_fields(_get_battery_kwh())
                                hour_label = f" ({charge.charge_hour}:00 Uhr)" if charge.charge_hour is not None else ""
                                flash(t('flash.co2_fetched', value=co2, hour=hour_label), 'info')

                db.session.add(charge)
                db.session.commit()
                cost_str = f'€{charge.total_cost:.2f}' if charge.total_cost is not None else '€—'
                flash(t('flash.charge_saved', date=charge.date.strftime("%d.%m.%Y"), kwh=charge.kwh_loaded or 0, cost=cost_str), 'success')
                return redirect(url_for('input_charge'))

            except Exception as e:
                logger.error(f"Error saving charge: {e}")
                flash(t('flash.save_error', error=e), 'danger')

        # Pre-fill date with today
        last_charge = Charge.query.order_by(Charge.date.desc()).first()
        vehicle_configured = bool(AppConfig.get('vehicle_api_brand', ''))
        return render_template('input.html',
                               today=date.today().isoformat(),
                               last_charge=last_charge,
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
                               operators=_get_operator_list(),
                               operator_prices=_get_operator_prices())

    # ── HISTORY ────────────────────────────────────────────────
    @app.route('/history')
    def history():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        charge_type = request.args.get('type', '')
        year = request.args.get('year', '', type=str)

        query = Charge.query
        if charge_type in ('AC', 'DC', 'PV'):
            query = query.filter_by(charge_type=charge_type)
        if year and year.isdigit():
            from sqlalchemy import extract
            query = query.filter(extract('year', Charge.date) == int(year))

        charges = query.order_by(Charge.date.desc()).paginate(
            page=page, per_page=per_page, error_out=False)

        years = db.session.query(
            db.func.distinct(db.func.strftime('%Y', Charge.date))
        ).order_by(db.func.strftime('%Y', Charge.date).desc()).all()
        years = [y[0] for y in years if y[0]]

        return render_template('history.html', charges=charges,
                               charge_type=charge_type, year=year, years=years)

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
                charge.calculate_fields(_get_battery_kwh())
                db.session.commit()
                flash(t('flash.entry_updated'), 'success')
                return redirect(url_for('history'))
            except Exception as e:
                flash(t('flash.error', error=e), 'danger')

        return render_template('edit.html', charge=charge,
                               operators=_get_operator_list(),
                               operator_prices=_get_operator_prices(),
                               home_lat=AppConfig.get('home_lat', ''),
                               home_lon=AppConfig.get('home_lon', ''),
                               home_label=AppConfig.get('home_label', ''),
                               work_lat=AppConfig.get('work_lat', ''),
                               work_lon=AppConfig.get('work_lon', ''),
                               work_label=AppConfig.get('work_label', ''))

    @app.route('/delete/<int:charge_id>', methods=['POST'])
    def delete_charge(charge_id):
        charge = Charge.query.get_or_404(charge_id)
        db.session.delete(charge)
        db.session.commit()
        flash(t('flash.entry_deleted'), 'warning')
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
                AppConfig.set('car_model', request.form.get('car_model', '').strip())
                AppConfig.set('battery_kwh', request.form.get('battery_kwh', ''))
                AppConfig.set('max_ac_kw', request.form.get('max_ac_kw', ''))
                AppConfig.set('battery_co2_per_kwh', request.form.get('battery_co2_per_kwh', ''))
                AppConfig.set('fossil_co2_per_km', request.form.get('fossil_co2_per_km', ''))
                AppConfig.set('recuperation_kwh_per_km', request.form.get('recuperation_kwh_per_km', ''))
                flash(t('flash.vehicle_saved'), 'success')

            elif action == 'save_operators':
                import json as _json
                # The settings form submits parallel arrays: one row per
                # operator, each with a name (may be blank for empty row),
                # a price (may be blank), and an is_builtin flag so we
                # know not to re-add built-ins to the custom list.
                names    = request.form.getlist('op_name')
                prices   = request.form.getlist('op_price')
                builtins = request.form.getlist('op_builtin')  # '1'/'0' per row
                # Pad the builtin flag in case the form was tampered with —
                # safer than crashing on IndexError.
                while len(builtins) < len(names):
                    builtins.append('0')

                custom_list = []
                price_map = {}
                for i, raw_name in enumerate(names):
                    name = (raw_name or '').strip()
                    if not name:
                        continue
                    if builtins[i] != '1' and name not in custom_list:
                        # Skip names that collide with built-ins to avoid
                        # duplicate dropdown entries.
                        if name not in DEFAULT_OPERATORS:
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

                AppConfig.set('custom_operators', _json.dumps(custom_list))
                AppConfig.set('operator_prices', _json.dumps(price_map))
                flash(t('flash.operators_saved'), 'success')

            elif action == 'save_pv':
                AppConfig.set('pv_kwp', request.form.get('pv_kwp', ''))
                AppConfig.set('pv_yield_per_kwp', request.form.get('pv_yield_per_kwp', ''))
                AppConfig.set('pv_lifetime', request.form.get('pv_lifetime', ''))
                AppConfig.set('pv_production_co2', request.form.get('pv_production_co2', ''))
                AppConfig.set('pv_price_eur_per_kwh', request.form.get('pv_price_eur_per_kwh', ''))
                flash(t('flash.pv_saved'), 'success')

            elif action == 'add_thg':
                try:
                    thg = ThgQuota(
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

            elif action == 'save_vehicle_api':
                AppConfig.set('vehicle_api_brand', request.form.get('vehicle_api_brand', ''))
                AppConfig.set('vehicle_api_username', request.form.get('vehicle_api_username', ''))
                AppConfig.set('vehicle_api_password', request.form.get('vehicle_api_password', ''))
                AppConfig.set('vehicle_api_pin', request.form.get('vehicle_api_pin', ''))
                AppConfig.set('vehicle_api_region', request.form.get('vehicle_api_region', 'EU'))
                AppConfig.set('vehicle_api_vin', request.form.get('vehicle_api_vin', ''))
                # Also save sync settings (same form)
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
                flash(t('flash.api_creds_saved'), 'success')

            elif action == 'test_vehicle_api':
                brand = AppConfig.get('vehicle_api_brand', '')
                if brand:
                    try:
                        from services.vehicle import get_connector
                        creds = _get_vehicle_credentials()
                        connector = get_connector(brand, creds)
                        result = connector.test_connection()
                        if result:
                            status = connector.get_status()
                            parts = []
                            if status.soc_percent is not None:
                                parts.append(f'SoC: {status.soc_percent}%')
                            if status.odometer_km is not None:
                                parts.append(f'Tacho: {status.odometer_km:,} km')
                            if status.estimated_range_km is not None:
                                parts.append(f'Range: {status.estimated_range_km} km')
                            info = ', '.join(parts) if parts else ''
                            flash(t('flash.api_connected', info=info), 'success')
                        else:
                            flash(t('flash.api_connect_failed'), 'danger')
                    except Exception as e:
                        flash(t('flash.error', error=e), 'danger')
                else:
                    flash(t('flash.api_no_brand'), 'warning')

            elif action == 'delete_vehicle_api':
                for key in ('vehicle_api_brand', 'vehicle_api_username', 'vehicle_api_password',
                            'vehicle_api_pin', 'vehicle_api_region', 'vehicle_api_vin',
                            'vehicle_sync_enabled', 'vehicle_sync_interval_hours'):
                    entry = AppConfig.query.get(key)
                    if entry:
                        db.session.delete(entry)
                db.session.commit()
                from services.vehicle.sync_service import stop_sync
                stop_sync()
                flash(t('flash.api_creds_deleted'), 'warning')

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

            elif action in ('sync_vehicle_now', 'sync_vehicle_force'):
                force = action == 'sync_vehicle_force'
                brand = AppConfig.get('vehicle_api_brand', '')
                if brand:
                    try:
                        from services.vehicle import get_connector
                        from services.vehicle.sync_service import log_sync_result
                        import json as _json
                        creds = _get_vehicle_credentials()
                        connector = get_connector(brand, creds)
                        status = connector.get_status(force=force)
                        _save_vehicle_sync(status, _get_battery_kwh(),
                                           raw_json=_json.dumps(status.raw_data, default=str))
                        log_sync_result(status,
                                        mode_label='force' if force else 'cached',
                                        source='settings')
                        parts = []
                        if status.soc_percent is not None:
                            parts.append(f'SoC: {status.soc_percent}%')
                        if status.odometer_km is not None:
                            parts.append(f'Tacho: {status.odometer_km:,} km')
                        mode = 'Live' if force else 'Cached'
                        flash(t('flash.sync_success', mode=mode, parts=', '.join(parts)), 'success')
                    except Exception as e:
                        flash(t('flash.sync_error', error=e), 'danger')
                else:
                    flash(t('flash.no_brand_configured'), 'warning')

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

        return render_template('settings.html',
                               entsoe_key=AppConfig.get('entsoe_api_key', ''),
                               car_model_val=AppConfig.get('car_model', Config.CAR_MODEL),
                               vehicle_brands=vehicle_brands,
                               installed_brand_keys=installed_brand_keys,
                               vehicle_api_brand=AppConfig.get('vehicle_api_brand', ''),
                               vehicle_api_username=AppConfig.get('vehicle_api_username', ''),
                               vehicle_api_password=AppConfig.get('vehicle_api_password', ''),
                               vehicle_api_pin=AppConfig.get('vehicle_api_pin', ''),
                               vehicle_api_region=AppConfig.get('vehicle_api_region', 'EU'),
                               vehicle_api_vin=AppConfig.get('vehicle_api_vin', ''),
                               vehicle_sync_enabled=AppConfig.get('vehicle_sync_enabled', 'false'),
                               vehicle_sync_interval=AppConfig.get('vehicle_sync_interval_hours', '4'),
                               vehicle_sync_mode=AppConfig.get('vehicle_sync_mode', 'cached'),
                               smart_active_start_hour=AppConfig.get('smart_active_start_hour', '6'),
                               smart_active_end_hour=AppConfig.get('smart_active_end_hour', '22'),
                               smart_active_interval_min=AppConfig.get('smart_active_interval_min', '10'),
                               last_vehicle_sync=last_sync,
                               battery_kwh=AppConfig.get('battery_kwh', str(Config.BATTERY_CAPACITY_KWH)),
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
                               thg_quotas=ThgQuota.query.order_by(ThgQuota.year_from).all(),
                               total_charges=Charge.query.count(),
                               co2_missing=Charge.query.filter(Charge.co2_g_per_kwh.is_(None), Charge.charge_type != 'PV').count(),
                               auth_enabled=(AppConfig.get('auth_enabled', 'false') == 'true'),
                               auth_username=AppConfig.get('auth_username', ''),
                               hide_ssl_card=hide_ssl_card,
                               custom_operators_text=_get_custom_operators_text(),
                               operators_builtin=DEFAULT_OPERATORS,
                               operators_custom=_get_custom_operators(),
                               operator_prices=_get_operator_prices(),
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
        # info box above the JSON dump).
        normalized = {
            'timestamp': sync.timestamp.isoformat(),
            'soc_percent': sync.soc_percent,
            'odometer_km': sync.odometer_km,
            'is_charging': sync.is_charging,
            'charge_power_kw': sync.charge_power_kw,
            'estimated_range_km': sync.estimated_range_km,
            'battery_12v_percent': sync.battery_12v_percent,
            'battery_soh_percent': sync.battery_soh_percent,
            'total_regenerated_kwh': sync.total_regenerated_kwh,
            'regen_cumulative_kwh': sync.regen_cumulative_kwh,
            'consumption_30d_kwh_per_100km': sync.consumption_30d_kwh_per_100km,
            'location_lat': sync.location_lat,
            'location_lon': sync.location_lon,
        }
        soh_note = None
        if (sync.battery_soh_percent is not None and sync.battery_soh_percent > 100
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
        data = build_report(s, e, lang=lang)
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
            return jsonify({'error': 'Nur für Kia/Hyundai verfügbar'}), 400
        AppConfig.set('vehicle_api_brand', brand)
        from services.vehicle.token_fetch import start_fetch
        if start_fetch(brand):
            return jsonify({'success': True})
        return jsonify({'error': 'Läuft bereits'}), 409

    @app.route('/api/vehicle/token/status')
    def api_vehicle_token_status():
        """Poll token fetch status."""
        from services.vehicle.token_fetch import get_state
        state = get_state()
        if state.get('token'):
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
        browser, we extract the code and exchange for a refresh_token."""
        data = request.get_json() or {}
        brand = data.get('brand') or AppConfig.get('vehicle_api_brand', '')
        url = data.get('url') or ''
        if brand not in ('kia', 'hyundai'):
            return jsonify({'error': 'Nur für Kia/Hyundai verfügbar'}), 400
        from services.vehicle.token_fetch import exchange_manual_url
        ok, msg, token = exchange_manual_url(brand, url)
        if ok and token:
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
            return jsonify({'error': 'Nur für Kia/Hyundai verfügbar'}), 400
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
            if result.returncode == 0:
                # Force reload of connector modules so they re-check for installed packages
                import importlib
                CONNECTOR_MAP = {
                    'hyundai-kia': 'connector_hyundai_kia',
                    'vw': 'connector_vag', 'skoda': 'connector_vag', 'seatcupra': 'connector_vag',
                    'tesla': 'connector_tesla', 'renault': 'connector_renault',
                    'polestar': 'connector_polestar', 'mg': 'connector_mg',
                    'smart': 'connector_smart', 'porsche': 'connector_porsche',
                }
                connector_mod = CONNECTOR_MAP.get(pkg_key, '')
                modules_to_reload = [
                    f'services.vehicle.{connector_mod}',
                    'services.vehicle.registry',
                    'services.vehicle',
                ]
                for mod_name in modules_to_reload:
                    if mod_name in sys.modules:
                        try:
                            importlib.reload(sys.modules[mod_name])
                        except Exception:
                            pass
                    else:
                        try:
                            importlib.import_module(mod_name)
                        except Exception:
                            pass
                return jsonify({'success': True, 'installed': packages})
            else:
                error = result.stderr.strip().split('\n')[-1] if result.stderr else 'pip install fehlgeschlagen'
                return jsonify({'success': False, 'error': error}), 500
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

    @app.route('/api/vehicle/status')
    def api_vehicle_status():
        """Fetch current vehicle status with full details."""
        brand = AppConfig.get('vehicle_api_brand', '')
        if not brand:
            return jsonify({'error': 'not_configured'}), 400

        force = request.args.get('force', '0') == '1'

        # Validate token format for Kia/Hyundai
        if brand in ('kia', 'hyundai'):
            import re as _re
            token = AppConfig.get('vehicle_api_password', '')
            if not _re.match(r'^[A-Z0-9]{48}$', token):
                return jsonify({'error': 'Ungültiger Token. Bitte unter Einstellungen → Token holen.'}), 400

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
            import json as _json
            creds = _get_vehicle_credentials()
            connector = get_connector(brand, creds)
            s = connector.get_status(force=force)
            sync = _save_vehicle_sync(s, _get_battery_kwh(),
                                      raw_json=_json.dumps(s.raw_data, default=str))
            log_sync_result(s,
                            mode_label='force' if force else 'cached',
                            source='dashboard')
            return jsonify({
                'soc': s.soc_percent,
                'odometer': s.odometer_km,
                'is_charging': s.is_charging,
                'is_plugged_in': s.is_plugged_in,
                'is_locked': s.is_locked,
                'range_km': s.estimated_range_km,
                'battery_12v': s.battery_12v_percent,
                'battery_soh': s.battery_soh_percent,
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

    @app.route('/api/update/install', methods=['POST'])
    def api_update_install():
        """Stage an update and trigger a graceful shutdown so the helper
        can swap files and restart the app."""
        from updater import check_for_update, apply_update
        new_version, zip_url = check_for_update()
        if not new_version or not zip_url:
            return jsonify({'error': 'no_update_available'}), 400

        ok = apply_update(zip_url, new_version)
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

        # Auto-fresh runs in a BACKGROUND thread so the page renders
        # immediately with whatever GPS data we already have. The user can
        # see freshness in the header and hit "Jetzt synchronisieren" if
        # they want to wait for fresh data right now.
        #
        # Conditions: brand configured, brand supports GPS, auto-sync on,
        # last GPS sync >2h old, daily API counter <180/200, and not already
        # currently in a background fresh from a recent visit.
        try:
            brand = AppConfig.get('vehicle_api_brand', '')
            auto_sync_enabled = AppConfig.get('vehicle_sync_enabled', 'false') == 'true'
            if brand and auto_sync_enabled and is_brand_supports_location(brand):
                from datetime import datetime as _dt, timedelta as _td
                last_with_gps = (VehicleSync.query
                                 .filter(VehicleSync.location_lat.isnot(None))
                                 .order_by(VehicleSync.timestamp.desc())
                                 .first())
                stale = True
                if last_with_gps:
                    stale = (_dt.now() - last_with_gps.timestamp) > _td(hours=2)
                today_str = date.today().isoformat()
                counter_date = AppConfig.get('vehicle_api_counter_date', '')
                if counter_date != today_str:
                    AppConfig.set('vehicle_api_counter_date', today_str)
                    AppConfig.set('vehicle_api_counter', '0')
                api_count = int(AppConfig.get('vehicle_api_counter', '0'))

                # De-bounce: don't fire if a previous /trips visit kicked one
                # off in the last 5 minutes (the request might still be in
                # flight; the Kia API takes 5-10 s).
                last_bg_str = AppConfig.get('trips_last_bg_fresh_at', '')
                bg_in_progress = False
                if last_bg_str:
                    try:
                        last_bg = _dt.fromisoformat(last_bg_str)
                        bg_in_progress = (_dt.now() - last_bg) < _td(minutes=5)
                    except ValueError:
                        pass

                if stale and api_count < 180 and not bg_in_progress:
                    AppConfig.set('trips_last_bg_fresh_at', _dt.now().isoformat())
                    AppConfig.set('vehicle_api_counter', str(api_count + 1))

                    def _bg_fresh(captured_app, captured_brand):
                        with captured_app.app_context():
                            try:
                                from services.vehicle import get_connector
                                from services.vehicle.sync_service import log_sync_result
                                import json as _json
                                creds = _get_vehicle_credentials()
                                connector = get_connector(captured_brand, creds)
                                status = connector.get_status(force=True)
                                _save_vehicle_sync(status, _get_battery_kwh(),
                                                   raw_json=_json.dumps(status.raw_data, default=str))
                                log_sync_result(status, mode_label='force',
                                                source='trips-auto')
                            except Exception as e:
                                logger.warning(f"trips_page background auto-fresh failed: {e}")

                    import threading as _th
                    _th.Thread(target=_bg_fresh, args=(app, brand), daemon=True).start()
                    logger.info("trips_page: background auto-fresh started")
        except Exception as e:
            logger.warning(f"trips_page auto-fresh dispatch failed: {e}")

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

        trips = get_trips(limit=200)
        events = get_parking_events(limit=200)
        summary = get_trip_summary()
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
        events = list(reversed(get_parking_events()))  # chronological
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
            sync = _save_vehicle_sync(status, _get_battery_kwh(),
                                      raw_json=_json.dumps(status.raw_data, default=str))
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

        if request.method == 'POST':
            try:
                date_str = request.form.get('date')
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
                )
                flash(t('flash.maintenance_saved'), 'success')
            except Exception as e:
                flash(t('flash.error', error=e), 'danger')
            return redirect(url_for('maintenance_page'))

        # Determine current odometer (latest charge or vehicle sync)
        current_odo = None
        last_charge = Charge.query.filter(Charge.odometer.isnot(None)).order_by(Charge.date.desc()).first()
        if last_charge:
            current_odo = last_charge.odometer
        last_sync = VehicleSync.query.filter(VehicleSync.odometer_km.isnot(None)).order_by(VehicleSync.timestamp.desc()).first()
        if last_sync and (current_odo is None or (last_sync.odometer_km or 0) > current_odo):
            current_odo = last_sync.odometer_km

        return render_template('maintenance.html',
                               entries=list_entries(),
                               due_items=get_due_items(current_odo),
                               summary=get_summary(),
                               current_odo=current_odo,
                               today=date.today().isoformat(),
                               default_intervals=DEFAULT_INTERVALS)

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
