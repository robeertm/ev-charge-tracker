"""Feature matrix: which fields each brand connector actually populates.

This is hand-curated based on what each upstream library exposes today.
The keys correspond to features users actually care about; the values are
``'yes'`` (works), ``'no'`` (not provided by the API), or ``'partial'``
(works on some models but not others).

Used by the Settings UI to set realistic expectations when a user picks
a brand, and by the dashboard to hide widgets that won't have data.
"""

# Feature keys map directly to translation keys (set.feat.<key>) so we
# don't have to repeat labels here.
FEATURE_KEYS = [
    'soc_range_odo',
    'live_status',
    'location',
    'battery_12v',
    'soh',
    'regenerated',
    'consumption_30d',
    'doors_locks',
    'climate',
    'tires',
    'charge_limits',   # AC/DC SoC limit display (Bluelink/UVO only)
    'charge_eta',      # AC/DC time-to-full estimate (Bluelink/UVO + Tesla)
    'portable_charge', # Portable charger time-to-full (Bluelink/UVO only)
]


# 'yes' / 'no' / 'partial'
MATRIX = {
    # Kia / Hyundai — the gold standard, most data via Bluelink/UVO
    'kia': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'yes',
        'battery_12v':     'yes',
        'soh':             'partial',  # Kona = yes, Niro = computed fallback
        'regenerated':     'yes',
        'consumption_30d': 'yes',
        'doors_locks':     'yes',
        'climate':         'yes',
        'tires':           'yes',
        'charge_limits':   'yes',
        'charge_eta':      'yes',
        'portable_charge': 'yes',
    },
    'hyundai': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'yes',
        'battery_12v':     'yes',
        'soh':             'partial',
        'regenerated':     'yes',
        'consumption_30d': 'yes',
        'doors_locks':     'yes',
        'climate':         'yes',
        'tires':           'yes',
        'charge_limits':   'yes',
        'charge_eta':      'yes',
        'portable_charge': 'yes',
    },

    # Tesla — second-best after the v2.3.5 connector expansion
    'tesla': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'yes',
        'battery_12v':     'no',     # not exposed by Tesla API
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'yes',
        'climate':         'yes',
        'tires':           'yes',    # via tpms_pressure_*
        'charge_limits':   'partial',
        'charge_eta':      'yes',
        'portable_charge': 'no',
    },

    # Renault / Dacia — has location, basic live data
    'renault': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'yes',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'partial',
        'climate':         'partial',
        'tires':           'no',
    },
    'dacia': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'yes',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'partial',
        'climate':         'partial',
        'tires':           'no',
    },

    # VW group — VW removed GPS from WeConnect API in 2024
    'vw':    {'soc_range_odo': 'yes', 'live_status': 'yes', 'location': 'no',
              'battery_12v': 'no', 'soh': 'no', 'regenerated': 'no',
              'consumption_30d': 'no', 'doors_locks': 'partial', 'climate': 'partial', 'tires': 'no'},
    'skoda': {'soc_range_odo': 'yes', 'live_status': 'yes', 'location': 'no',
              'battery_12v': 'no', 'soh': 'no', 'regenerated': 'no',
              'consumption_30d': 'no', 'doors_locks': 'partial', 'climate': 'partial', 'tires': 'no'},
    'seat':  {'soc_range_odo': 'yes', 'live_status': 'yes', 'location': 'no',
              'battery_12v': 'no', 'soh': 'no', 'regenerated': 'no',
              'consumption_30d': 'no', 'doors_locks': 'partial', 'climate': 'partial', 'tires': 'no'},
    'cupra': {'soc_range_odo': 'yes', 'live_status': 'yes', 'location': 'no',
              'battery_12v': 'no', 'soh': 'no', 'regenerated': 'no',
              'consumption_30d': 'no', 'doors_locks': 'partial', 'climate': 'partial', 'tires': 'no'},
    'audi':  {'soc_range_odo': 'yes', 'live_status': 'yes', 'location': 'no',
              'battery_12v': 'no', 'soh': 'no', 'regenerated': 'no',
              'consumption_30d': 'no', 'doors_locks': 'partial', 'climate': 'partial', 'tires': 'no'},

    # Polestar — minimal API
    'polestar': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'no',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'no',
        'climate':         'no',
        'tires':           'no',
    },

    # MG / SAIC — minimal API
    'mg': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'no',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'no',
        'climate':         'no',
        'tires':           'no',
    },

    # Smart #1 / #3
    'smart': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'no',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'no',
        'climate':         'no',
        'tires':           'no',
    },

    # Porsche
    'porsche': {
        'soc_range_odo':   'yes',
        'live_status':     'yes',
        'location':        'no',
        'battery_12v':     'no',
        'soh':             'no',
        'regenerated':     'no',
        'consumption_30d': 'no',
        'doors_locks':     'no',
        'climate':         'no',
        'tires':           'no',
    },
}


def get_features(brand_key: str) -> dict:
    """Return the feature support dict for a brand. Missing keys default
    to 'no' so callers can treat the dict as complete — a brand whose
    entry predates a new FEATURE_KEYS addition still reports the new
    feature as unsupported instead of None."""
    defaults = {k: 'no' for k in FEATURE_KEYS}
    return {**defaults, **MATRIX.get(brand_key, {})}


def features_supported(brand_key: str, *required_keys) -> bool:
    """True if the brand fully supports all listed feature keys."""
    feats = get_features(brand_key)
    return all(feats.get(k) == 'yes' for k in required_keys)
