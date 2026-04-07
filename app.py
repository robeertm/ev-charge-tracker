"""EV Charge Tracker - Main Flask Application."""
import os
import logging
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from models.database import db, Charge, AppConfig, ThgQuota, VehicleSync
from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    register_routes(app)

    # Auto-start vehicle sync if configured
    try:
        from services.vehicle.sync_service import start_sync
        start_sync(app)
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


def register_routes(app):

    @app.context_processor
    def inject_globals():
        return {
            'app_version': Config.APP_VERSION,
            'car_model': AppConfig.get('car_model', Config.CAR_MODEL),
        }

    # ── DASHBOARD ──────────────────────────────────────────────
    @app.route('/')
    def dashboard():
        from services.stats_service import get_summary_stats, get_chart_data, get_ac_dc_stats, get_yearly_stats
        stats = get_summary_stats()
        chart_data = get_chart_data()
        acdc = get_ac_dc_stats()
        yearly = get_yearly_stats()
        vehicle_configured = bool(AppConfig.get('vehicle_api_brand', ''))
        return render_template('dashboard.html',
                               stats=stats, chart_data=chart_data,
                               acdc=acdc, yearly=yearly,
                               vehicle_configured=vehicle_configured)

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
                )
                charge.calculate_fields(_get_battery_kwh())

                # If no CO2 provided, set automatically
                if charge.co2_g_per_kwh is None:
                    if charge.charge_type == 'PV':
                        charge.co2_g_per_kwh = _get_pv_co2()
                        charge.calculate_fields(_get_battery_kwh())
                        flash(f'PV CO₂-Wert: {charge.co2_g_per_kwh} g/kWh', 'info')
                    else:
                        api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
                        if api_key:
                            from services.entsoe_service import get_co2_intensity
                            co2 = get_co2_intensity(api_key, datetime.combine(charge.date, datetime.min.time()), hour=charge.charge_hour)
                            if co2:
                                charge.co2_g_per_kwh = co2
                                charge.calculate_fields(_get_battery_kwh())
                                hour_label = f" ({charge.charge_hour}:00 Uhr)" if charge.charge_hour is not None else ""
                                flash(f'CO₂-Intensität automatisch von ENTSO-E geholt: {co2} g/kWh{hour_label}', 'info')

                db.session.add(charge)
                db.session.commit()
                flash(f'Ladevorgang vom {charge.date.strftime("%d.%m.%Y")} gespeichert! ({charge.kwh_loaded} kWh, €{charge.total_cost:.2f})', 'success')
                return redirect(url_for('input_charge'))

            except Exception as e:
                logger.error(f"Error saving charge: {e}")
                flash(f'Fehler beim Speichern: {e}', 'danger')

        # Pre-fill date with today
        last_charge = Charge.query.order_by(Charge.date.desc()).first()
        return render_template('input.html',
                               today=date.today().isoformat(),
                               last_charge=last_charge,
                               pv_co2=_get_pv_co2(),
                               pv_price=AppConfig.get('pv_price_eur_per_kwh', '0.00'))

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
                flash('Eintrag aktualisiert!', 'success')
                return redirect(url_for('history'))
            except Exception as e:
                flash(f'Fehler: {e}', 'danger')

        return render_template('edit.html', charge=charge)

    @app.route('/delete/<int:charge_id>', methods=['POST'])
    def delete_charge(charge_id):
        charge = Charge.query.get_or_404(charge_id)
        db.session.delete(charge)
        db.session.commit()
        flash('Eintrag gelöscht.', 'warning')
        return redirect(url_for('history'))

    # ── SETTINGS ───────────────────────────────────────────────
    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'save_entsoe':
                key = request.form.get('entsoe_key', '').strip()
                AppConfig.set('entsoe_api_key', key)
                flash('ENTSO-E API Key gespeichert!', 'success')

            elif action == 'test_entsoe':
                key = AppConfig.get('entsoe_api_key', '')
                if key:
                    from services.entsoe_service import test_api_key
                    if test_api_key(key):
                        flash('ENTSO-E API Key ist gültig! ✓', 'success')
                    else:
                        flash('ENTSO-E API Key ungültig oder API nicht erreichbar.', 'danger')
                else:
                    flash('Kein API Key hinterlegt.', 'warning')

            elif action == 'save_car':
                AppConfig.set('car_model', request.form.get('car_model', '').strip())
                AppConfig.set('battery_kwh', request.form.get('battery_kwh', ''))
                AppConfig.set('max_ac_kw', request.form.get('max_ac_kw', ''))
                AppConfig.set('battery_co2_per_kwh', request.form.get('battery_co2_per_kwh', ''))
                AppConfig.set('fossil_co2_per_km', request.form.get('fossil_co2_per_km', ''))
                AppConfig.set('recuperation_kwh_per_km', request.form.get('recuperation_kwh_per_km', ''))
                flash('Fahrzeugdaten gespeichert!', 'success')

            elif action == 'save_pv':
                AppConfig.set('pv_kwp', request.form.get('pv_kwp', ''))
                AppConfig.set('pv_yield_per_kwp', request.form.get('pv_yield_per_kwp', ''))
                AppConfig.set('pv_lifetime', request.form.get('pv_lifetime', ''))
                AppConfig.set('pv_production_co2', request.form.get('pv_production_co2', ''))
                AppConfig.set('pv_price_eur_per_kwh', request.form.get('pv_price_eur_per_kwh', ''))
                flash('PV-Anlage gespeichert!', 'success')

            elif action == 'add_thg':
                try:
                    thg = ThgQuota(
                        year_from=int(request.form['thg_year_from']),
                        year_to=int(request.form['thg_year_to']),
                        amount_eur=float(request.form['thg_amount'].replace(',', '.')),
                    )
                    db.session.add(thg)
                    db.session.commit()
                    flash(f'THG-Quote {thg.year_from}/{thg.year_to} hinzugefügt!', 'success')
                except Exception as e:
                    flash(f'Fehler: {e}', 'danger')

            elif action == 'delete_thg':
                thg = ThgQuota.query.get(request.form.get('thg_id'))
                if thg:
                    db.session.delete(thg)
                    db.session.commit()
                    flash('THG-Quote gelöscht.', 'warning')

            elif action == 'import_csv':
                file = request.files.get('csv_file')
                if file and file.filename:
                    try:
                        import io
                        from import_gsheet import import_csv_data
                        replace = 'replace_data' in request.form
                        stream = io.StringIO(file.stream.read().decode('utf-8'))
                        result = import_csv_data(stream, replace=replace)
                        msg = f"{result['imported']} Ladevorgänge importiert, {result['skipped']} Zeilen übersprungen."
                        if result['errors']:
                            msg += f" {len(result['errors'])} Fehler."
                        flash(msg, 'success')
                        # Auto-start CO2 backfill
                        from services.co2_backfill import start_backfill
                        if start_backfill(app):
                            flash('CO₂-Daten werden im Hintergrund von ENTSO-E geladen...', 'info')
                    except Exception as e:
                        flash(f'Import-Fehler: {e}', 'danger')
                else:
                    flash('Keine Datei ausgewählt.', 'warning')

            elif action == 'backfill_co2':
                from services.co2_backfill import start_backfill
                if start_backfill(app):
                    flash('CO₂-Daten werden im Hintergrund geladen...', 'info')
                else:
                    flash('Backfill läuft bereits oder keine fehlenden Werte.', 'warning')

            elif action == 'save_vehicle_api':
                AppConfig.set('vehicle_api_brand', request.form.get('vehicle_api_brand', ''))
                AppConfig.set('vehicle_api_username', request.form.get('vehicle_api_username', ''))
                AppConfig.set('vehicle_api_password', request.form.get('vehicle_api_password', ''))
                AppConfig.set('vehicle_api_pin', request.form.get('vehicle_api_pin', ''))
                AppConfig.set('vehicle_api_region', request.form.get('vehicle_api_region', 'EU'))
                AppConfig.set('vehicle_api_vin', request.form.get('vehicle_api_vin', ''))
                flash('Fahrzeug-API Zugangsdaten gespeichert!', 'success')

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
                                parts.append(f'Reichweite: {status.estimated_range_km} km')
                            info = ', '.join(parts) if parts else 'Verbunden'
                            flash(f'Fahrzeug-API verbunden! {info}', 'success')
                        else:
                            flash('Verbindung fehlgeschlagen. Zugangsdaten prüfen.', 'danger')
                    except Exception as e:
                        flash(f'Fehler: {e}', 'danger')
                else:
                    flash('Keine Fahrzeugmarke ausgewählt.', 'warning')

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
                flash('Fahrzeug-API Zugangsdaten gelöscht.', 'warning')

            elif action == 'save_vehicle_sync':
                enabled = 'true' if 'vehicle_sync_enabled' in request.form else 'false'
                AppConfig.set('vehicle_sync_enabled', enabled)
                AppConfig.set('vehicle_sync_interval_hours', request.form.get('vehicle_sync_interval', '4'))
                if enabled == 'true':
                    from services.vehicle.sync_service import start_sync
                    if start_sync(app):
                        flash('Automatische Synchronisierung gestartet!', 'success')
                    else:
                        flash('Sync-Einstellungen gespeichert.', 'success')
                else:
                    from services.vehicle.sync_service import stop_sync
                    stop_sync()
                    flash('Automatische Synchronisierung deaktiviert.', 'warning')

            elif action == 'sync_vehicle_now':
                brand = AppConfig.get('vehicle_api_brand', '')
                if brand:
                    try:
                        from services.vehicle import get_connector
                        import json as _json
                        creds = _get_vehicle_credentials()
                        connector = get_connector(brand, creds)
                        status = connector.get_status()
                        sync = VehicleSync(
                            soc_percent=status.soc_percent,
                            odometer_km=status.odometer_km,
                            is_charging=status.is_charging,
                            charge_power_kw=status.charge_power_kw,
                            estimated_range_km=status.estimated_range_km,
                            raw_json=_json.dumps(status.raw_data),
                        )
                        db.session.add(sync)
                        db.session.commit()
                        parts = []
                        if status.soc_percent is not None:
                            parts.append(f'SoC: {status.soc_percent}%')
                        if status.odometer_km is not None:
                            parts.append(f'Tacho: {status.odometer_km:,} km')
                        flash(f'Fahrzeugdaten abgerufen! {", ".join(parts)}', 'success')
                    except Exception as e:
                        flash(f'Sync-Fehler: {e}', 'danger')
                else:
                    flash('Keine Fahrzeugmarke konfiguriert.', 'warning')

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
                               thg_quotas=ThgQuota.query.order_by(ThgQuota.year_from).all(),
                               total_charges=Charge.query.count(),
                               co2_missing=Charge.query.filter(Charge.co2_g_per_kwh.is_(None), Charge.charge_type != 'PV').count(),
                               app_version=Config.APP_VERSION)

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
                modules_to_reload = [
                    'services.vehicle.connector_hyundai_kia' if pkg_key == 'hyundai-kia'
                    else 'services.vehicle.connector_vag',
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

        try:
            from services.vehicle import get_connector
            import json as _json
            creds = _get_vehicle_credentials()
            connector = get_connector(brand, creds)
            s = connector.get_status()
            sync = VehicleSync(
                soc_percent=s.soc_percent,
                odometer_km=s.odometer_km,
                is_charging=s.is_charging,
                charge_power_kw=s.charge_power_kw,
                estimated_range_km=s.estimated_range_km,
                raw_json=_json.dumps(s.raw_data),
            )
            db.session.add(sync)
            db.session.commit()
            return jsonify({
                'soc': s.soc_percent,
                'odometer': s.odometer_km,
                'is_charging': s.is_charging,
                'is_plugged_in': s.is_plugged_in,
                'is_locked': s.is_locked,
                'range_km': s.estimated_range_km,
                'battery_12v': s.battery_12v_percent,
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
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/stats')
    def api_stats():
        from services.stats_service import get_summary_stats
        return jsonify(get_summary_stats())

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
    print(f"\n⚡ EV Charge Tracker v{Config.APP_VERSION}")
    print(f"🚗 {Config.CAR_MODEL}")
    print(f"🌐 http://localhost:{Config.APP_PORT}")
    print(f"📱 Vom Smartphone: http://<deine-ip>:{Config.APP_PORT}\n")
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)
