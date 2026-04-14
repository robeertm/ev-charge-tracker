"""Web-UI login / password protection.

Runs *in front of* every route when the user has opted in. Credentials live
in AppConfig (hashed via Werkzeug), session lives in Flask's signed cookie.
Opt-in so existing installs don't lock out their owners after an update.

Threat model: another Tailscale user who happens to have the VM shared with
them (or anyone who knows the hostname + is on the tailnet) should not be
able to read charges/settings without knowing a password the owner chose.
This module gives that password-gate layer; Tailscale still handles the
underlying network ACL.
"""
from __future__ import annotations

import logging
import secrets

from flask import session
from werkzeug.security import check_password_hash, generate_password_hash

from models.database import AppConfig

logger = logging.getLogger(__name__)

_KEY_ENABLED = 'auth_enabled'
_KEY_USERNAME = 'auth_username'
_KEY_PW_HASH = 'auth_password_hash'
_KEY_SESSION_SECRET = 'flask_session_secret'


# ── Opt-in toggle and credential storage ─────────────────────────────

def is_auth_enabled() -> bool:
    return AppConfig.get(_KEY_ENABLED, 'false') == 'true'


def get_username() -> str:
    return AppConfig.get(_KEY_USERNAME, '') or ''


def set_credentials(username: str, password: str) -> None:
    """Store credentials and flip the enabled flag on."""
    username = (username or '').strip()
    if not username:
        raise ValueError('Benutzername darf nicht leer sein.')
    if not password or len(password) < 6:
        raise ValueError('Passwort muss mindestens 6 Zeichen haben.')
    AppConfig.set(_KEY_USERNAME, username)
    AppConfig.set(_KEY_PW_HASH, generate_password_hash(password))
    AppConfig.set(_KEY_ENABLED, 'true')
    logger.info(f"Web-UI auth enabled for user '{username}'")


def disable_auth() -> None:
    AppConfig.set(_KEY_ENABLED, 'false')
    logger.info("Web-UI auth disabled")


def verify_credentials(username: str, password: str) -> bool:
    stored_user = get_username()
    stored_hash = AppConfig.get(_KEY_PW_HASH, '') or ''
    if not stored_user or not stored_hash:
        return False
    if username.strip() != stored_user:
        return False
    try:
        return check_password_hash(stored_hash, password or '')
    except Exception:
        return False


# ── Session helpers ───────────────────────────────────────────────────

def is_logged_in() -> bool:
    return bool(session.get('authed_user'))


def login_user(username: str) -> None:
    session['authed_user'] = username
    session.permanent = True


def logout_user() -> None:
    session.pop('authed_user', None)


# ── Stable per-install session secret ────────────────────────────────

def get_or_create_session_secret() -> str:
    """Return a per-install random secret for signing Flask session cookies.

    Lazily generates and stores a 32-byte hex secret in AppConfig on first
    call. This ensures sessions survive app restarts without being tied to
    a hardcoded default SECRET_KEY or requiring the user to set an env var.
    """
    stored = AppConfig.get(_KEY_SESSION_SECRET, '') or ''
    if stored and len(stored) >= 32:
        return stored
    new_secret = secrets.token_hex(32)
    AppConfig.set(_KEY_SESSION_SECRET, new_secret)
    logger.info("Generated new Flask session secret")
    return new_secret
