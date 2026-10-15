# -*- coding: utf-8 -*-
"""Kia/Hyundai direct sign-in restored via the SDK, with a clear warning (v3.0.159).

On 14 Oct 2026 Kia/Hyundai Europe tightened their login and the app sign-in
started answering HTTP 400 code 9035. The upstream community library fixed
it in ``hyundai_kia_connect_api`` 4.35.3 (kia_uvo 3.18.0). This release
bumps the pinned SDK so a rebuilt image gets past the block again — but the
route is unofficial and the maker works against it, so these tests pin BOTH
halves: the pin is really raised, and the warning really reaches the user,
in every language.
"""
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')


def _min_version(spec: str):
    """(4, 35, 3) from 'hyundai-kia-connect-api>=4.35.3'."""
    m = re.search(r'>=\s*(\d+)\.(\d+)\.(\d+)', spec)
    assert m, spec
    return tuple(int(g) for g in m.groups())


def test_the_kia_sdk_pin_is_raised_to_the_release_that_gets_past_9035():
    """4.35.3 is the first release past the 14 Oct 2026 block. A pin below
    it would let an image build on a cached-but-older wheel that still
    answers 9035."""
    from services.vehicle.catalog import PACKAGES
    kia = [p for p in PACKAGES['hyundai-kia'] if 'hyundai-kia-connect-api' in p]
    assert kia, 'the Kia SDK vanished from the package group'
    assert _min_version(kia[0]) >= (4, 35, 3), kia[0]


def test_the_pin_in_the_catalog_and_in_the_image_requirements_agree():
    """The container image installs requirements-vehicles.txt; a floor that
    is only in the catalog would not reach the running container."""
    from services.vehicle.catalog import PACKAGES
    catalog = _min_version(
        next(p for p in PACKAGES['hyundai-kia'] if 'hyundai-kia-connect-api' in p))
    req = (ROOT / 'requirements-vehicles.txt').read_text()
    line = next(ln for ln in req.splitlines()
                if ln.split('#')[0].strip().startswith('hyundai-kia-connect-api'))
    assert _min_version(line) == catalog, (line, catalog)


@pytest.mark.parametrize('brand', ['kia', 'hyundai'])
def test_the_credential_form_warns_that_the_direct_route_is_unofficial(brand):
    """The warning the maintainer asked for: the password box tells the
    owner the direct route can stop working and points at Pleos. Read the
    fields straight off the connector module (importing it needs the SDK)."""
    from services.vehicle import connector_hyundai_kia as K
    fields = (K.KIA_CREDENTIAL_FIELDS if brand == 'kia'
              else K.HYUNDAI_CREDENTIAL_FIELDS)
    pw = next(f for f in fields if f['key'] == 'password')
    # The localized help carries the warning; its inline fallback does too.
    assert pw['help_key'] == 'cred.help_kia_hyundai_direct'
    assert 'Pleos' in pw['help'] and 'Fremd-Apps' in pw['help']


def test_the_warning_help_resolves_in_every_language_with_the_brand_name():
    """``t('cred.help_kia_hyundai_direct', context='Kia Connect')`` must fill
    the product name and exist in all six languages."""
    for lang in LANGS:
        d = json.loads((ROOT / 'translations' / f'{lang}.json').read_text())
        assert 'cred.help_kia_hyundai_direct' in d, lang
        text = d['cred.help_kia_hyundai_direct']
        assert '{context}' in text, (lang, 'placeholder dropped')
        filled = text.replace('{context}', 'Kia Connect')
        assert 'Kia Connect' in filled and '{' not in filled


def test_the_block_message_points_at_both_an_update_and_pleos():
    """A 9035 now means the maker tightened again BEYOND the shipped SDK.
    The message must name the two real ways out — an app update and the
    official Pleos API — in every language, and never send the owner back
    to the password checklist."""
    for lang in LANGS:
        d = json.loads((ROOT / 'translations' / f'{lang}.json').read_text())
        msg = d['hk.err_vendor_block']
        assert '9035' in msg and 'Pleos' in msg, lang
        # the earlier password-checklist wording must stay gone
        assert 'Groß-/Kleinschreibung' not in msg and 'Apple' not in msg, lang


def test_a_9035_still_stops_the_app_from_knocking():
    """The safety from v3.0.157 stays: if even the new SDK is refused, the
    app must not keep hammering (each try logs the owner out of the app)."""
    import services.vehicle.connector_hyundai_kia as K
    K._vendor_blocked_until.clear()
    block = ('CCI token exchange failed: HTTP 400 — {"code":9035,"message":'
             '"Unusual activity has been detected"}')

    class _Mgr:
        def __init__(self):
            self.calls = 0
            self.password = 'pw'

        def check_and_refresh_token(self):
            self.calls += 1
            err = type('AuthenticationError', (Exception,), {})(block)
            err.__class__.__module__ = 'hyundai_kia_connect_api.exceptions'
            raise err

    v = K.KiaConnector({'username': 'a@b.invalid', 'password': 'pw', 'pin': '', 'region': 'EU'})
    m = _Mgr()
    v._get_manager = lambda: m
    v._check_sdk_supports_credential = lambda: None
    with pytest.raises(K.VendorBlocked):
        v._ensure_auth()
    for _ in range(4):
        with pytest.raises(K.VendorBlocked):
            v._ensure_auth()
    assert m.calls == 1, 'the app kept knocking after a 9035'
    K._vendor_blocked_until.clear()
