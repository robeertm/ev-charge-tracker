# -*- coding: utf-8 -*-
"""The one brand catalog: what the UI offers, and what each brand needs.

Before this module there were three lists of brands and they had drifted
apart:

  * ``templates/_car_wizard.html`` carried a hard-coded JavaScript array,
  * ``templates/settings.html`` carried a hard-coded ``<select>``,
  * ``app.py`` carried the pip-package map for the install button.

The drift was not cosmetic. The wizard offered a Škoda tile that wrote
``api_brand = 'vw'``, so an Enyaq was handed to ``VWConnector`` — the VW
WeConnect connector — and the sign-in could only fail. The same tile
covered Seat and Cupra, which are two different ``BRAND_PARAM`` values
upstream, and Audi and Dacia had no tile at all although
``services.vehicle.connector_vag`` and ``connector_renault`` register
them. Four working connectors were unreachable from the UI and one was
reachable under the wrong name.

So the catalog lives here, once, next to the connectors it describes, and
both templates render from it. ``tests/test_vehicle_catalog.py`` checks
every key against the ``register(...)`` calls in the connector modules —
a tile that names a brand no connector registers is now a failing test,
not a support mail.

``pkg`` is a *group* key, not a package name: several brands share one
pip install (Seat and Cupra are both ``seatcupra``). ``None`` means the
brand needs nothing beyond the core requirements.
"""
from __future__ import annotations

from typing import Dict, List, Optional


class Brand:
    """One selectable brand.

    ``key``    — the registry key, i.e. exactly what is stored in
                 ``Vehicle.api_brand`` and looked up by
                 ``registry.get_connector()``.
    ``label``  — what the user sees. Product names, not brand keys.
    ``pkg``    — package group for the install button, or ``None``.
    ``token``  — True when the brand uses the Kia/Hyundai sign-in pane
                 (password via the SDK's CCI flow, browser token as
                 fallback) instead of the plain credential pane.
    ``legacy`` — the brand still works but its API is being retired. It
                 stays selectable so existing vehicles keep their stored
                 value (a <select> that cannot represent what is in the
                 database would silently rewrite it on the next save),
                 but it is not offered as a choice to someone setting up
                 a new car.
    ``sunset`` — free text naming when the old access stops, shown next
                 to the label. A date is what makes a deprecation
                 actionable; "deprecated" on its own is not.
    """

    __slots__ = ('key', 'label', 'pkg', 'token', 'legacy', 'sunset', 'replaced_by')

    def __init__(self, key: str, label: str, pkg: Optional[str], token: bool = False,
                 legacy: bool = False, sunset: str = '', replaced_by: str = ''):
        self.key = key
        self.label = label
        self.pkg = pkg
        self.token = token
        self.legacy = legacy
        self.sunset = sunset
        self.replaced_by = replaced_by

    def as_dict(self) -> dict:
        return {'key': self.key, 'label': self.label,
                'pkg': self.pkg or '', 'token': self.token,
                'legacy': self.legacy, 'sunset': self.sunset,
                'replaced_by': self.replaced_by}


# Order is display order: the two brands with the richest data first, then
# the VW group, then the rest roughly by how often they come up.
BRANDS: List[Brand] = [
    Brand('hyundai',  'Hyundai',      'hyundai-kia', token=True),
    Brand('kia',      'Kia',          'hyundai-kia', token=True),
    # The official Škoda API — no package, plain HTTPS with an API key
    # the owner creates in the MyŠkoda app.
    Brand('skoda_api', 'Škoda',        None),
    # The old access. Škoda stops serving third-party clients in October
    # 2026; kept selectable so existing cars keep working until they are
    # switched over, but not offered to anyone setting up a new one.
    Brand('skoda',    'Škoda',        'skoda',
          legacy=True, sunset='10/2026', replaced_by='skoda_api'),
    Brand('vw',       'VW',           'vw'),
    Brand('seat',     'Seat',         'seatcupra'),
    Brand('cupra',    'Cupra',        'seatcupra'),
    Brand('audi',     'Audi',         'audi'),
    Brand('tesla',    'Tesla',        'tesla'),
    Brand('renault',  'Renault',      'renault'),
    Brand('dacia',    'Dacia',        'renault'),
    Brand('polestar', 'Polestar',     'polestar'),
    Brand('mg',       'MG',           'mg'),
    Brand('smart',    'Smart #1/#3',  'smart'),
    Brand('porsche',  'Porsche',      'porsche'),
]

# XPENG is deliberately NOT in the list above. Its connector is
# registered and works, but it authenticates against the Enode
# aggregator with a client ID and a client secret, not with the car
# account's e-mail and password. Both credential panes are labelled for
# the latter, so a tile would ask for the wrong two things. Rendering
# per-brand labels from ``connector.credential_fields()`` is the real
# fix and its own piece of work; until then XPENG stays reachable by
# setting ``api_brand`` directly rather than half-offered here.


# pip packages per group key. The install button and the native
# installer both read this; the container image installs
# requirements-vehicles.txt instead, which must stay in step with it
# (tests/test_vehicle_catalog.py checks that too).
PACKAGES: Dict[str, List[str]] = {
    # >=4.26.5 for the headless CCI password sign-in (bypasses the IdP
    # WAF that blocks the legacy browser authorize, upstream #1273).
    # selenium/webdriver-manager stay for the browser-login *fallback*.
    'hyundai-kia': ['hyundai-kia-connect-api>=4.26.5', 'selenium', 'webdriver-manager'],
    'vw':          ['carconnectivity', 'carconnectivity-connector-volkswagen'],
    'skoda':       ['carconnectivity', 'carconnectivity-connector-skoda'],
    'seatcupra':   ['carconnectivity', 'carconnectivity-connector-seatcupra'],
    'audi':        ['carconnectivity', 'carconnectivity-connector-audi'],
    'tesla':       ['teslapy'],
    'renault':     ['renault-api', 'aiohttp'],
    'polestar':    ['pypolestar'],
    'mg':          ['saic-ismart-client-ng'],
    'smart':       ['pySmartHashtag'],
    'porsche':     ['pyporscheconnectapi'],
}


def brands_for_ui() -> List[dict]:
    """The catalog as plain dicts, ready for ``|tojson`` in a template.

    Includes the legacy entries: the fleet form must be able to display
    what is already stored.
    """
    return [b.as_dict() for b in BRANDS]


def by_key(key: str) -> Optional[Brand]:
    for b in BRANDS:
        if b.key == key:
            return b
    return None


def legacy_keys() -> List[str]:
    """Brands whose API is being retired — the ones worth warning about."""
    return [b.key for b in BRANDS if b.legacy]


def credentials_present(brand: str, username: str = '', password: str = '',
                        vin: str = '') -> bool:
    """Is this vehicle configured enough to talk to its API?

    Three places used to spell this out as ``api_brand and api_username``,
    which was true for every brand until the official Škoda API arrived —
    it authenticates with an API key and a VIN and has **no username at
    all**. A correctly configured Škoda would therefore have been told it
    had no credentials, by the "test connection" button, by the manual
    sync and by the fleet table, all three.

    So the question is asked once, here, in terms of what the brands
    actually need: either a user account, or a key bound to a VIN.
    """
    if not (brand or '').strip():
        return False
    if (username or '').strip():
        return True
    return bool((password or '').strip() and (vin or '').strip())
