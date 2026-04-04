"""EV Charge Tracker - Main Flask Application."""
import os
import logging
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from models.database import db, Charge, AppConfig, ThgQuota
from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


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
        return render_template('dashboard.html',
                               stats=stats, chart_data=chart_data,
                               acdc=acdc, yearly=yearly)

    # ── EINGABE ────────────────────────────────────────────────
    @app.route('/input', methods=['GET', 'POST'])
    def input_charge():
        if request.method == 'POST':
            try:
                charge = Charge(
                    date=datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
                    eur_per_kwh=_float(request.form.get('eur_per_kwh')),
                    kwh_loaded=_float(request.form.get('kwh_loaded')),
                    charge_type=request.form.get('charge_type', 'AC').upper(),
                    soc_from=_int(request.form.get('soc_from')),
                    soc_to=_int(request.form.get('soc_to')),
                    loss_kwh=_float(request.form.get('loss_kwh')),
                    co2_g_per_kwh=_int(request.form.get('co2_g_per_kwh')),
                    notes=request.form.get('notes', '').strip() or None,
                )
                charge.calculate_fields()

                # If no CO2 provided, try ENTSO-E
                if charge.co2_g_per_kwh is None:
                    api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
                    if api_key:
                        from services.entsoe_service import get_co2_intensity
                        co2 = get_co2_intensity(api_key, datetime.combine(charge.date, datetime.min.time()))
                        if co2:
                            charge.co2_g_per_kwh = co2
                            charge.calculate_fields()
                            flash(f'CO₂-Intensität automatisch von ENTSO-E geholt: {co2} g/kWh', 'info')

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
                               last_charge=last_charge)

    # ── HISTORY ────────────────────────────────────────────────
    @app.route('/history')
    def history():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        charge_type = request.args.get('type', '')
        year = request.args.get('year', '', type=str)

        query = Charge.query
        if charge_type in ('AC', 'DC'):
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
                charge.eur_per_kwh = _float(request.form.get('eur_per_kwh'))
                charge.kwh_loaded = _float(request.form.get('kwh_loaded'))
                charge.charge_type = request.form.get('charge_type', 'AC').upper()
                charge.soc_from = _int(request.form.get('soc_from'))
                charge.soc_to = _int(request.form.get('soc_to'))
                charge.loss_kwh = _float(request.form.get('loss_kwh'))
                charge.co2_g_per_kwh = _int(request.form.get('co2_g_per_kwh'))
                charge.notes = request.form.get('notes', '').strip() or None
                charge.calculate_fields()
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
                flash('Fahrzeugdaten gespeichert!', 'success')

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

            return redirect(url_for('settings'))

        return render_template('settings.html',
                               entsoe_key=AppConfig.get('entsoe_api_key', ''),
                               car_model_val=AppConfig.get('car_model', Config.CAR_MODEL),
                               battery_kwh=AppConfig.get('battery_kwh', str(Config.BATTERY_CAPACITY_KWH)),
                               max_ac_kw=AppConfig.get('max_ac_kw', ''),
                               thg_quotas=ThgQuota.query.order_by(ThgQuota.year_from).all(),
                               total_charges=Charge.query.count(),
                               app_version=Config.APP_VERSION)

    # ── API ENDPOINTS ──────────────────────────────────────────
    @app.route('/api/co2/<date_str>')
    def api_get_co2(date_str):
        """Fetch CO2 intensity for a date via ENTSO-E."""
        try:
            target = datetime.strptime(date_str, '%Y-%m-%d')
            api_key = AppConfig.get('entsoe_api_key', Config.ENTSOE_API_KEY)
            if not api_key:
                return jsonify({'error': 'No ENTSO-E API key configured'}), 400

            from services.entsoe_service import get_co2_intensity
            co2 = get_co2_intensity(api_key, target)
            if co2:
                return jsonify({'co2_g_per_kwh': co2, 'date': date_str})
            return jsonify({'error': 'No data available'}), 404
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
