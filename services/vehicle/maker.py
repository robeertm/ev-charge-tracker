# -*- coding: utf-8 -*-
"""Who built the car — never the integration that reports it.

A car connected through Home Assistant carries ``api_brand =
'homeassistant'``, and an install seeded from the old flat config copied
that key, capitalised, into the free-text ``Vehicle.brand`` field. The
battery certificate then printed "homeassistant · G6" under "Make /
Model": the name of a piece of home-automation software in the place a
buyer reads the car's make. The same raw keys leaked as "Kia_pleos" or
"Skoda_api".

The make is answered here, once, for every place that shows it:

1. ``Vehicle.brand`` — what the owner typed — unless it names an
   integration. A stored brand *key* ("kia_pleos", "Skoda_api") or a known
   make in another spelling ("skoda", "Xpeng") becomes the make's proper
   name; anything else is the owner's word and is kept as written.
2. ``Vehicle.api_brand`` when the key stands for one make
   (``catalog.Brand.maker``). Integration keys have none.
3. The model: a known make in front of it ("XPENG G6"), else a model name
   that the bundled spec catalogue (``ev_specs.json``) knows under exactly
   one make ("G6" → XPENG).
4. The vehicle's name, if it starts with a known make ("XPENG G6").
5. Otherwise ``None`` — and the caller shows the model alone. An honest
   gap beats a wrong make.

Functions take a ``Vehicle`` row or a plain mapping with the same keys
(the certificate passes its data dict).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

def integration_keys() -> List[str]:
    """Registered brand keys that are interfaces, not makers. Derived from
    the catalog, so a new integration only needs ``integration=True`` there."""
    from .catalog import BRANDS
    return [b.key for b in BRANDS if b.integration]


#: Names of integrations and data routes that are not registered as brand
#: keys but may have been typed into the free-text brand field. Compared
#: after folding (lowercase, no accents, letters and digits only).
_INTEGRATION_WORDS = frozenset({
    'homeassistant', 'enode', 'smartcar', 'tronity', 'obd', 'obd2', 'obdii',
    'elm327',
})


def _fold(text) -> str:
    """Lowercase, accents stripped: 'Škoda' → 'skoda'."""
    decomposed = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def _compact(text) -> str:
    """Folded, letters and digits only: 'Home Assistant' → 'homeassistant',
    'Kia (Pleos)' → 'kiapleos', 'Skoda_api' → 'skodaapi'."""
    return re.sub(r'[^0-9a-z]', '', _fold(text))


def _tokens(text) -> List[str]:
    return _fold(text).split()


def _get(vehicle, name):
    if vehicle is None:
        return None
    if isinstance(vehicle, dict):
        return vehicle.get(name)
    return getattr(vehicle, name, None)


@lru_cache(maxsize=1)
def _catalog_maps() -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """(compact key/label → maker or None, compact maker → maker)."""
    from .catalog import BRANDS
    by_key: Dict[str, Optional[str]] = {}
    makers: Dict[str, str] = {}
    for b in BRANDS:
        by_key[_compact(b.key)] = b.maker
        by_key.setdefault(_compact(b.label), b.maker)
        if b.maker:
            makers[_compact(b.maker)] = b.maker
    return by_key, makers


@lru_cache(maxsize=1)
def _spec_data() -> Tuple[Dict[str, str], Dict[Tuple[str, ...], set]]:
    """(compact make → make, model tokens → {makes}) from ev_specs.json."""
    makes: Dict[str, str] = {}
    models: Dict[Tuple[str, ...], set] = {}
    try:
        from .ev_specs_service import _load
        rows = _load().get('vehicles', [])
    except Exception:
        rows = []
    for r in rows:
        make = (r.get('make') or '').strip()
        model = (r.get('model') or '').strip()
        if not make:
            continue
        makes.setdefault(_compact(make), make)
        if model:
            models.setdefault(tuple(_tokens(model)), set()).add(make)
    return makes, models


@lru_cache(maxsize=1)
def _known_makers() -> List[Tuple[Tuple[str, ...], str]]:
    """Every make we can recognise, as (folded tokens, proper name), the
    catalog's spelling winning over the spec dataset's ('XPENG' over
    'XPeng'), longest names first so 'Land Rover' beats a shorter match."""
    spec_makes, _ = _spec_data()
    _, cat_makers = _catalog_maps()
    canon: Dict[str, str] = dict(spec_makes)
    canon.update(cat_makers)
    out = {}
    for name in canon.values():
        toks = tuple(_tokens(name))
        if toks:
            out[toks] = name
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def _canonical(text) -> Optional[str]:
    """The proper name of a known make written some other way, or None."""
    c = _compact(text)
    if not c:
        return None
    _, cat_makers = _catalog_maps()
    if c in cat_makers:
        return cat_makers[c]
    spec_makes, _ = _spec_data()
    return spec_makes.get(c)


def is_integration(text) -> bool:
    """True when ``text`` names an integration rather than a car maker."""
    c = _compact(text)
    if not c:
        return False
    if c in _INTEGRATION_WORDS:
        return True
    by_key, _ = _catalog_maps()
    return c in by_key and by_key[c] is None


def _maker_prefix(text) -> Optional[str]:
    toks = _tokens(text)
    if not toks:
        return None
    for mtoks, name in _known_makers():
        if tuple(toks[:len(mtoks)]) == mtoks:
            return name
    return None


def _maker_from_model_spec(model) -> Optional[str]:
    """A model the spec catalogue lists under exactly one make. The longest
    catalogue model that the stored model starts with wins, so 'G6 Long
    Range' still finds 'G6'."""
    toks = tuple(_tokens(model))
    if not toks:
        return None
    _, models = _spec_data()
    for n in range(len(toks), 0, -1):
        makes = models.get(toks[:n])
        if makes:
            if len(makes) == 1:
                make = next(iter(makes))
                return _canonical(make) or make
            return None
    return None


def display_maker(vehicle) -> Optional[str]:
    """The car's maker for display, or ``None`` when it cannot be told.

    Never returns an integration name such as Home Assistant.
    """
    if vehicle is None:
        return None
    by_key, _ = _catalog_maps()

    typed = (_get(vehicle, 'brand') or '').strip()
    if typed and not is_integration(typed):
        c = _compact(typed)
        if c in by_key:                     # a stored brand key or label
            return by_key[c]
        return _canonical(typed) or typed

    key = (_get(vehicle, 'api_brand') or '').strip().lower()
    if key:
        from .catalog import by_key as _brand
        b = _brand(key)
        if b is not None and b.maker:
            return b.maker

    model = (_get(vehicle, 'model') or '').strip()
    if model:
        m = _maker_prefix(model) or _maker_from_model_spec(model)
        if m:
            return m

    name = (_get(vehicle, 'name') or '').strip()
    if name:
        m = _maker_prefix(name)
        if m:
            return m
    return None


def maker_model_label(vehicle, sep: str = ' · ') -> str:
    """'Maker · Model', or just the model, or just the maker, or ''.

    The maker is not repeated when the model already starts with it
    ('XPENG G6', not 'XPENG · XPENG G6').
    """
    maker = display_maker(vehicle)
    model = (_get(vehicle, 'model') or '').strip()
    if maker and model:
        mt, ot = _tokens(maker), _tokens(model)
        if ot[:len(mt)] == mt:
            return model
        return f'{maker}{sep}{model}'
    return model or maker or ''
