"""Fetch Kia/Hyundai refresh token via Selenium with mobile user-agent.

Based on: https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/wiki/Kia-Europe-Login-Flow
Key insight: Kia requires a MOBILE user-agent string, otherwise login is blocked.
"""
import logging
import re
import threading

logger = logging.getLogger(__name__)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 4.1.1; Galaxy Nexus Build/JRO03C) "
    "AppleWebKit/535.19 (KHTML, like Gecko) Chrome/18.0.1025.166 "
    "Mobile Safari/535.19_CCS_APP_AOS"
)

BRAND_CONFIG = {
    'kia': {
        'client_id': 'fdc85c00-0a2f-4c64-bcb4-2cfb1500730a',
        'base_url': 'https://idpconnect-eu.kia.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peukiaidm-online-sales',
        'login_redirect': 'https://www.kia.com/api/bin/oneid/login',
        'login_state': 'aHR0cHM6Ly93d3cua2lhLmNvbS9kZS8=_default',
        'redirect_final': 'https://prd.eu-ccapi.kia.com:8080/api/v1/user/oauth2/redirect',
        'success_selector': "a[class='logout user']",
    },
    'hyundai': {
        'client_id': '6d477c38-3ca4-4cf3-9557-2a1929a94654',
        'base_url': 'https://idpconnect-eu.hyundai.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peuhyundaiidm-online-sales',
        'login_redirect': 'https://www.hyundai.com/api/bin/oneid/login',
        'login_state': 'aHR0cHM6Ly93d3cuaHl1bmRhaS5jb20vZGUv_default',
        'redirect_final': 'https://prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/redirect',
        'success_selector': "a[class='logout user'], .logged-in, [data-logged-in]",
    },
}

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

    login_url = (
        f"{cfg['base_url']}authorize?ui_locales=de&scope=openid%20profile%20email%20phone"
        f"&response_type=code&client_id={cfg['login_client_id']}"
        f"&redirect_uri={cfg['login_redirect']}"
        f"&state={cfg['login_state']}"
    )
    redirect_url = (
        f"{cfg['base_url']}authorize?response_type=code&client_id={cfg['client_id']}"
        f"&redirect_uri={cfg['redirect_final']}&lang=de&state=ccsp"
    )
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
        options.add_argument(f'user-agent={MOBILE_USER_AGENT}')
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

            # Wait for successful login (max 5 min)
            wait = WebDriverWait(driver, 300)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, cfg['success_selector'])))

            _fetch_state['status'] = 'Login erkannt! Token wird abgerufen...'

            # Navigate to CCSP authorize to get the auth code
            driver.get(redirect_url)
            import time
            time.sleep(3)

            current_url = driver.current_url
            match = re.search(r'code=([^&]+)', current_url)
            if not match:
                _fetch_state.update(running=False, error='Kein Auth-Code in Redirect gefunden.')
                return

            code = match.group(1)

            # Exchange code for tokens
            import requests as req
            resp = req.post(token_url, data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': cfg['redirect_final'],
                'client_id': cfg['client_id'],
                'client_secret': 'secret',
            })

            if resp.status_code == 200:
                tokens = resp.json()
                refresh_token = tokens.get('refresh_token')
                if refresh_token:
                    _fetch_state.update(running=False, status='Token erfolgreich!', token=refresh_token)
                    return

            _fetch_state.update(running=False, error=f'Token-Austausch fehlgeschlagen ({resp.status_code})')

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    except Exception as e:
        _fetch_state.update(running=False, error=str(e))


def start_fetch(brand_key):
    if _fetch_state['running']:
        return False
    t = threading.Thread(target=_do_fetch, args=(brand_key,), daemon=True)
    t.start()
    return True


def cancel_fetch():
    _fetch_state['running'] = False
