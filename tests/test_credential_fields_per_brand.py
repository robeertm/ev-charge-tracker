# -*- coding: utf-8 -*-
"""The credential form must ask for what the brand actually needs.

A customer set up an Enyaq on the official Škoda API, which authenticates
with an API key and a VIN and has **no account at all**. The form showed
him five fixed boxes — user name, password, PIN, region, VIN — and he had
to experiment to find out which one the key belonged in.

The information was already in the code: every connector implements
``credential_fields()`` and always had. Nothing ever read it. The catalog
even carried a comment saying so ("rendering per-brand labels from
``connector.credential_fields()`` is the real fix and its own piece of
work"), and XPENG was left out of the brand list *because* of it.

These tests are static — no Flask, no database, no vehicle SDK — so they
run on an installation where none of the brand packages are present.
That is exactly the situation the form has to work in: someone picking a
brand has not installed anything yet.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.vehicle import registry                          # noqa: E402
from services.vehicle.catalog import (                         # noqa: E402
    BRANDS, FIELD_COLUMN, credentials_of, credentials_present,
    form_fields, required_columns,
)

LANGS = ('de', 'en', 'es', 'fr', 'it', 'nl')


@pytest.mark.parametrize('brand', BRANDS, ids=lambda b: b.key)
def test_every_brand_describes_itself_without_its_package(brand):
    """``register()`` needs the pip package; describing the brand must not.

    Otherwise the form can only draw boxes for brands you have already
    installed — and you install a brand *after* choosing it, not before.
    """
    assert form_fields(brand.key), (
        f'{brand.key} names no credential fields — the form would fall '
        f'back to showing every box again')


@pytest.mark.parametrize('brand', BRANDS, ids=lambda b: b.key)
def test_every_field_maps_to_a_stored_column(brand):
    for f in form_fields(brand.key):
        assert f['column'] in set(FIELD_COLUMN.values())
        assert f['input_id'] == 'vf_' + f['column']


@pytest.mark.parametrize('brand', BRANDS, ids=lambda b: b.key)
def test_every_field_can_be_translated(brand):
    """A label without a key would print German in the English UI."""
    import json
    texte = {lang: json.loads((ROOT / 'translations' / f'{lang}.json')
                              .read_text(encoding='utf-8'))
             for lang in LANGS}
    for f in registry.credential_fields_of(brand.key):
        if f.get('key') not in FIELD_COLUMN:
            continue
        assert f.get('label_key'), f'{brand.key}/{f["key"]} has no label_key'
        for lang in LANGS:
            assert f['label_key'] in texte[lang], \
                f'{f["label_key"]} missing from {lang}.json'
            if f.get('help_key'):
                assert f['help_key'] in texte[lang], \
                    f'{f["help_key"]} missing from {lang}.json'


def test_the_official_skoda_api_asks_for_a_key_and_a_vin_and_nothing_else():
    """The customer's case, as a test.

    Seen red against the old form, which showed all five boxes for every
    brand alike.
    """
    spalten = [f['column'] for f in form_fields('skoda_api')]
    assert spalten == ['api_password', 'api_vin']
    assert 'api_username' not in spalten
    assert 'api_pin' not in spalten


def test_kia_still_asks_for_account_pin_and_region():
    spalten = [f['column'] for f in form_fields('kia')]
    assert spalten == ['api_username', 'api_password', 'api_pin', 'api_region']


def test_a_pin_is_not_required_to_count_as_configured():
    """Reading a car needs no PIN — the connector defaults it to ''.

    Requiring it would tell every existing Kia owner without one that
    their car is not set up, and hide the test and sync buttons.
    """
    assert 'api_pin' not in required_columns('kia')
    assert credentials_present('kia', username='u', password='p')


def test_a_leftover_user_name_does_not_make_a_skoda_look_configured():
    """Switching an old Škoda to the official API leaves the account name
    behind. It is not a credential this connector will ever send."""
    assert not credentials_present('skoda_api', username='alt@example.com')
    assert credentials_present('skoda_api', password='key', vin='TMBTESTVIN000001')


class _Fahrzeug:
    api_username = 'u@example.com'
    api_password = 'geheim'
    api_pin = '1234'
    api_region = 'de_DE'
    api_vin = 'TMBTESTVIN000001'


def test_the_credential_dict_carries_locale():
    """Renault and Dacia read ``locale``, and no hand-written copy of the
    credential dict ever had that key — so a French owner could pick their
    region in the form and still be signed in against de_DE."""
    creds = credentials_of(_Fahrzeug())
    assert creds['locale'] == 'de_DE'
    assert creds['region'] == 'de_DE'
    assert creds['username'] == 'u@example.com'


def test_nobody_writes_an_eighth_copy_of_the_credential_dict():
    """There were seven, and they had drifted. One is enough."""
    # Look for the SHAPE, not for a single line: a dict that binds
    # 'username' and 'password' to api_* in the same literal. A prefill
    # dict that happens to carry one user name is not this thing —
    # searching for the term alone flagged the car wizard's, which is
    # not a credential dict at all.
    muster = re.compile(
        r"""['"]username['"]\s*:\s*[^\n]*api_username[^\n]*\n"""
        r"""\s*['"]password['"]\s*:\s*[^\n]*api_password""")
    treffer = []
    for p in [ROOT / 'app.py'] + sorted((ROOT / 'services').rglob('*.py')):
        if p.name == 'catalog.py':
            continue
        text = p.read_text(encoding='utf-8')
        for m in muster.finditer(text):
            treffer.append(f'{p.relative_to(ROOT)}:'
                           f'{text[:m.start()].count(chr(10)) + 1}')
    assert not treffer, (
        'credential dict built by hand instead of catalog.credentials_of: '
        + ', '.join(treffer))


def test_the_form_does_not_normalise_a_region_it_does_not_own():
    """One column carries 'EU' (Kia), 'eu' (MG) and 'de_DE' (Renault).

    The save route used to upper-case all of them. MG's gateway table is
    lower-case and falls back to Europe on a miss, so a China MG was
    silently served the EU gateway.
    """
    quelle = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert "request.form.get('api_region', '') or '').strip().upper()" not in quelle
    mg = (ROOT / 'services' / 'vehicle' / 'connector_mg.py').read_text(encoding='utf-8')
    assert '.strip().lower()' in mg
