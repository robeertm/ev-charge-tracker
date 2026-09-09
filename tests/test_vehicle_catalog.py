# -*- coding: utf-8 -*-
"""The brand catalog must agree with the connectors and with the image.

The bug this guards against: the car wizard offered a Škoda tile that
stored ``api_brand = 'vw'``. ``registry.get_connector('vw', …)`` returns
``VWConnector`` — ``CONNECTOR_TYPE = 'volkswagen'`` — so a Škoda Enyaq
was signed in against VW WeConnect and could only fail. Meanwhile
``connector_vag`` registered ``skoda``, ``seat``, ``cupra`` and ``audi``
and no UI could reach any of them.

The catalog is read statically here, and the connectors are read as TEXT
rather than imported: their ``register(...)`` calls only run when the
upstream SDK is installed, so importing them would make this test pass
or fail depending on which pip packages happen to be present.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.vehicle.catalog import BRANDS, PACKAGES, brands_for_ui  # noqa: E402


def _registered_keys():
    keys = set()
    for p in (ROOT / 'services' / 'vehicle').glob('connector_*.py'):
        keys |= set(re.findall(r"""\bregister\(\s*['"]([a-z0-9_]+)['"]""", p.read_text()))
    return keys


@pytest.mark.parametrize('brand', BRANDS, ids=lambda b: b.key)
def test_every_offered_brand_has_a_connector(brand):
    """A tile whose key no connector registers means the user picks a car,
    saves it, and the first sync raises "Unknown vehicle brand"."""
    assert brand.key in _registered_keys()


@pytest.mark.parametrize('brand', BRANDS, ids=lambda b: b.key)
def test_every_offered_brand_has_an_installable_package(brand):
    """The install button posts ``pkg`` and the server looks it up in
    PACKAGES. A group key with no entry answers 'Unbekanntes Paket'."""
    if brand.pkg is None:
        return
    assert brand.pkg in PACKAGES


def test_brand_keys_are_unique():
    """Two tiles with the same key are two names for one connector — that
    is exactly how Škoda came to be stored as 'vw'."""
    keys = [b.key for b in BRANDS]
    assert len(keys) == len(set(keys)), f'doppelte Schluessel: {keys}'


def test_the_image_ships_every_package_the_button_would_install():
    """requirements-vehicles.txt is baked into the container image. If it
    misses a package the install button knows about, that brand shows
    "Paket nötig" in a container where nothing can usefully install it —
    the writable layer is discarded by the next `docker compose pull`."""
    req = (ROOT / 'requirements-vehicles.txt').read_text()
    shipped = {re.split(r'[<>=!~ ]', ln.split('#')[0].strip())[0].lower()
               for ln in req.splitlines() if ln.split('#')[0].strip()}
    # Deliberately absent from the image: they drive the one-off browser
    # token and need a browser binary the slim image does not carry.
    on_demand = {'selenium', 'webdriver-manager'}
    wanted = {re.split(r'[<>=!~ ]', pkg)[0].lower()
              for pkgs in PACKAGES.values() for pkg in pkgs} - on_demand
    assert not (wanted - shipped), f'nicht im Abbild: {sorted(wanted - shipped)}'


def test_the_catalog_survives_json_serialisation():
    """It is handed to the template with ``|tojson``."""
    import json
    rows = brands_for_ui()
    assert rows and json.loads(json.dumps(rows)) == rows
    assert all({'key', 'label', 'pkg', 'token',
                'legacy', 'sunset', 'replaced_by'} == set(r) for r in rows)


def test_a_retired_brand_stays_selectable_but_is_not_offered():
    """A legacy brand must still appear in the catalog the fleet form
    renders — a <select> that cannot represent the value stored in the
    database would silently rewrite it the next time the form is saved.
    The wizard filters it out separately, so nobody sets up a NEW car on
    an access the manufacturer is switching off."""
    from services.vehicle.catalog import legacy_keys
    keys = legacy_keys()
    assert 'skoda' in keys, 'the retiring Škoda access must be flagged'
    for k in keys:
        b = next(x for x in BRANDS if x.key == k)
        assert b.sunset, f'{k}: a deprecation without a date is not actionable'
        assert b.replaced_by, f'{k}: say what replaces it'
        assert any(x.key == b.replaced_by for x in BRANDS), \
            f'{k}: replacement {b.replaced_by} is not in the catalog'


def test_labels_of_offered_brands_are_unique():
    """Two tiles reading 'Škoda' would be a coin flip for the user. The
    legacy entry may share the label because the fleet form marks it with
    its sunset date, but the wizard must never show two of the same."""
    offered = [b.label for b in BRANDS if not b.legacy]
    assert len(offered) == len(set(offered)), offered


def test_kia_and_hyundai_are_the_only_token_brands():
    """The wizard shows the token/password pane for ``token: true``. Any
    other brand landing there would be asked for a PIN and a region it
    does not have."""
    assert {b.key for b in BRANDS if b.token} == {'kia', 'hyundai'}
