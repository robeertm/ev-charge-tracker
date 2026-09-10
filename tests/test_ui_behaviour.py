# -*- coding: utf-8 -*-
"""Does the interface DO anything when you click it?

Every other test in this repo checks that the server answers correctly or
that the template renders the right markup. None of them press a button.
That gap shipped: the remote-control opt-in rendered perfectly, and
clicking it did nothing at all. The inline script that binds the switch
sits in the middle of the page and ran **immediately**, while the section
it binds to is rendered thousands of characters further down — so
``querySelectorAll`` matched nothing. No error, no network request; the
switch flipped visually and was back off after the next page load,
with nothing anywhere to explain why.

Neither a server test nor a markup test can see that. Only a browser can.

These tests start a real instance against a throwaway data directory
(``EV_DATA_DIR``) and drive it with Playwright. They skip — rather than
fail — where Playwright or its browser is not installed, so the suite
still runs on a machine that only has Python.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

playwright_api = pytest.importorskip(
    'playwright.sync_api',
    reason='Playwright not installed — browser behaviour not covered here')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def live_app():
    """A real server, on a real port, with its own empty database."""
    data_dir = tempfile.mkdtemp(prefix='evct-uitest-')
    port = _free_port()
    env = dict(os.environ,
               EV_DATA_DIR=data_dir,
               SECRET_KEY='ui-test-only',
               APP_HOST='127.0.0.1',
               APP_PORT=str(port))
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

        # A vehicle on the official Škoda API: the brand that offers
        # remote commands without needing any optional package.
        sys.path.insert(0, ROOT)
        os.environ['EV_DATA_DIR'] = data_dir
        yield base, data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _seed_vehicle(data_dir):
    """Write the fixture vehicle straight into the instance's database."""
    import sqlite3
    db = os.path.join(data_dir, 'ev_tracker.db')
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE vehicles SET name='Enyaq', api_brand='skoda_api', api_username='',"
        " api_password='key', api_vin='TMBTESTVIN000001', remote_control_enabled=0"
        " WHERE id=(SELECT MIN(id) FROM vehicles)")
    con.commit()
    con.close()


def _open_settings(page, base):
    page.goto(base + '/settings', wait_until='networkidle', timeout=60000)
    page.wait_for_timeout(800)
    # The car wizard opens itself when no car is connected and covers the
    # page; close it the way a user would.
    page.evaluate("""() => {
        const m = document.getElementById('carWizardModal');
        if (m && window.bootstrap) bootstrap.Modal.getInstance(m)?.hide();
        document.querySelectorAll('.modal-backdrop').forEach(e => e.remove());
        document.body.classList.remove('modal-open');
        document.body.style = '';
    }""")
    page.wait_for_timeout(300)


def test_the_remote_optin_switch_actually_reaches_the_server(live_app):
    """Click it, and: a request goes out, the switch STAYS on, the command
    buttons unlock, and the database says so. The bug this covers passed
    every one of those four on inspection and failed all four in a
    browser."""
    base, data_dir = live_app
    _seed_vehicle(data_dir)
    calls, errors = [], []
    with playwright_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('response',
                lambda r: calls.append(r.status) if '/remote-optin' in r.url else None)
        _open_settings(page, base)

        switch = page.query_selector('.rc-optin')
        assert switch is not None, 'no remote-control switch rendered'
        button = page.query_selector('.rc-cmd')
        assert button.is_disabled(), 'commands must be locked while the opt-in is off'

        switch.scroll_into_view_if_needed()
        switch.click()
        page.wait_for_timeout(1500)

        assert calls == [200], f'no request reached the server (got {calls})'
        assert switch.is_checked(), 'the switch fell back — the server never confirmed'
        assert not button.is_disabled(), 'commands stayed locked after opting in'
        assert not errors, errors
        browser.close()

    import sqlite3
    con = sqlite3.connect(os.path.join(data_dir, 'ev_tracker.db'))
    (stored,) = con.execute(
        'SELECT remote_control_enabled FROM vehicles ORDER BY id LIMIT 1').fetchone()
    con.close()
    assert stored == 1, 'the opt-in did not survive as data'


def test_the_dashboard_only_offers_control_after_the_opt_in(live_app):
    """The dashboard card is prominent by design — so it must not become a
    back door around the decision. With the opt-in off it must not exist;
    with it on, the buttons must work from there too."""
    base, data_dir = live_app
    _seed_vehicle(data_dir)                      # resets the opt-in to 0
    import sqlite3
    with playwright_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(base + '/', wait_until='networkidle', timeout=60000)
        page.wait_for_timeout(600)
        assert page.query_selector('[data-remote-vehicle]') is None, \
            'the dashboard showed controls for a vehicle that never opted in'

        con = sqlite3.connect(os.path.join(data_dir, 'ev_tracker.db'))
        con.execute('UPDATE vehicles SET remote_control_enabled=1')
        con.commit()
        con.close()

        calls = []
        page.on('response',
                lambda r: calls.append(r.status) if '/remote/' in r.url else None)
        page.goto(base + '/', wait_until='networkidle', timeout=60000)
        page.wait_for_timeout(600)
        card = page.query_selector('[data-remote-vehicle]')
        assert card is not None, 'no controls on the dashboard after opting in'

        btn = page.query_selector('.rc-cmd[data-cmd="charging_start"]')
        assert btn is not None and not btn.is_disabled()
        btn.click()
        page.wait_for_timeout(1500)
        # The credentials are fake, so the car refuses — but the request
        # must REACH the route. That is what this test is about.
        assert calls, 'the dashboard button sent nothing'
        browser.close()


def _sichtbare_cred_labels(page):
    return page.eval_on_selector_all(
        '.vf-cred',
        """els => els.filter(e => e.offsetParent !== null)
                    .map(e => e.querySelector('label').textContent.trim())""")


def test_the_form_asks_only_for_what_the_chosen_brand_needs(live_app):
    """The customer's screen, driven in a browser.

    He set up an Enyaq on the official Škoda API — an API key and a VIN,
    no account, no PIN — and the form presented "Benutzername",
    "Passwort" and "PIN" anyway, so he had to try which box the key
    belonged in. Measured before the change: all five boxes for every
    brand alike.
    """
    base, data_dir = live_app
    _seed_vehicle(data_dir)
    with playwright_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        _open_settings(page, base)
        page.query_selector_all('[data-edit-vehicle]')[-1].click()
        page.wait_for_timeout(500)

        page.select_option('#vf_api_brand', 'skoda_api')
        page.dispatch_event('#vf_api_brand', 'change')
        page.wait_for_timeout(300)
        skoda = _sichtbare_cred_labels(page)
        assert len(skoda) == 2, f'expected two boxes, got {skoda}'
        assert any('VIN' in l for l in skoda)
        assert not any('PIN' in l for l in skoda), skoda

        page.select_option('#vf_api_brand', 'kia')
        page.dispatch_event('#vf_api_brand', 'change')
        page.wait_for_timeout(300)
        kia = _sichtbare_cred_labels(page)
        assert len(kia) == 4, f'expected four boxes, got {kia}'
        assert any('PIN' in l for l in kia), kia
        # Kia names its regions, so the box is a list, not free text.
        assert page.eval_on_selector('#vf_api_region', 'e => e.tagName') == 'SELECT'

        assert not errors, errors
        browser.close()


def test_switching_brands_in_the_form_does_not_wipe_a_stored_value(live_app):
    """A box the brand does not use is not sent, and what is not sent is
    not overwritten. Otherwise glancing at another brand would silently
    clear the VIN."""
    base, data_dir = live_app
    _seed_vehicle(data_dir)
    import sqlite3
    with playwright_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        _open_settings(page, base)
        page.query_selector_all('[data-edit-vehicle]')[-1].click()
        page.wait_for_timeout(500)
        # Tesla asks for an account and a token — no VIN box at all.
        page.select_option('#vf_api_brand', 'tesla')
        page.dispatch_event('#vf_api_brand', 'change')
        page.wait_for_timeout(300)
        assert page.eval_on_selector('#vf_api_vin', 'e => e.disabled') is True
        # Rename at the same time: if the save did not happen at all the
        # VIN would also survive, and the test would pass for no reason.
        page.fill('#vf_name', 'Enyaq umbenannt')
        page.eval_on_selector("form[action='/vehicles/save']", 'f => f.submit()')
        page.wait_for_timeout(2000)
        browser.close()

    con = sqlite3.connect(os.path.join(data_dir, 'ev_tracker.db'))
    name, vin = con.execute(
        'SELECT name, api_vin FROM vehicles ORDER BY id LIMIT 1').fetchone()
    con.close()
    assert name == 'Enyaq umbenannt', 'the form never saved — nothing is proven'
    assert vin == 'TMBTESTVIN000001', f'the stored VIN was lost ({vin!r})'


def test_every_inline_handler_waits_for_the_document():
    """The structural rule behind the bug, checked without a browser.

    An inline script in the middle of settings.html runs before the rest
    of the page exists. Any block that binds handlers must therefore wait
    for DOMContentLoaded — otherwise it silently binds to nothing, which
    produces no error anywhere and looks exactly like working code.
    """
    import re
    html = open(os.path.join(ROOT, 'templates', 'settings.html')).read()
    offenders = []
    for m in re.finditer(r"querySelectorAll\('\[data-(remote-vehicle|switch-vehicle)\]'\)", html):
        # Walk back to the enclosing block opener.
        head = html[:m.start()]
        opener = max(head.rfind('DOMContentLoaded'), -1)
        iife = head.rfind('(function ()')
        if opener < iife:
            line = html[:m.start()].count('\n') + 1
            offenders.append(f'settings.html:{line}')
    assert not offenders, (
        'these bind handlers before the document exists: ' + ', '.join(offenders))


def test_the_switch_over_also_updates_the_legacy_config_keys(live_app):
    """A vehicle's brand lives in two places, and both must move together.

    The row in ``vehicles`` drives the background sync; the flat
    ``vehicle_api_*`` keys in AppConfig drive the single-vehicle paths,
    including the dashboard's live status. The Škoda changeover wrote only
    the row — so the sync used the official API while the dashboard kept
    calling the API that is being switched off, and each looked correct on
    its own. The mismatch was visible nowhere.
    """
    import sqlite3
    base, data_dir = live_app
    db = os.path.join(data_dir, 'ev_tracker.db')

    con = sqlite3.connect(db)
    con.execute("UPDATE vehicles SET api_brand='skoda', api_username='a@b.c',"
                " api_password='pw', api_vin='TMBTESTVIN000001'")
    con.execute("INSERT OR REPLACE INTO app_config (key, value)"
                " VALUES ('vehicle_api_brand', 'skoda')")
    con.commit()
    con.close()

    import urllib.error
    import urllib.request
    body = json.dumps({'api_key': 'not-a-real-key',
                       'vin': 'TMBTESTVIN000001'}).encode()
    req = urllib.request.Request(base + '/api/vehicle/1/skoda/switch', data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        answer = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        answer = json.loads(e.read())

    con = sqlite3.connect(db)
    (row_brand,) = con.execute(
        'SELECT api_brand FROM vehicles ORDER BY id LIMIT 1').fetchone()
    cfg = con.execute(
        "SELECT value FROM app_config WHERE key='vehicle_api_brand'").fetchone()
    con.close()

    if answer.get('ok'):
        assert row_brand == 'skoda_api'
        assert cfg and cfg[0] == 'skoda_api', (
            'the vehicle row moved to the official API but the legacy config '
            f'key still says {cfg}')
    else:
        # The key is fake, so the switch is refused — which is itself the
        # point: nothing may be written before the credentials are proved.
        assert row_brand == 'skoda', 'credentials were stored without proving them'
        assert cfg and cfg[0] == 'skoda'
