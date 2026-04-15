"""Notification config for reboot alerts via ntfy.sh.

The config lives *outside* the encrypted LUKS volume so that
`ev-unlock-web` (which runs before LUKS is unlocked) can read it
and send a push when the VM comes up waiting for a passphrase.

Location: /var/lib/ev-tracker/notify.json — owned by ev-tracker so
no sudo is needed. In dev (or if that path is not writable) we fall
back to DATA_DIR/notify.json so the UI still works.
"""
import json
import os
from pathlib import Path

import urllib.request
import urllib.error

from config import DATA_DIR

PRIMARY_PATH = Path('/var/lib/ev-tracker/notify.json')
FALLBACK_PATH = Path(DATA_DIR) / 'notify.json'

DEFAULTS = {
    'enabled': False,
    'topic': '',
    'server': 'https://ntfy.sh',
}


def _config_path() -> Path:
    parent = PRIMARY_PATH.parent
    try:
        if parent.exists() and os.access(parent, os.W_OK):
            return PRIMARY_PATH
    except Exception:
        pass
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    return FALLBACK_PATH


def load() -> dict:
    path = _config_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save(enabled: bool, topic: str, server: str) -> Path:
    path = _config_path()
    payload = {
        'enabled': bool(enabled),
        'topic': (topic or '').strip(),
        'server': _normalize_server(server),
    }
    tmp = path.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o640)
    except Exception:
        pass
    return path


def _normalize_server(server: str) -> str:
    s = (server or '').strip() or DEFAULTS['server']
    if not s.startswith(('http://', 'https://')):
        s = 'https://' + s
    return s.rstrip('/')


def send(topic: str, server: str, message: str, title: str | None = None) -> tuple[bool, str]:
    topic = (topic or '').strip()
    if not topic:
        return False, 'Topic fehlt'
    url = f"{_normalize_server(server)}/{topic}"
    headers = {'Content-Type': 'text/plain; charset=utf-8'}
    if title:
        headers['Title'] = title
    req = urllib.request.Request(
        url,
        data=message.encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            if 200 <= resp.status < 300:
                return True, 'ok'
            return False, f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except urllib.error.URLError as e:
        return False, f'Netzwerkfehler: {e.reason}'
    except Exception as e:
        return False, str(e)
