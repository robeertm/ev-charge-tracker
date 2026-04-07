"""Fetch Kia/Hyundai refresh token via Selenium with mobile user-agent.

Based on: https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/wiki/Kia-Europe-Login-Flow
Key insight: Kia requires a MOBILE user-agent string, otherwise login is blocked.
"""
import logging
import re
import threading

logger = logging.getLogger(__name__)

KIA_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 4.1.1; Galaxy Nexus Build/JRO03C) "
    "AppleWebKit/535.19 (KHTML, like Gecko) Chrome/18.0.1025.166 "
    "Mobile Safari/535.19_CCS_APP_AOS"
)

CLIENT_ID = "fdc85c00-0a2f-4c64-bcb4-2cfb1500730a"
BASE_URL = "https://idpconnect-eu.kia.com/auth/api/v2/user/oauth2/"
LOGIN_URL = (
    f"{BASE_URL}authorize?ui_locales=de&scope=openid%20profile%20email%20phone"
    f"&response_type=code&client_id=peukiaidm-online-sales"
    f"&redirect_uri=https://www.kia.com/api/bin/oneid/login"
    f"&state=aHR0cHM6Ly93d3cua2lhLmNvbS9kZS8=_default"
)
REDIRECT_URL_FINAL = "https://prd.eu-ccapi.kia.com:8080/api/v1/user/oauth2/redirect"
REDIRECT_URL = (
    f"{BASE_URL}authorize?response_type=code&client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URL_FINAL}&lang=de&state=ccsp"
)
TOKEN_URL = f"{BASE_URL}token"

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

    try:
        # Auto-install selenium if missing
        try:
            from selenium import webdriver
        except ImportError:
            _fetch_state['status'] = 'Selenium wird installiert...'
            import subprocess, sys
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'selenium', 'webdriver-manager'],
                           capture_output=True, timeout=120)
            from selenium import webdriver

        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            from webdriver_manager.chrome import ChromeDriverManager
            _fetch_state['status'] = 'ChromeDriver wird vorbereitet...'
            service = Service(ChromeDriverManager().install())
        except ImportError:
            service = None

        options = webdriver.ChromeOptions()
        options.add_argument(f'user-agent={KIA_USER_AGENT}')
        options.add_argument('--window-size=420,750')

        _fetch_state['status'] = 'Browser wird gestartet...'
        driver = webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)

        try:
            _fetch_state['status'] = 'Bitte im Browser einloggen (reCAPTCHA lösen)...'
            driver.get(LOGIN_URL)

            # Wait for successful login (max 5 min) — look for logout link on kia.com
            wait = WebDriverWait(driver, 300)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[class='logout user']")))

            _fetch_state['status'] = 'Login erkannt! Token wird abgerufen...'

            # Navigate to CCSP authorize to get the auth code
            driver.get(REDIRECT_URL)
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
            resp = req.post(TOKEN_URL, data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URL_FINAL,
                'client_id': CLIENT_ID,
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
