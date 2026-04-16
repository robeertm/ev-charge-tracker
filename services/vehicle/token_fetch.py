"""Fetch Kia/Hyundai refresh token via Selenium.

Kia and Hyundai EU use *different* OAuth flows despite looking similar:

- **Kia EU** uses the "oneid / online-sales" flow on the public kia.com
  marketing site. Client secret is literally the string "secret". Mobile
  user-agent required (desktop UA gets blocked by "use the app" page).
  See: https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/wiki/Kia-Europe-Login-Flow

- **Hyundai EU** uses the "ctb" (Connected Car Telematics Business) flow
  on ctbapi.hyundai-europe.com with a real 48-char client_secret and
  different authorize query params (needs connector_client_id, captcha=1,
  ui_locales, etc). Desktop UA preferred.
  See: https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/tree/master/Hyundai%20Token%20Solution

Both UAs must keep the `_CCS_APP_AOS` suffix — it's the token that bypasses
the "you must use the app" block.
"""
import logging
import re
import threading
import urllib.parse

logger = logging.getLogger(__name__)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 4.1.1; Galaxy Nexus Build/JRO03C) "
    "AppleWebKit/535.19 (KHTML, like Gecko) Chrome/18.0.1025.166 "
    "Mobile Safari/535.19_CCS_APP_AOS"
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36_CCS_APP_AOS"
)

BRAND_CONFIG = {
    'kia': {
        'client_id': 'fdc85c00-0a2f-4c64-bcb4-2cfb1500730a',
        'client_secret': 'secret',  # Kia really does use the literal string
        'base_url': 'https://idpconnect-eu.kia.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peukiaidm-online-sales',
        'login_redirect': 'https://www.kia.com/api/bin/oneid/login',
        'login_state': 'aHR0cHM6Ly93d3cua2lhLmNvbS9kZS8=_default',
        'redirect_final': 'https://prd.eu-ccapi.kia.com:8080/api/v1/user/oauth2/redirect',
        'success_selector': "a[class='logout user']",
        'user_agent': MOBILE_USER_AGENT,
        'flow': 'oneid',
    },
    'hyundai': {
        'client_id': '6d477c38-3ca4-4cf3-9557-2a1929a94654',
        'client_secret': 'KUy49XxPzLpLuoK0xhBC77W6VXhmtQR9iQhmIFjjoY4IpxsV',
        'base_url': 'https://idpconnect-eu.hyundai.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peuhyundaiidm-ctb',
        'login_redirect': 'https://ctbapi.hyundai-europe.com/api/auth',
        'login_state': 'EN_',
        'redirect_final': 'https://prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token',
        'success_selector': "button.mail_check, button.ctb_button",
        'user_agent': DESKTOP_USER_AGENT,
        'flow': 'ctb',
    },
}


def _build_login_url(cfg):
    """Build the per-brand authorize URL for the initial browser login."""
    if cfg['flow'] == 'ctb':
        # Hyundai CTB flow — needs connector_client_id, captcha=1, etc.
        return (
            f"{cfg['base_url']}authorize?"
            f"client_id={cfg['login_client_id']}"
            f"&redirect_uri={urllib.parse.quote(cfg['login_redirect'], safe='')}"
            f"&nonce=&state={cfg['login_state']}"
            f"&scope=openid+profile+email+phone"
            f"&response_type=code"
            f"&connector_client_id={cfg['login_client_id']}"
            f"&connector_scope=&connector_session_key=&country="
            f"&captcha=1&ui_locales=en-US&lang=en"
        )
    # Kia oneid flow — unchanged, proven working
    return (
        f"{cfg['base_url']}authorize?ui_locales=de"
        f"&scope=openid%20profile%20email%20phone"
        f"&response_type=code&client_id={cfg['login_client_id']}"
        f"&redirect_uri={cfg['login_redirect']}"
        f"&state={cfg['login_state']}"
    )

_fetch_state = {
    'running': False,
    'status': '',
    'token': None,
    'error': None,
}


def get_state():
    return dict(_fetch_state)


def _do_fetch(brand_key):
    global _fetch_state
    _fetch_state = {'running': True, 'status': 'Starte...', 'token': None, 'error': None}

    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        _fetch_state.update(running=False, error=f'Unbekannte Marke: {brand_key}')
        return

    login_url = _build_login_url(cfg)
    # Shares the same builder as the manual flow so Selenium and manual
    # paste both hit the exact same URL — and both rely on a properly
    # URL-encoded redirect_uri.
    redirect_url = get_manual_step2_url(brand_key)
    token_url = f"{cfg['base_url']}token"

    try:
        # Auto-install/upgrade selenium if missing or too old (need 4.11+ for Selenium Manager)
        try:
            import selenium
            from packaging.version import Version
            if Version(selenium.__version__) < Version('4.11.0'):
                raise ImportError('selenium too old')
            from selenium import webdriver
        except ImportError:
            _fetch_state['status'] = 'Selenium wird installiert...'
            import subprocess, sys
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade',
                            'selenium>=4.11', 'packaging'],
                           capture_output=True, timeout=180)
            from selenium import webdriver

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        options = webdriver.ChromeOptions()
        options.add_argument(f"user-agent={cfg['user_agent']}")
        options.add_argument('--window-size=420,750')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        # Debian ships the binary as /usr/bin/chromium, not /usr/bin/chrome
        import os as _os
        for _cand in ('/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'):
            if _os.path.exists(_cand):
                options.binary_location = _cand
                break

        _fetch_state['status'] = 'Browser wird gestartet...'
        # Selenium 4.11+ auto-downloads the matching chromedriver via Selenium Manager
        driver = webdriver.Chrome(options=options)

        try:
            _fetch_state['status'] = 'Bitte im Browser einloggen (reCAPTCHA lösen)...'
            driver.get(login_url)

            import time

            # ── STEP 1: wait for the login chain to land somewhere ─────
            # Three possible landing URLs we accept as "login done":
            #   (a) redirect_final host + code=  → chain auto-completed
            #       (observed on Hyundai CTB: IdP session + CCSP cookies
            #       let the browser redirect all the way through in one
            #       shot to prd.eu-ccapi.hyundai.com with code=...)
            #   (b) login_redirect host (ctbapi... or kia oneid callback)
            #       → intermediate landing, we need step 2 to get the code
            #   (c) Kia: logout-link selector on the kia.com landing page
            #       → same as (b), need step 2
            # We do NOT wait for the old button.mail_check/ctb_button
            # selectors — those are not present on upstream's post-login
            # JSON blob and were the root cause of past 5-min timeouts.
            final_host = cfg['redirect_final'].split('://', 1)[1].split('/', 1)[0]
            login_redirect_host = cfg['login_redirect'].split('://', 1)[1].split('/', 1)[0]

            def _login_landed(drv):
                url = drv.current_url or ''
                if final_host in url and 'code=' in url:
                    return 'final'
                if login_redirect_host in url and ('code=' in url or 'login_success=y' in url):
                    return 'landing'
                if cfg['flow'] == 'oneid':
                    try:
                        if drv.find_elements(By.CSS_SELECTOR, cfg['success_selector']):
                            return 'landing'
                    except Exception:
                        pass
                return False

            wait = WebDriverWait(driver, 300)  # 5 min max for user + reCAPTCHA
            try:
                landing = wait.until(_login_landed)
            except Exception as wait_exc:
                last_url = driver.current_url if driver else '(browser down)'
                raise RuntimeError(
                    f'Timeout beim Warten auf Login-Redirect. '
                    f'Letzte URL: {last_url[:300]}. '
                    f'Original: {type(wait_exc).__name__}: {str(wait_exc)[:200]}'
                )

            # ── STEP 2: if we're on the intermediate landing page, drive
            # to the CCSP authorize URL so the IdP session cookie gets
            # exchanged for a real CCSP code. Skip this when the chain
            # already auto-completed.
            current_url = driver.current_url or ''
            if landing != 'final':
                _fetch_state['status'] = 'Login erkannt, hole Auth-Code...'
                driver.get(redirect_url)
                for _ in range(15):
                    current_url = driver.current_url or ''
                    if 'code=' in current_url and final_host in current_url:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError(
                        f'Kein Redirect zur CCSP-URL nach Login. '
                        f'Letzte URL: {current_url[:300]}'
                    )
            else:
                _fetch_state['status'] = 'Auto-Chain erkannt, extrahiere Code...'

            match = re.search(r'code=([^&]+)', current_url)
            if not match:
                _fetch_state.update(running=False,
                    error=f'Kein Auth-Code in URL: {current_url[:300]}')
                return

            code = match.group(1)
            _fetch_state['status'] = 'Code extrahiert, tausche gegen Token...'

            # Both flows use redirect_final as the OAuth redirect_uri at
            # token-exchange time: for Kia it's the 2nd authorize target
            # (prd.eu-ccapi.kia.com/.../oauth2/redirect), for Hyundai CTB
            # it's the CCSP code's landing URL on prd.eu-ccapi.hyundai.com.
            # The login_redirect (ctbapi) is only for the intermediate hop
            # and we explicitly wait past it before extracting the code.
            # Kia's client_secret is literally "secret", Hyundai's is a real
            # 48-char string — both come from the brand config dict.
            import requests as req
            try:
                resp = req.post(token_url, data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': cfg['redirect_final'],
                    'client_id': cfg['client_id'],
                    'client_secret': cfg.get('client_secret', 'secret'),
                }, timeout=15)
            except Exception as post_exc:
                _fetch_state.update(running=False,
                    error=f'Token-POST fehlgeschlagen: {type(post_exc).__name__}: {str(post_exc)[:200]}')
                return

            if resp.status_code == 200:
                tokens = resp.json()
                refresh_token = tokens.get('refresh_token')
                if refresh_token:
                    _fetch_state.update(running=False,
                        status='Token erfolgreich!', token=refresh_token)
                    return
                _fetch_state.update(running=False,
                    error=f'Token-Endpoint gab 200 aber kein refresh_token: {str(tokens)[:300]}')
                return

            _fetch_state.update(running=False,
                error=f'Token-Austausch fehlgeschlagen. Status {resp.status_code}. '
                      f'Body: {resp.text[:300]}')

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"token_fetch failed:\n{tb}")
        exc_name = type(e).__name__
        msg = str(e) or exc_name
        if exc_name == 'InvalidSessionIdException' or 'invalid session id' in msg.lower():
            friendly = (
                'Browser-Session beendet. Bitte das Browserfenster in noVNC '
                'NICHT schließen während der Token geholt wird — Selenium '
                'braucht es für den kompletten Flow. Erneut versuchen. '
                'Alternativ: "Token manuell eingeben" unten benutzen.'
            )
            _fetch_state.update(running=False, error=friendly)
        else:
            _fetch_state.update(running=False, error=f'{exc_name}: {msg}')


def start_fetch(brand_key):
    if _fetch_state['running']:
        return False
    t = threading.Thread(target=_do_fetch, args=(brand_key,), daemon=True)
    t.start()
    return True


def get_manual_step2_url(brand_key: str) -> str | None:
    """Return the second-authorize URL the user visits in their browser
    to produce the final prd.eu-ccapi URL (which contains the CCSP code).

    The user's browser session from step 1 still has the IdP cookie, so
    this URL silently 302-redirects to the correct landing page without
    asking for login again. Works identically for Kia and Hyundai.

    The redirect_uri is URL-encoded here because an unescaped "https://..."
    inside a query parameter causes some strict OAuth2 servers (notably
    Hyundai's) to return 400 Bad Request when the URL is used as an
    `<a href>` click target. Selenium's `driver.get()` normalizes it
    automatically, a browser click does not."""
    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        return None
    encoded_redirect = urllib.parse.quote(cfg['redirect_final'], safe='')
    return (
        f"{cfg['base_url']}authorize?response_type=code"
        f"&client_id={cfg['client_id']}"
        f"&redirect_uri={encoded_redirect}"
        f"&lang=en&state=ccsp"
    )


def exchange_manual_url(brand_key: str, url: str) -> tuple[bool, str, str | None]:
    """Manual fallback when Selenium can't complete the flow.

    The user logs in in their own browser, copies the final URL (the one
    containing ?code=...) from the address bar, pastes it here. We extract
    the code and POST to the token endpoint — no Selenium needed.

    Returns (ok, message, refresh_token_or_None).
    """
    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        return False, f'Unbekannte Marke: {brand_key}', None

    url = (url or '').strip()

    # Reject the ctbapi URL explicitly — that's the first-stage code which
    # belongs to the login_client_id and isn't valid at our token endpoint.
    # We detect it by the host, NOT by `login_success=y` — that flag is
    # also present on the valid final URL (prd.eu-ccapi.hyundai.com).
    final_host = cfg['redirect_final'].split('://', 1)[1].split('/', 1)[0]
    login_redirect_host = cfg['login_redirect'].split('://', 1)[1].split('/', 1)[0]
    if login_redirect_host in url and final_host not in url:
        step2 = get_manual_step2_url(brand_key) or ''
        return False, (
            'Falsche URL. Das ist die Login-URL (Stufe 1), nicht die finale '
            'Token-URL (Stufe 2). Nächster Schritt: öffne diese URL im '
            'gleichen Browser (du bist noch eingeloggt, sie redirected '
            'automatisch): ' + step2 + ' — dann die Adressleiste von der '
            'Ziel-Seite kopieren und hier einfügen.'
        ), None

    match = re.search(r'code=([^&\s]+)', url)
    if not match:
        return False, 'Keine `code=` Query-Parameter in der URL gefunden.', None
    code = match.group(1)

    token_url = f"{cfg['base_url']}token"
    try:
        import requests as req
        resp = req.post(token_url, data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': cfg['redirect_final'],
            'client_id': cfg['client_id'],
            'client_secret': cfg.get('client_secret', 'secret'),
        }, timeout=15)
    except Exception as e:
        return False, f'Token-POST fehlgeschlagen: {type(e).__name__}: {str(e)[:200]}', None

    if resp.status_code == 200:
        tokens = resp.json()
        rt = tokens.get('refresh_token')
        if rt:
            # Also store it in the shared state so the UI picks it up via
            # the same polling endpoint as the Selenium path.
            _fetch_state.update(
                running=False, status='Token erfolgreich (manuell)!',
                token=rt, error=None,
            )
            return True, 'Token erfolgreich', rt
        return False, f'Token-Endpoint gab 200 aber kein refresh_token: {str(tokens)[:300]}', None
    return False, (
        f'Token-Austausch fehlgeschlagen. Status {resp.status_code}. '
        f'Body: {resp.text[:300]}'
    ), None


def cancel_fetch():
    _fetch_state['running'] = False
