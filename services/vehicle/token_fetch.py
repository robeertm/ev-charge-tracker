"""Fetch Kia/Hyundai refresh token via browser-based OAuth flow.

Opens a Chrome window for the user to log in manually (reCAPTCHA).
After login, captures the authorization code from the redirect URL
and exchanges it for a refresh_token.
"""
import logging
import threading
import time
import requests as req

logger = logging.getLogger(__name__)

# OAuth parameters (EU)
BRAND_CONFIG = {
    'kia': {
        'auth_host': 'idpconnect-eu.kia.com',
        'api_host': 'prd.eu-ccapi.kia.com',
    },
    'hyundai': {
        'auth_host': 'idpconnect-eu.hyundai.com',
        'api_host': 'prd.eu-ccapi.hyundai.com',
    },
}

CLIENT_ID = '6d477c38-3ca4-4cf3-9557-2a1929a94654'
CLIENT_SECRET = 'KUy49XxPzLpLuoK0xhBC77W6VXhmtQR9iQhmIFjjoY4IpxsV'

# State for tracking the fetch process
_fetch_state = {
    'running': False,
    'status': '',
    'token': None,
    'error': None,
}


def get_state():
    return dict(_fetch_state)


def _do_fetch(brand_key):
    """Run the Selenium-based token fetch in a background thread."""
    global _fetch_state
    _fetch_state = {'running': True, 'status': 'Browser wird gestartet...', 'token': None, 'error': None}

    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        _fetch_state.update(running=False, error=f'Unbekannte Marke: {brand_key}')
        return

    auth_url = (
        f"https://{cfg['auth_host']}/auth/api/v2/user/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri=https://{cfg['api_host']}:8080/api/v1/user/oauth2/redirect"
        f"&response_type=code"
        f"&state=test"
    )
    redirect_prefix = f"https://{cfg['api_host']}:8080/api/v1/user/oauth2/redirect"
    token_url = f"https://{cfg['auth_host']}/auth/api/v2/user/oauth2/token"

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        # Try webdriver-manager, fall back to system Chrome
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        except ImportError:
            service = None

        options = webdriver.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=500,700')

        if service:
            driver = webdriver.Chrome(service=service, options=options)
        else:
            driver = webdriver.Chrome(options=options)

        _fetch_state['status'] = 'Bitte im Browser einloggen...'
        driver.get(auth_url)

        # Wait for redirect with auth code (max 5 minutes)
        auth_code = None
        for _ in range(300):
            if not _fetch_state['running']:
                driver.quit()
                return
            try:
                current_url = driver.current_url
                if redirect_prefix in current_url and 'code=' in current_url:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(current_url)
                    params = parse_qs(parsed.query)
                    auth_code = params.get('code', [None])[0]
                    break
            except Exception:
                pass
            time.sleep(1)

        driver.quit()

        if not auth_code:
            _fetch_state.update(running=False, error='Timeout — kein Login erkannt (5 Min).')
            return

        # Exchange code for tokens
        _fetch_state['status'] = 'Token wird abgerufen...'
        resp = req.post(token_url, data={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'code': auth_code,
            'grant_type': 'authorization_code',
            'redirect_uri': f"https://{cfg['api_host']}:8080/api/v1/user/oauth2/redirect",
        })

        if resp.status_code == 200:
            data = resp.json()
            refresh_token = data.get('refresh_token')
            if refresh_token:
                _fetch_state.update(running=False, status='Token erfolgreich!', token=refresh_token)
                return

        _fetch_state.update(running=False, error=f'Token-Austausch fehlgeschlagen: {resp.status_code} {resp.text[:200]}')

    except ImportError:
        _fetch_state.update(running=False, error='selenium nicht installiert. Bitte erst installieren.')
    except Exception as e:
        _fetch_state.update(running=False, error=str(e))


def start_fetch(brand_key):
    """Start the token fetch process in a background thread."""
    if _fetch_state['running']:
        return False
    t = threading.Thread(target=_do_fetch, args=(brand_key,), daemon=True)
    t.start()
    return True


def cancel_fetch():
    """Cancel a running fetch."""
    _fetch_state['running'] = False
