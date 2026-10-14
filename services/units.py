"""Distance units — what the owner reads, never what is stored (v3.0.155).

Everything in the database is kilometres: odometers, ranges, trip
distances, km/h, kWh per 100 km, g CO₂ per km. A car that reports miles is
converted the moment its numbers arrive (services/vehicle/base.to_km), so
every rule inside the app — "the odometer moved", "a trip of 2 km",
"kWh per 100 km" — keeps meaning one thing.

Only the page converts, in both directions:

* shown:   km → the chosen unit (``dist``, ``per_dist``)
* entered: the chosen unit → km (``to_km``, ``per_dist_to_km``)

🔑 Two shapes, and nothing else:

* a LENGTH (odometer, range, trip, speed) gets smaller in miles:  × 0.621…
* a RATE PER LENGTH (kWh/100 km, €/100 km, kWh/km, g/km) gets larger:
  × 1.609… — a mile is longer, so more of everything is spent on it.

kWh per 100 mi (not mi/kWh) on purpose: it is what the EPA prints on a US
window sticker, and it points the same way as kWh/100 km — lower is better
— so every colour band, threshold and "good/bad" arrow stays right.

The setting is installation-wide like the language (AppConfig
``distance_unit``), read once per request in ``init_app``.
"""
from __future__ import annotations

KM_PER_MI = 1.609344
UNITS = ('km', 'mi')
_DEFAULT = 'km'
_current = _DEFAULT


def set_unit(unit):
    global _current
    _current = unit if unit in UNITS else _DEFAULT


def get_unit():
    return _current


def is_mi():
    return _current == 'mi'


def _num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round(v, nd):
    if nd is None:
        return v
    return int(round(v)) if nd == 0 else round(v, nd)


# ── shown: km → chosen unit ───────────────────────────────────────────
def dist(km, nd=None, mi_nd=None):
    """A length (km, km/h) in the chosen unit. None stays None.

    In km and without ``nd`` the value comes back exactly as given — a page
    that printed ``12`` keeps printing ``12``, not ``12.0``. ``mi_nd`` rounds
    only in miles (a whole-km trip is 7.5 mi, not 7.456…)."""
    if not is_mi():
        if nd is None or km is None or km == '':
            return km
        v = _num(km)
        return None if v is None else _round(v, nd)
    v = _num(km)
    if v is None:
        return None
    return _round(v / KM_PER_MI, mi_nd if mi_nd is not None else nd)


def per_dist(x_per_km, nd=None, mi_nd=None):
    """A rate per length (kWh/100 km, €/100 km, kWh/km, g/km) per the
    chosen unit. Same rules as ``dist``."""
    if not is_mi():
        if nd is None or x_per_km is None or x_per_km == '':
            return x_per_km
        v = _num(x_per_km)
        return None if v is None else _round(v, nd)
    v = _num(x_per_km)
    if v is None:
        return None
    return _round(v * KM_PER_MI, mi_nd if mi_nd is not None else nd)


# ── entered: chosen unit → km ─────────────────────────────────────────
def to_km(value, nd=None):
    """A length the owner typed, back to km for storage."""
    v = _num(value)
    if v is None:
        return None
    return _round(v * KM_PER_MI if is_mi() else v, nd)


def per_dist_to_km(value, nd=None):
    """A rate per length the owner typed, back to "per km"."""
    v = _num(value)
    if v is None:
        return None
    return _round(v / KM_PER_MI if is_mi() else v, nd)


def km_input(value, original_km=None, as_int=True, nd=0):
    """What the owner typed (in the chosen unit) as km for storage.

    🔴 A form that is only saved, not changed, must not move anything: 37 750
    km shows as 23 457 mi, and 23 457 mi back is 37 751 km. When the field
    still holds exactly what the page rendered for ``original_km`` (the
    form sends it along as ``<name>__km``), the stored value comes back
    untouched. In km the result is what the app always parsed (int(float)).
    """
    if value is None or str(value).strip() == '':
        return None
    v = _num(str(value).strip().replace(',', '.'))
    if v is None:
        return None
    if not is_mi():
        return int(v) if as_int else v
    o = _num(original_km)
    if o is not None and round(o / KM_PER_MI, nd) == round(v, nd):
        return int(o) if as_int else o
    km = v * KM_PER_MI
    return int(round(km)) if as_int else round(km, 3)


def rate_input(value, original_per_km=None, nd=3):
    """A rate per length the owner typed (kWh/mi, g/mi) back to "per km".
    Same rule as ``km_input``: an unchanged field returns the stored value
    untouched. In km the result is what the app always parsed."""
    if value is None or str(value).strip() == '':
        return None
    v = _num(str(value).strip().replace(',', '.'))
    if v is None:
        return None
    if not is_mi():
        return v
    o = _num(original_per_km)
    if o is not None and round(o * KM_PER_MI, nd) == round(v, nd):
        return o
    return v / KM_PER_MI


# ── labels ────────────────────────────────────────────────────────────
def dist_label():
    return 'mi' if is_mi() else 'km'


def speed_label():
    return 'mph' if is_mi() else 'km/h'


def cons_label():
    return 'kWh/100 mi' if is_mi() else 'kWh/100 km'


# The unit tokens a translation may carry. t() fills them before any
# ``{placeholder}`` formatting, so a text with both works.
def tokens():
    try:
        from services.i18n import get_language
        comma = get_language() != 'en'
    except Exception:
        comma = False

    def num(v, nd):
        txt = ('%.' + str(nd) + 'f') % v
        return txt.replace('.', ',') if comma else txt

    return {
        '{u_dist}': dist_label(),
        '{u_speed}': speed_label(),
        '{u_cons}': cons_label(),
        '{u_per100}': '/100 mi' if is_mi() else '/100 km',
        '{u_per}': '/mi' if is_mi() else '/km',
        # the reference figures some hints quote (EU ICE mean, Niro regen)
        '{u_co2_ref}': '%s g%s' % (num(per_dist(164), 0), '/mi' if is_mi() else '/km'),
        '{u_recup_ref}': '%s kWh%s' % (num(per_dist(0.086), 3), '/mi' if is_mi() else '/km'),
        '{u_cur}': cur(),
    }


def fill(text):
    if not text or '{u_' not in text:
        return text
    for k, v in tokens().items():
        text = text.replace(k, v)
    return text


def js_config():
    """What the page's own scripts need to convert API numbers (which stay
    in km — Home Assistant and every other caller rely on that)."""
    return {
        'unit': get_unit(),
        'dist': dist_label(),
        'speed': speed_label(),
        'cons': cons_label(),
        'per100': '/100 mi' if is_mi() else '/100 km',
        'per': '/mi' if is_mi() else '/km',
        'f': (1 / KM_PER_MI) if is_mi() else 1.0,
        'cur': cur(),
        'curPre': cur_pre(),
        'curCode': cur_code(),
    }


# ── currency (v3.0.155) ──────────────────────────────────────────────
# Money is NEVER converted: every price in the app is what the owner typed
# or what their own tariff says, so it already is in their currency. Only
# the sign changes — "€0.30/kWh" on a US install was simply a wrong label.
# PDFs print the ISO code: their core font is latin-1 and has no zł or ₹.
CURRENCIES = {
    'EUR': '€', 'USD': '$', 'GBP': '£', 'CHF': 'CHF', 'CAD': 'CA$', 'AUD': 'A$',
    'NZD': 'NZ$', 'SEK': 'kr', 'NOK': 'kr', 'DKK': 'kr', 'ISK': 'kr', 'PLN': 'zł',
    'CZK': 'Kč', 'HUF': 'Ft', 'RON': 'lei', 'BGN': 'лв', 'TRY': '₺', 'JPY': '¥',
    'CNY': '¥', 'KRW': '₩', 'INR': '₹', 'ZAR': 'R', 'BRL': 'R$', 'MXN': 'MX$',
    'SGD': 'S$', 'HKD': 'HK$', 'ILS': '₪', 'AED': 'AED', 'UAH': '₴',
}
_EURO_AREA = {'AT', 'BE', 'CY', 'DE', 'EE', 'ES', 'FI', 'FR', 'GR', 'HR', 'IE', 'IT',
              'LT', 'LU', 'LV', 'MT', 'NL', 'PT', 'SI', 'SK', 'AD', 'MC', 'SM', 'VA',
              'ME', 'XK'}
COUNTRY_CURRENCY = dict({c: 'EUR' for c in _EURO_AREA}, **{
    'US': 'USD', 'PR': 'USD', 'GB': 'GBP', 'CH': 'CHF', 'LI': 'CHF', 'CA': 'CAD',
    'AU': 'AUD', 'NZ': 'NZD', 'SE': 'SEK', 'NO': 'NOK', 'DK': 'DKK', 'IS': 'ISK',
    'PL': 'PLN', 'CZ': 'CZK', 'HU': 'HUF', 'RO': 'RON', 'BG': 'BGN', 'TR': 'TRY',
    'JP': 'JPY', 'CN': 'CNY', 'KR': 'KRW', 'IN': 'INR', 'ZA': 'ZAR', 'BR': 'BRL',
    'MX': 'MXN', 'SG': 'SGD', 'HK': 'HKD', 'IL': 'ILS', 'AE': 'AED', 'UA': 'UAH',
})
# Countries whose roads are signed in miles.
MILES_COUNTRIES = {'US', 'GB', 'PR', 'LR', 'MM', 'VI', 'GU', 'AS', 'MP'}
_DEFAULT_CURRENCY = 'EUR'
_currency = _DEFAULT_CURRENCY


def set_currency(code):
    global _currency
    _currency = code if code in CURRENCIES else _DEFAULT_CURRENCY


def get_currency():
    return _currency


def cur():
    """The sign, as written after a number ("9.00 €", "9.00 $")."""
    return CURRENCIES.get(_currency, '€')


def cur_pre():
    """The sign before a number: "€9", "$9" — but "CHF 9", not "CHF9"."""
    sym = cur()
    return sym + '\u00a0' if sym[-1:].isalpha() else sym


def cur_code():
    return _currency


def country_of(accept_languages):
    """The country in the browser's language list ("en-US" → "US"), first
    entry that names one. None when the browser names none (a script, a
    health check, Home Assistant) — then nothing is guessed."""
    for tag in accept_languages or ():
        parts = str(tag).replace('_', '-').split('-')
        if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
            return parts[-1].upper()
    return None


def unit_for(country):
    return 'mi' if country in MILES_COUNTRIES else 'km'


def resolve(stored_unit, stored_currency, accept_languages):
    """(unit, currency, currency_to_remember) for one request.

    * unit: the stored choice, or — "auto", the default — what the
      browser's country drives on: it follows the device, like a phone.
    * currency: the stored choice. Never stored yet → the browser's
      country decides ONCE and that is remembered: money must not change
      its sign because someone opened the page on a phone set to en-US.
    """
    country = country_of(accept_languages)
    unit = stored_unit if stored_unit in UNITS else (unit_for(country) if country else _DEFAULT)
    if stored_currency in CURRENCIES:
        return unit, stored_currency, None
    detected = COUNTRY_CURRENCY.get(country) if country else None
    return unit, detected or _DEFAULT_CURRENCY, detected


SETTLED_MARK = 'region_settled_v3_0_155'


def settle_existing_install(db):
    """Once, at the first start of v3.0.155: an install that already HAS
    data keeps exactly what it showed until now. Until v3.0.154 there was
    no detection — German, euros and km were the defaults — so a family
    member opening the page on a phone set to en-US must not suddenly get
    English, miles and a "$" in front of prices typed in euros.

    * German UI (stored, or never chosen = the old default) → de, EUR, km
      written down explicitly;
    * a US/Canadian Kia/Hyundai → USD/CAD and miles/km;
    * a different stored language (en, fr …) → left to the browser: that
      owner already chose for themselves, and a US owner reading English
      is exactly who the detection is for.

    A fresh install (no data yet) is only marked — it detects from the
    browser from its first page on. Returns what was written."""
    from sqlalchemy import text
    from models.database import AppConfig
    if AppConfig.get(SETTLED_MARK):
        return None
    try:
        has_data = bool(db.session.execute(text(
            'SELECT (SELECT COUNT(*) FROM charges) + (SELECT COUNT(*) FROM vehicle_syncs)')).scalar())
    except Exception:
        has_data = False
    written = {}
    if has_data:
        regions = {(r or '').strip().upper() for (r,) in db.session.execute(
            text('SELECT api_region FROM vehicles')).fetchall()}
        lang = AppConfig.get('app_language')
        if 'US' in regions:
            written = {'currency': 'USD', 'distance_unit': 'mi'}
        elif 'CA' in regions:
            written = {'currency': 'CAD', 'distance_unit': 'km'}
        if lang in (None, '', 'de'):
            written.setdefault('currency', 'EUR')
            written.setdefault('distance_unit', 'km')
            written['app_language'] = 'de'
        for k, v in written.items():
            if AppConfig.get(k) in (None, ''):
                AppConfig.set(k, v)
    AppConfig.set(SETTLED_MARK, 'done')
    return written


def init_app(app):
    from flask import request
    from models.database import AppConfig

    @app.before_request
    def _set_unit():
        try:
            langs = [l for l, _q in request.accept_languages]
        except Exception:
            langs = []
        unit, currency, remember = resolve(
            AppConfig.get('distance_unit', 'auto'), AppConfig.get('currency'), langs)
        set_unit(unit)
        set_currency(currency)
        if remember:
            try:
                AppConfig.set('currency', remember)
                AppConfig.set('currency_detected_from', ','.join(langs[:3])[:64])
            except Exception:
                pass

    register_jinja(app.jinja_env)


def register_jinja(env):
    """The filters and globals the templates use — one place, so the
    template compile check (tests/test_settings_sections.py) registers
    exactly what the app does."""
    env.filters['dist'] = dist
    env.filters['per_dist'] = per_dist
    env.globals['dist_unit'] = dist_label
    env.globals['speed_unit'] = speed_label
    env.globals['cons_unit'] = cons_label
    env.globals['units_js'] = js_config
    env.globals['distance_unit'] = get_unit
    env.globals['cur'] = cur
    env.globals['cur_pre'] = cur_pre
    env.globals['cur_code'] = cur_code
    env.globals['currencies'] = CURRENCIES
