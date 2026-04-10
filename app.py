"""EV Charge Tracker - Main Flask Application."""
import io
import os
import sys
import logging
from datetime import datetime, date
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
from config import Config
from services.i18n import t

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory log ring buffer for the /logs page
from services.log_service import install as _install_log_ring, set_request_logging as _set_req_log
_install_log_ring(level=logging.INFO)
# Werkzeug access logging defaults to OFF (user can toggle on /logs page)
_set_req_log(False)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    db.init_app(app)

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

        # Migrate: add last_seen_at to parking_events
        try:
            parking_columns = [c['name'] for c in inspector.get_columns('parking_events')]
            if 'last_seen_at' not in parking_columns:
                db.session.execute(text('ALTER TABLE parking_events ADD COLUMN last_seen_at DATETIME'))
        except Exception:
            pass  # table might not exist yet on a fresh install — create_all() will handle it

        db.session.commit()

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


def _build_vehicle_sync(status, battery_kwh, raw_json=''):
    """Build a VehicleSync row from a connector VehicleStatus."""
    regen_kwh = None
    if status.total_power_regenerated_kwh is not None:
        try:
            regen_kwh = round(float(status.total_power_regenerated_kwh) / 10.0, 1)
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
        vehicle_history = get_vehicle_history() if vehicle_configured else None
        return render_template('dashboard.html',
                               stats=stats, chart_data=chart_data,
                               acdc=acdc, yearly=yearly,
                               vehicle_configured=vehicle_configured,
                               vehicle_history=vehicle_history,
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
                               work_label=AppConfig.get('work_label', ''))

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
                charge.calculate_fields(_get_battery_kwh())
                db.session.commit()
                flash(t('flash.entry_updated'), 'success')
                return redirect(url_for('history'))
            except Exception as e:
                flash(t('flash.error', error=e), 'danger')

        return render_template('edit.html', charge=charge)

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
                return redirect(url_for('settings'))

            elif action == 'save_car':
                AppConfig.set('car_model', request.form.get('car_model', '').strip())
                AppConfig.set('battery_kwh', request.form.get('battery_kwh', ''))
                AppConfig.set('max_ac_kw', request.form.get('max_ac_kw', ''))
                AppConfig.set('battery_co2_per_kwh', request.form.get('battery_co2_per_kwh', ''))
                AppConfig.set('fossil_co2_per_km', request.form.get('fossil_co2_per_km', ''))
                AppConfig.set('recuperation_kwh_per_km', request.form.get('recuperation_kwh_per_km', ''))
                flash(t('flash.vehicle_saved'), 'success')

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
                        import io
                        from import_gsheet import import_csv_data
                        replace = 'replace_data' in request.form
                        stream = io.StringIO(file.stream.read().decode('utf-8'))
                        result = import_csv_data(stream, replace=replace)
                        msg = t('flash.import_success', count=result['imported'])
                        if result['errors']:
                            msg += ' ' + t('flash.import_errors', count=len(result['errors']))
                        flash(msg, 'success')
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
                                           raw_json=_json.dumps(status.raw_data))
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

            return redirect(url_for('settings'))

        # Vehicle API brands (only those with installed dependencies)
        try:
            from services.vehicle import get_available_brands
            vehicle_brands = get_available_brands()
        except Exception:
            vehicle_brands = []
        installed_brand_keys = [b['key'] for b in vehicle_brands]

        # Last vehicle sync
        last_sync = VehicleSync.query.order_by(VehicleSync.timestamp.desc()).first()

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
                               app_version=Config.APP_VERSION)

    # ── REPORT ─────────────────────────────────────────────────
    @app.route('/report')
    def report():
        from services.report_service import generate_report
        from flask import send_file
        pdf_bytes = generate_report()
        if not pdf_bytes:
            flash(t('flash.no_report_data'), 'warning')
            return redirect(url_for('dashboard'))
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
                                      raw_json=_json.dumps(s.raw_data))
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
                'total_regenerated_kwh': s.total_power_regenerated_kwh,
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
                                                   raw_json=_json.dumps(status.raw_data))
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
                                      raw_json=_json.dumps(status.raw_data))
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

    @app.route('/api/locations/favorites', methods=['GET', 'POST', 'DELETE'])
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

    @app.route('/api/export/csv')
    def export_csv():
        """Export all charges as CSV."""
        import csv
        import io
        charges = Charge.query.order_by(Charge.date).all()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Datum', 'EUR/kWh', 'kWh', 'Kosten', 'Typ', 'Von%', 'Bis%', 'Geladen%',
                         'Verlust_kWh', 'Verlust%', 'CO2_g/kWh', 'CO2_kg', 'Notizen'])
        for c in charges:
            writer.writerow([
                c.date.strftime('%d.%m.%Y') if c.date else '',
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
