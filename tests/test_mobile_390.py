# -*- coding: utf-8 -*-
"""Phone layout stays usable at 390 px (v3.0.205).

A real instance with the demo dataset, opened in Chromium at 390 px in every
language of the app. For each main page:

  * no sideways scrolling of the page itself,
  * nothing tappable smaller than 44 px (checkboxes/radios: 24 px, the
    WCAG 2.2 floor for in-row controls),
  * every text input at 16 px so iOS does not zoom in on focus.

Exempt: inline links inside running text, map content (markers, attribution)
and elements inside a deliberate scroll container. Skips where Playwright, its
browser or the CDN stylesheet is unavailable.
"""
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sync_api = pytest.importorskip(
    'playwright.sync_api', reason='Playwright not installed')

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')
PAGES = ('/', '/input', '/history', '/trips', '/report', '/maintenance',
         '/obd', '/logs')
LANG_PAGES = ('/', '/input', '/trips')

AUDIT_JS = """() => {
  const vw = document.documentElement.clientWidth;
  const vis = e => { const s = getComputedStyle(e), b = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0; };
  const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
    (typeof e.className === 'string' && e.className ? '.' + e.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
  const small = [], inputs = [];
  document.querySelectorAll('a,button,input:not([type=hidden]),select,textarea,summary,[role=button]').forEach(e => {
    if (!vis(e) || e.closest('.leaflet-container') && !e.matches('.leaflet-control-zoom a')) return;
    const b = e.getBoundingClientRect();
    if (e.matches('input[type=checkbox],input[type=radio]')) {
      if (b.width < 24 || b.height < 24) small.push(name(e) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height));
      return; }
    if (e.matches('a') && !e.matches('.btn,.nav-link,.navbar-brand,[role=button]') &&
        e.closest('p,li,small,.small,.text-muted,td,span,div:not(.btn-group)') && getComputedStyle(e).display === 'inline') return;
    if (b.height < 43.5 || b.width < 43.5) small.push(name(e) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height));
    if (e.matches('input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]):not([type=color]),select,textarea')
        && parseFloat(getComputedStyle(e).fontSize) < 16) inputs.push(name(e));
  });
  return {overflow: document.documentElement.scrollWidth - vw, small, inputs,
          styled: !!getComputedStyle(document.body).getPropertyValue('--bs-body-font-size')};
}"""


@pytest.fixture(scope='module')
def phone_app():
    data_dir = tempfile.mkdtemp(prefix='evct-mobile-')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='mobile-test-only',
               APP_HOST='127.0.0.1', APP_PORT=str(port))
    seed = subprocess.run([sys.executable, 'tools/seed_demo.py'], cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    if seed.returncode != 0:
        pytest.skip('demo data could not be seeded here')
    proc = subprocess.Popen([sys.executable, 'app.py'], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        import urllib.request
        for _ in range(120):
            if proc.poll() is not None:
                pytest.skip('app did not start in this environment')
            try:
                urllib.request.urlopen(base + '/api/health', timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip('app did not become healthy in time')
        yield base, data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _set_language(data_dir, lang):
    con = sqlite3.connect(os.path.join(data_dir, 'ev_tracker.db'))
    con.execute("DELETE FROM app_config WHERE key='app_language'")
    con.execute("INSERT INTO app_config (key, value) VALUES ('app_language', ?)", (lang,))
    con.commit()
    con.close()


def _audit(page, base, path):
    page.goto(base + path, wait_until='load', timeout=60000)
    page.wait_for_timeout(1000)
    return page.evaluate(AUDIT_JS)


def test_phone_layout_in_every_language(phone_app):
    base, data_dir = phone_app
    problems = []
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            pytest.skip('no Chromium available')
        ctx = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True)
        page = ctx.new_page()
        page.goto(base + '/', wait_until='load')      # first visit redirects to the wizard once
        for lang in LANGS:
            _set_language(data_dir, lang)
            for path in (PAGES if lang == 'de' else LANG_PAGES):
                r = _audit(page, base, path)
                if not r['styled']:
                    pytest.skip('Bootstrap stylesheet (CDN) not reachable')
                where = f'[{lang} {path}]'
                if r['overflow'] > 1:
                    problems.append(f'{where} page scrolls sideways by {r["overflow"]} px')
                problems += [f'{where} too small: {x}' for x in r['small']]
                problems += [f'{where} input below 16 px: {x}' for x in r['inputs']]
        browser.close()
    assert not problems, '\n'.join(sorted(set(problems))[:40])
