"""Brand registry — connectors register themselves at import time."""

_REGISTRY: dict = {}


def register(key: str, connector_class):
    _REGISTRY[key] = connector_class


def get_connector(brand_key: str, credentials: dict):
    cls = _REGISTRY.get(brand_key)
    if not cls:
        raise ValueError(f"Unknown vehicle brand: {brand_key}")
    return cls(credentials)


def get_available_brands() -> list:
    return [{"key": k, "name": cls.brand_name()} for k, cls in _REGISTRY.items()]


# Auto-import connectors (they register themselves if their deps are installed)
try:
    from . import connector_hyundai_kia  # noqa: F401
except ImportError:
    pass

try:
    from . import connector_vag  # noqa: F401
except ImportError:
    pass
