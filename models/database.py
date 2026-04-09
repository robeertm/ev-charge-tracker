from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date

db = SQLAlchemy()


class Charge(db.Model):
    __tablename__ = 'charges'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    charge_hour = db.Column(db.Integer)  # 0-23, hour of charging
    odometer = db.Column(db.Integer)  # km-Stand bei Ladung
    eur_per_kwh = db.Column(db.Float)
    kwh_loaded = db.Column(db.Float)
    total_cost = db.Column(db.Float)
    charge_type = db.Column(db.String(2))  # AC or DC
    soc_from = db.Column(db.Integer)
    soc_to = db.Column(db.Integer)
    soc_charged = db.Column(db.Integer)
    loss_kwh = db.Column(db.Float)
    loss_pct = db.Column(db.Float)
    co2_g_per_kwh = db.Column(db.Integer)
    co2_kg = db.Column(db.Float)
    notes = db.Column(db.Text)
    location_lat = db.Column(db.Float)
    location_lon = db.Column(db.Float)
    location_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.now)

    def calculate_fields(self, battery_kwh=None):
        """Auto-calculate derived fields."""
        if self.eur_per_kwh is not None and self.kwh_loaded is not None:
            self.total_cost = round(self.eur_per_kwh * self.kwh_loaded, 2)
        if self.soc_from is not None and self.soc_to is not None:
            self.soc_charged = self.soc_to - self.soc_from
            # Auto-calculate loss if not manually provided
            if self.loss_kwh is None and battery_kwh and self.kwh_loaded and self.soc_charged > 0:
                kwh_in_battery = self.soc_charged / 100 * battery_kwh
                calculated_loss = round(self.kwh_loaded - kwh_in_battery, 3)
                self.loss_kwh = max(calculated_loss, 0.0)
        if self.loss_kwh is not None and self.kwh_loaded and self.kwh_loaded > 0:
            self.loss_pct = round(self.loss_kwh / self.kwh_loaded * 100, 2)
        if self.co2_g_per_kwh is not None and self.kwh_loaded is not None:
            self.co2_kg = round(self.kwh_loaded * self.co2_g_per_kwh / 1000, 2)

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date.isoformat() if self.date else None,
            'charge_hour': self.charge_hour,
            'odometer': self.odometer,
            'eur_per_kwh': self.eur_per_kwh,
            'kwh_loaded': self.kwh_loaded,
            'total_cost': self.total_cost,
            'charge_type': self.charge_type,
            'soc_from': self.soc_from,
            'soc_to': self.soc_to,
            'soc_charged': self.soc_charged,
            'loss_kwh': self.loss_kwh,
            'loss_pct': self.loss_pct,
            'co2_g_per_kwh': self.co2_g_per_kwh,
            'co2_kg': self.co2_kg,
            'notes': self.notes,
            'location_lat': self.location_lat,
            'location_lon': self.location_lon,
            'location_name': self.location_name,
        }


class AppConfig(db.Model):
    __tablename__ = 'app_config'

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)

    @staticmethod
    def get(key, default=None):
        entry = AppConfig.query.get(key)
        return entry.value if entry else default

    @staticmethod
    def set(key, value):
        entry = AppConfig.query.get(key)
        if entry:
            entry.value = str(value)
        else:
            entry = AppConfig(key=key, value=str(value))
            db.session.add(entry)
        db.session.commit()


class VehicleSync(db.Model):
    __tablename__ = 'vehicle_syncs'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    soc_percent = db.Column(db.Integer)
    odometer_km = db.Column(db.Integer)
    is_charging = db.Column(db.Boolean)
    charge_power_kw = db.Column(db.Float)
    estimated_range_km = db.Column(db.Integer)
    # Extended history columns
    battery_12v_percent = db.Column(db.Integer)
    battery_soh_percent = db.Column(db.Float)
    total_regenerated_kwh = db.Column(db.Float)
    consumption_30d_kwh_per_100km = db.Column(db.Float)
    location_lat = db.Column(db.Float)
    location_lon = db.Column(db.Float)
    raw_json = db.Column(db.Text)

    # Fields used for change detection (any difference triggers a new row)
    TRACKED_FIELDS = (
        'soc_percent', 'odometer_km', 'estimated_range_km',
        'battery_12v_percent', 'battery_soh_percent',
        'total_regenerated_kwh', 'consumption_30d_kwh_per_100km',
        'location_lat', 'location_lon',
    )

    def differs_from(self, other):
        """Return True if any tracked field differs (None counts as different)."""
        if other is None:
            return True
        for f in self.TRACKED_FIELDS:
            a = getattr(self, f)
            b = getattr(other, f)
            if a is None and b is None:
                continue
            if a is None or b is None:
                return True
            # Floats: tolerate tiny noise
            if isinstance(a, float) or isinstance(b, float):
                if abs(float(a) - float(b)) > 1e-4:
                    return True
            elif a != b:
                return True
        return False


class ThgQuota(db.Model):
    __tablename__ = 'thg_quotas'

    id = db.Column(db.Integer, primary_key=True)
    year_from = db.Column(db.Integer, nullable=False)
    year_to = db.Column(db.Integer, nullable=False)
    amount_eur = db.Column(db.Float, nullable=False)


class ParkingEvent(db.Model):
    """A single parking spell. Created when the vehicle stops at a new
    location, closed when it moves >100m away."""
    __tablename__ = 'parking_events'

    id = db.Column(db.Integer, primary_key=True)
    arrived_at = db.Column(db.DateTime, nullable=False, index=True)
    departed_at = db.Column(db.DateTime, index=True)  # NULL = currently parked
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    label = db.Column(db.String(32))   # 'home' | 'work' | 'favorite' | 'other'
    favorite_name = db.Column(db.String(120))  # name of matched favorite, if any
    address = db.Column(db.Text)
    odometer_arrived = db.Column(db.Integer)
    odometer_departed = db.Column(db.Integer)
    soc_arrived = db.Column(db.Integer)
    soc_departed = db.Column(db.Integer)


class MaintenanceEntry(db.Model):
    """Maintenance log: inspections, tires, brakes, etc., with optional reminders."""
    __tablename__ = 'maintenance_log'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    item_type = db.Column(db.String(40), nullable=False)  # 'inspection','tires','brakes','wiper','battery_12v','other'
    title = db.Column(db.String(120))
    odometer_km = db.Column(db.Integer)
    cost_eur = db.Column(db.Float)
    notes = db.Column(db.Text)
    next_due_km = db.Column(db.Integer)
    next_due_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.now)


class GeocodeCache(db.Model):
    """Reverse geocoding cache (Nominatim) — keyed by rounded lat/lon to avoid
    re-querying for nearby coords."""
    __tablename__ = 'geocode_cache'

    id = db.Column(db.Integer, primary_key=True)
    lat_key = db.Column(db.String(20), nullable=False, index=True)
    lon_key = db.Column(db.String(20), nullable=False, index=True)
    address = db.Column(db.Text)
    fetched_at = db.Column(db.DateTime, default=datetime.now)


class WeatherCache(db.Model):
    """Daily mean temperature cache from Open-Meteo, keyed by date+coords."""
    __tablename__ = 'weather_cache'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    lat_key = db.Column(db.String(20), nullable=False)
    lon_key = db.Column(db.String(20), nullable=False)
    temp_mean_c = db.Column(db.Float)
    fetched_at = db.Column(db.DateTime, default=datetime.now)
