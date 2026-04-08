"""Client-side Kia/Hyundai OAuth token flow.

Instead of Selenium on the server, this generates URLs for the user
to complete the OAuth flow in their own browser. Works on headless servers.

Based on: https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api/wiki/Kia-Europe-Login-Flow
"""
import logging
import re

import requests as req

logger = logging.getLogger(__name__)

BRAND_CONFIG = {
    'kia': {
        'client_id': 'fdc85c00-0a2f-4c64-bcb4-2cfb1500730a',
        'base_url': 'https://idpconnect-eu.kia.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peukiaidm-online-sales',
        'login_redirect': 'https://www.kia.com/api/bin/oneid/login',
        'login_state': 'aHR0cHM6Ly93d3cua2lhLmNvbS9kZS8=_default',
        'redirect_final': 'https://prd.eu-ccapi.kia.com:8080/api/v1/user/oauth2/redirect',
    },
    'hyundai': {
        'client_id': '6d477c38-3ca4-4cf3-9557-2a1929a94654',
        'base_url': 'https://idpconnect-eu.hyundai.com/auth/api/v2/user/oauth2/',
        'login_client_id': 'peuhyundaiidm-online-sales',
        'login_redirect': 'https://www.hyundai.com/api/bin/oneid/login',
        'login_state': 'aHR0cHM6Ly93d3cuaHl1bmRhaS5jb20vZGUv_default',
        'redirect_final': 'https://prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/redirect',
    },
}


def get_urls(brand_key):
    """Return the login and authorize URLs for the given brand."""
    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        return None

    login_url = (
        f"{cfg['base_url']}authorize?ui_locales=de&scope=openid%20profile%20email%20phone"
        f"&response_type=code&client_id={cfg['login_client_id']}"
        f"&redirect_uri={cfg['login_redirect']}"
        f"&state={cfg['login_state']}"
    )
    authorize_url = (
        f"{cfg['base_url']}authorize?response_type=code&client_id={cfg['client_id']}"
        f"&redirect_uri={cfg['redirect_final']}&lang=de&state=ccsp"
    )
    return {
        'login_url': login_url,
        'authorize_url': authorize_url,
    }


def exchange_code(brand_key, redirect_url_or_code):
    """Extract auth code from redirect URL and exchange for refresh token."""
    cfg = BRAND_CONFIG.get(brand_key)
    if not cfg:
        return {'error': 'Unbekannte Marke'}

    # Accept either a full URL or just the code
    code = redirect_url_or_code.strip()
    match = re.search(r'code=([^&]+)', code)
    if match:
        code = match.group(1)

    if not code:
        return {'error': 'Kein Auth-Code gefunden'}

    token_url = f"{cfg['base_url']}token"
    try:
        resp = req.post(token_url, data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': cfg['redirect_final'],
            'client_id': cfg['client_id'],
            'client_secret': 'secret',
        }, timeout=30)

        if resp.status_code == 200:
            tokens = resp.json()
            refresh_token = tokens.get('refresh_token')
            if refresh_token:
                return {'token': refresh_token}
            return {'error': 'Kein refresh_token in Antwort'}

        return {'error': f'Token-Austausch fehlgeschlagen (HTTP {resp.status_code})'}

    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return {'error': str(e)}
