# -*- coding: utf-8 -*-
"""Dark-mode row tints, singular/plural of the Pleos key hint, phone fleet list."""
import json
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LANGS = ('de', 'en', 'es', 'fr', 'it', 'nl')


def _dark_selectors():
    css = re.sub(r'/\*.*?\*/', '', (ROOT / 'static/css/custom.css').read_text(encoding='utf-8'), flags=re.S)
    out = ''
    for sels, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
        for sel in sels.split(','):
            sel = ' '.join(sel.split())
            if sel.startswith('[data-bs-theme="dark"]'):
                out += sel + '{' + body + '}\n'
    return out


@pytest.mark.parametrize('cls', ['table-success', 'table-info', 'table-warning', 'table-danger'])
def test_coloured_table_rows_have_a_dark_rule(cls):
    rules = _dark_selectors()
    assert re.search(r'\[data-bs-theme="dark"\] \.' + cls + r'\s*\{[^}]*--bs-table-bg', rules), cls


def test_muted_text_and_checkbox_are_lifted_in_coloured_rows():
    css = (ROOT / 'static/css/custom.css').read_text(encoding='utf-8')
    assert re.search(r'\[data-bs-theme="dark"\] :is\(\.table-success[^{]*\.text-muted', css)
    assert re.search(r'\[data-bs-theme="dark"\] :is\(\.table-success[^{]*\.form-check-input', css)


def test_fleet_list_has_phone_layout():
    css = (ROOT / 'static/css/custom.css').read_text(encoding='utf-8')
    assert '.fleet-table' in css and 'max-width: 767.98px' in css
    html = (ROOT / 'templates/settings.html').read_text(encoding='utf-8')
    assert 'fleet-table' in html and 'fleet-actions' in html


@pytest.mark.parametrize('lang', LANGS)
def test_singular_keys_exist_with_placeholders(lang):
    tr = json.loads((ROOT / 'translations' / f'{lang}.json').read_text(encoding='utf-8'))
    for k in ('pleos.key_expires_in_one', 'pleos.key_expired_since_one'):
        assert '{date}' in tr[k] and '{days}' not in tr[k] and '1' in tr[k]


def _hint(days, lang):
    from services import i18n
    from services.vehicle import pleos_key
    i18n.set_language(lang)
    info = {'date': '2026-11-10', 'source': 'read', 'days_left': days}
    orig = pleos_key.expiry
    pleos_key.expiry = lambda v, today=None: info
    try:
        return pleos_key.hint(SimpleNamespace(id=1, api_brand='kia_pleos'))
    finally:
        pleos_key.expiry = orig
        i18n.set_language('de')


@pytest.mark.parametrize('days,lang,needle', [
    (2, 'de', 'noch 2 Tage'), (1, 'de', 'noch 1 Tag)'),
    (-1, 'de', 'vor 1 Tag)'), (-2, 'de', 'vor 2 Tagen'),
    (2, 'en', '2 days left'), (1, 'en', '1 day left'),
    (-1, 'en', '1 day ago'), (-2, 'en', '2 days ago'),
])
def test_pleos_hint_plural(days, lang, needle):
    h = _hint(days, lang)
    assert h and needle in h['text'], h


def test_pleos_hint_today():
    assert 'heute' in _hint(0, 'de')['text']
