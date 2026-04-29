from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date

db = SQLAlchemy()


class Vehicle(db.Model):
    """Fleet vehicle. Multi-vehicle support landed in v2.29.0 — every
    Charge / VehicleSync / ParkingEvent / VehicleTrip / MaintenanceEntry
    row carries a vehicle_id FK that anchors the data to one specific
    car for life. Aggregate "fleet" stats sum across all vehicles;
    per-vehicle stats filter by vehicle_id.

    Lifecycle: ``is_archived = True`` retires a vehicle (sold / replaced)
    without losing its history. Archived vehicles still contribute to
    fleet-wide aggregates so the sold Kia's lifetime kWh / km / cost
    stays visible.
    """
    __tablename__ = 'vehicles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)        # display name (e.g. "Robert's Niro")
    brand = db.Column(db.String(32))                       # "Kia", "Hyundai", "Skoda", ...
    model = db.Column(db.String(64))                       # "Niro EV 64kWh MY21"
    color = db.Column(db.String(7))                        # hex like "#0d6efd" — UI badge / chart color
    icon = db.Column(db.String(40))                        # bootstrap-icon class without the "bi-" prefix

    # Hardware — falls back to AppConfig defaults when NULL during
    # the migration window, but new vehicles should always set these.
    battery_kwh = db.Column(db.Float)
    battery_soh_baseline = db.Column(db.Float)
    battery_co2_per_kwh = db.Column(db.Float)
    max_ac_kw = db.Column(db.Float)
    fossil_co2_per_km = db.Column(db.Float)
    recuperation_kwh_per_km = db.Column(db.Float)

    # API credentials — same shape as the legacy AppConfig.vehicle_api_*
    # keys, just per-vehicle. ``auto_sync`` gates whether the background
    # sync loop polls this vehicle (Phase 2 will wire this fully; in
    # Phase 1 only the primary still syncs from AppConfig).
    api_brand = db.Column(db.String(32))                    # 'kia' | 'hyundai' | 'vw' | 'mg' | ...
    api_username = db.Column(db.String(120))
    api_password = db.Column(db.String(120))
    api_pin = db.Column(db.String(40))
    api_region = db.Column(db.String(8))
    api_vin = db.Column(db.String(40))
    auto_sync = db.Column(db.Boolean, default=True)

    # Lifecycle
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    acquired_at = db.Column(db.Date)
    retired_at = db.Column(db.Date)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'brand': self.brand,
            'model': self.model,
            'color': self.color,
            'icon': self.icon,
            'battery_kwh': self.battery_kwh,
            'battery_soh_baseline': self.battery_soh_baseline,
            'battery_co2_per_kwh': self.battery_co2_per_kwh,
            'max_ac_kw': self.max_ac_kw,
            'fossil_co2_per_km': self.fossil_co2_per_km,
            'recuperation_kwh_per_km': self.recuperation_kwh_per_km,
            'api_brand': self.api_brand,
            'api_username': self.api_username,
            'api_pin': self.api_pin,
            'api_region': self.api_region,
            'api_vin': self.api_vin,
            'auto_sync': self.auto_sync,
            'is_archived': self.is_archived,
            'acquired_at': self.acquired_at.isoformat() if self.acquired_at else None,
            'retired_at': self.retired_at.isoformat() if self.retired_at else None,
            'notes': self.notes,
        }


class Charge(db.Model):
    __tablename__ = 'charges'

    id = db.Column(db.Integer, primary_key=True)
    # v2.29.0: per-vehicle scoping. Nullable for the migration window
    # (legacy rows written before vehicles existed); the startup hook
    # backfills every existing row to vehicle_id=1 (Vehicle#1 seeded
    # from AppConfig). Going forward the input form sets it from the
    # currently-active vehicle picker.
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)
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
    operator = db.Column(db.String(64))  # Anbieter/CPO of the charging station
    # Zusatzkosten — added v2.28.59 so providers with start fees /
    # contract base fees / blocking penalties can be tracked alongside
    # the per-kWh price. Both fees roll into total_cost so cost/100km
    # and similar aggregates reflect the true wallet impact.
    start_fee_eur = db.Column(db.Float)     # Vorgangs- oder Grundgebührenanteil
    blocking_fee_eur = db.Column(db.Float)  # Strafgebühr fürs Blockieren
    created_at = db.Column(db.DateTime, default=datetime.now)

    def calculate_fields(self, battery_kwh=None):
        """Auto-calculate derived fields."""
        if self.eur_per_kwh is not None and self.kwh_loaded is not None:
            base_cost = self.eur_per_kwh * self.kwh_loaded
            base_cost += float(self.start_fee_eur or 0)
            base_cost += float(self.blocking_fee_eur or 0)
            self.total_cost = round(base_cost, 2)
        elif self.start_fee_eur is not None or self.blocking_fee_eur is not None:
            # Charge with no kWh-based cost (e.g. PV at €0 with only a
            # Grundgebühr-Anteil) — total still reflects the extras.
            self.total_cost = round(
                float(self.start_fee_eur or 0) + float(self.blocking_fee_eur or 0), 2
            )
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
            'operator': self.operator,
            'start_fee_eur': self.start_fee_eur,
            'blocking_fee_eur': self.blocking_fee_eur,
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
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)  # v2.29.0
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    soc_percent = db.Column(db.Integer)
    odometer_km = db.Column(db.Integer)
    is_charging = db.Column(db.Boolean)
    charge_power_kw = db.Column(db.Float)
    estimated_range_km = db.Column(db.Integer)
    # Extended history columns
    battery_12v_percent = db.Column(db.Integer)
    battery_soh_percent = db.Column(db.Float)
    total_regenerated_kwh = db.Column(db.Float)  # rolling 3-month window (Kia/Hyundai)
    regen_cumulative_kwh = db.Column(db.Float)   # monotonic total since first sync
    consumption_30d_kwh_per_100km = db.Column(db.Float)
    location_lat = db.Column(db.Float)
    location_lon = db.Column(db.Float)
    # ECU timestamp from the payload's vehicleLocation.time — when the car
    # actually reported this GPS fix. Different from `timestamp` (which is
    # just when we polled). On Hyundai, cached-mode responses often echo
    # the last-known GPS long after the car last pinged; this field is
    # how the parking-event state machine detects stale echoes.
    location_last_updated_at = db.Column(db.DateTime)
    raw_json = db.Column(db.Text)

    # Fields used for change detection (any difference triggers a new row).
    # regen_cumulative_kwh is derived from total_regenerated_kwh and must NOT
    # be in this list, otherwise rollover-zero deltas would still trigger rows.
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
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)  # v3.0
    year_from = db.Column(db.Integer, nullable=False)
    year_to = db.Column(db.Integer, nullable=False)
    amount_eur = db.Column(db.Float, nullable=False)


class ParkingEvent(db.Model):
    """A single parking spell. Created when the vehicle stops at a new
    location, closed when it moves >100m away."""
    __tablename__ = 'parking_events'

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)  # v2.29.0
    arrived_at = db.Column(db.DateTime, nullable=False, index=True)
    last_seen_at = db.Column(db.DateTime)  # Most recent sync confirming this position
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


class VehicleTrip(db.Model):
    """Individual trip as reported by the Kia/Hyundai server.

    This is the truth source for Kia/Hyundai vehicles — the car uploads
    a trip record at the end of every drive (unrelated to our polling)
    and the manufacturer server caches it. `update_day_trip_info` pulls
    the full list for a date from the same endpoint the Bluelink/UVO
    mobile apps use — server-side, no car wake-up, no 12V drain.

    `start_time` is derived from (date + hhmmss) in the vehicle's local
    timezone as reported by the server; we store it as naive datetime to
    match the rest of the schema. Trips without a parseable hhmmss are
    skipped at ingest time (should not happen in practice).
    """
    __tablename__ = 'vehicle_trips'

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)  # v2.29.0
    trip_date = db.Column(db.Date, index=True, nullable=False)
    start_time = db.Column(db.DateTime, index=True, nullable=False, unique=True)
    drive_minutes = db.Column(db.Integer)
    idle_minutes = db.Column(db.Integer)
    distance_km = db.Column(db.Float)
    avg_speed_kmh = db.Column(db.Float)
    max_speed_kmh = db.Column(db.Integer)
    source = db.Column(db.String(32), default='sdk_day_trip_info')
    fetched_at = db.Column(db.DateTime, default=datetime.now)


class MaintenanceEntry(db.Model):
    """Maintenance log: inspections, tires, brakes, etc., with optional reminders."""
    __tablename__ = 'maintenance_log'

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), index=True)  # v2.29.0
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
    address = db.Column(db.Text)       # short form: "POI, PLZ Stadt" or "Straße Nr, PLZ Stadt"
    raw_json = db.Column(db.Text)      # full Nominatim response, lets us re-derive short form if format evolves
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
