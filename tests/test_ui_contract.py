# -*- coding: utf-8 -*-
"""The contract between the browser and the server, checked statically.

Every finding these tests guard against was a real defect a user hit:

  * The car wizard's "Installieren" button called
    ``/api/vehicle/brand-install``. That route has never existed — the one
    that does is ``/api/vehicle/install``. Flask answered with its HTML
    404 page, ``r.json()`` choked on the ``<``, and the customer saw
    "JSON.parse: unexpected character at line 1 column 1". Nobody could
    ever install a connector from the wizard.

  * The wizard's Škoda tile stored ``api_brand = 'vw'``, so an Enyaq was
    handed to the VW WeConnect connector. Audi, Dacia, Seat and Cupra had
    connectors but no way to pick them.

  * Fifteen ``cert.*`` keys were used with a German ``default=`` and
    existed in none of the six translation files, so the battery
    certificate printed German in every language.

They are static on purpose: they need no database, no Flask app and no
optional connector package, so they run everywhere — including on a
machine where none of the vehicle SDKs are installed.
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANGS = ('de', 'en', 'es', 'fr', 'it', 'nl')


def _flask_routes():
    """Every route pattern the app registers, as a regex."""
    pats = set()
    sources = [ROOT / 'app.py'] + list((ROOT / 'services').rglob('*.py'))
    for p in sources:
        for rule in re.findall(r"@\w+\.route\(\s*['\"]([^'\"]+)['\"]", p.read_text()):
            pats.add(re.sub(r'<[^>]+>', '[^/]+', rule))
    return pats


def _template_calls():
    """``fetch('/…')`` / ``xhr.open(…, '/…')`` targets found in templates.

    A URL built by concatenation (``'/api/trips/' + id``) is captured up
    to the quote, which is exactly the prefix that must match a route's
    static part — so those still resolve against the parameterised
    pattern.
    """
    calls = {}
    for p in sorted((ROOT / 'templates').rglob('*.html')):
        txt = p.read_text()
        for pat in (r"""fetch\(\s*['"`](/[^'"`?\s]+)""",
                    r"""\.open\(\s*['"][A-Z]+['"]\s*,\s*['"`](/[^'"`?\s]+)"""):
            for m in re.finditer(pat, txt):
                url = m.group(1)
                line = txt[:m.start()].count('\n') + 1
                calls.setdefault(url, []).append(f'{p.name}:{line}')
    return calls


def _resolves(url, patterns):
    if url.startswith('/static'):
        return True
    for pat in patterns:
        if re.fullmatch(pat, url):
            return True
        # concatenated URL: '/api/trips/' + a + '/' + b + '/split_data'
        if re.match(r'^' + pat.split('[^/]+')[0].rstrip('/') + r'(/|$)', url):
            if url.rstrip('/') == pat.split('[^/]+')[0].rstrip('/'):
                return True
    return False


def test_every_endpoint_the_browser_calls_exists():
    """A fetch() to a route that was never registered returns Flask's HTML
    404, and every caller in this app does ``await r.json()`` on it. The
    user does not see "not found" — they see a JSON parser error."""
    patterns = _flask_routes()
    assert len(patterns) > 50, 'route scan found suspiciously few routes'
    dead = {u: locs for u, locs in _template_calls().items()
            if not _resolves(u, patterns)}
    assert not dead, 'Endpunkte ohne Route: ' + json.dumps(dead, indent=2)


# ── i18n ──────────────────────────────────────────────────────────────
def _used_keys():
    keys = set()
    files = ([ROOT / 'app.py'] + list((ROOT / 'services').rglob('*.py'))
             + list((ROOT / 'templates').rglob('*.html')))
    for p in files:
        keys |= set(re.findall(r"""\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]""", p.read_text()))
    # t('maint.severity_' + item.severity) — the prefix is not a key.
    return {k for k in keys if not k.endswith('_')}


@pytest.mark.parametrize('lang', LANGS)
def test_no_language_is_missing_a_key_another_one_has(lang):
    """All six files must carry the same key set. A key present only in
    German silently renders German text inside an English page."""
    de = json.loads((ROOT / 'translations' / 'de.json').read_text())
    other = json.loads((ROOT / 'translations' / f'{lang}.json').read_text())
    assert set(de) == set(other), (
        f'{lang}: fehlend={sorted(set(de) - set(other))[:10]} '
        f'zusaetzlich={sorted(set(other) - set(de))[:10]}')


def test_every_used_key_is_actually_translated():
    """``t('x', default='deutscher Text')`` renders that German default in
    EVERY language when the key is missing everywhere — which is how the
    battery certificate came to print German for Dutch users."""
    de = json.loads((ROOT / 'translations' / 'de.json').read_text())
    missing = sorted(k for k in _used_keys() if k not in de)
    assert not missing, f'benutzt, aber nirgends uebersetzt: {missing}'


def test_nothing_decides_a_vehicle_is_configured_by_username_alone():
    """The official Škoda API has no username — it authenticates with an
    API key bound to a VIN. Four separate places used to spell the
    "is this car set up?" test as `api_username`, and one of them (the
    sync service) skipped SILENTLY, so a correctly configured Škoda was
    simply never synced and said nothing about it. The answer lives in
    services/vehicle/catalog.credentials_present(); this test stops a
    fifth copy from appearing.
    """
    hits = []
    for p in list((ROOT / 'services').rglob('*.py')) + [ROOT / 'app.py']:
        for i, line in enumerate(p.read_text().split('\n'), 1):
            code = line.split('#')[0]
            if 'api_username' not in code:
                continue
            if re.search(r'(if\s+not\s+[\w.]*api_username\s*[:)]|'
                         r'and\s+[\w.]*api_username\s*[:)])', code):
                hits.append(f'{p.name}:{i}: {line.strip()[:80]}')
    assert not hits, ('these gate on api_username alone:\n  ' + '\n  '.join(hits))
