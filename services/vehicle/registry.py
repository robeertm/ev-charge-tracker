"""Brand registry — connectors register themselves at import time.

Two questions, and they are NOT the same one:

  * **Can I talk to this car?** — needs the brand's pip package, so
    ``register()`` happens inside each module's ``if HAS_…:`` guard and
    ``get_connector()`` refuses a brand whose package is missing.

  * **What does this brand ask its owner for?** — must be answerable
    *before* anything is installed, because that is what the credential
    form has to draw. The official Škoda API wants an API key and a VIN
    and has no username at all; Kia wants e-mail, password, PIN and
    region. The form used to show all five inputs to everybody, so a
    Škoda owner with a key and a VIN was faced with "Benutzername",
    "Passwort" and "PIN" and had to guess which box the key belonged in.

``describe()`` answers the second question and is called unconditionally,
right next to the guarded ``register()``. A brand that cannot be used yet
can still describe itself, which is exactly what someone setting it up
needs.
"""

_REGISTRY: dict = {}
_DESCRIBED: dict = {}


def describe(key: str, connector_class):
    """Make a brand's credential description available without its package."""
    _DESCRIBED[key] = connector_class


def register(key: str, connector_class):
    _REGISTRY[key] = connector_class
    describe(key, connector_class)


def get_connector(brand_key: str, credentials: dict):
    cls = _REGISTRY.get(brand_key)
    if not cls:
        raise ValueError(f"Unknown vehicle brand: {brand_key}")
    return cls(credentials)


def is_installed(brand_key: str) -> bool:
    """True when this brand's connector can actually be constructed."""
    return brand_key in _REGISTRY


def credential_fields_of(brand_key: str) -> list:
    """What this brand asks its owner for, or ``[]`` if we don't know it.

    Answers for brands whose package is not installed — see the module
    docstring for why that matters.
    """
    cls = _DESCRIBED.get(brand_key)
    if cls is None:
        return []
    try:
        return list(cls.credential_fields() or [])
    except Exception:                       # a connector must not break the form
        return []


def get_available_brands() -> list:
    return [{"key": k, "name": cls.brand_name()} for k, cls in _REGISTRY.items()]


# Auto-import connectors (they register themselves if their deps are installed)
_CONNECTOR_MODULES = [
    'connector_hyundai_kia',
    'connector_vag',
    'connector_tesla',
    'connector_renault',
    'connector_polestar',
    'connector_mg',
    'connector_smart',
    'connector_porsche',
    'connector_xpeng',
    'connector_skoda_public',
]
for _mod in _CONNECTOR_MODULES:
    try:
        __import__(f'services.vehicle.{_mod}')
    except ImportError:
        pass
