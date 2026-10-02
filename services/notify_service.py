"""Push notifications — ntfy and Telegram, side by side.

WHY TWO CHANNELS
----------------
ntfy needs no account at all: pick an unguessable topic, install the app,
done. That is the lowest possible hurdle for someone who just installed
this tracker and wants a push when a charge finishes.

Telegram needs a bot (one chat with @BotFather) but gives a real inbox:
messages stay, you can search them, and they arrive on every device you
are already signed in on. Offering both is cheaper than arguing about
which is better — the install picks what it already uses.

WHAT CHANGED, AND WHY THE OLD KEYS ARE STILL HERE
-------------------------------------------------
This module began as "send a push when the VM comes up waiting for a LUKS
passphrase", which is why the config lives in a file outside the encrypted
volume rather than in the database: the unlock helper runs *before* the
database is reachable. That reason is going away, but the file is not —
every existing install has one, and the three original keys (``enabled``,
``topic``, ``server``) therefore keep their names and meaning. New keys are
added around them; an older reader of the file keeps working.

WHAT GETS SENT
--------------
Three kinds of event, each switchable on its own:

``charge``   a charge was detected, finished, or filed
``trouble``  something needs attention — the car's API stopped answering,
             the sync loop hung, a charge could not be filed cleanly
``update``   a new version is available, or one was installed

Location: /var/lib/ev-tracker/notify.json when that directory is writable
(the native install), otherwise DATA_DIR/notify.json — which is where a
container lands, inside the mounted volume.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import DATA_DIR

PRIMARY_PATH = Path('/var/lib/ev-tracker/notify.json')
FALLBACK_PATH = Path(DATA_DIR) / 'notify.json'

#: The event kinds a caller may raise. Anything else is refused rather
#: than silently delivered — a typo in a call site would otherwise send
#: a notification the user cannot switch off anywhere in the UI.
EVENTS = ('charge', 'trouble', 'update')

DEFAULTS = {
    # ── ntfy (original keys, unchanged meaning) ──────────────────
    'enabled': False,
    'topic': '',
    'server': 'https://ntfy.sh',
    # ── Telegram ─────────────────────────────────────────────────
    'telegram_enabled': False,
    'telegram_token': '',
    'telegram_chat_id': '',
    # ── What to send ─────────────────────────────────────────────
    # On by default: a channel nobody switched on sends nothing anyway,
    # so the safe default here is "everything the user asked for by
    # enabling a channel at all".
    'events': {'charge': True, 'trouble': True, 'update': True},
}

#: Short enough that a dead push server cannot stall a background loop,
#: long enough for a phone network round trip.
TIMEOUT_S = 6


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
    """The stored config, with every missing key filled from DEFAULTS."""
    path = _config_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    cfg = {**DEFAULTS, **(data if isinstance(data, dict) else {})}
    # `events` is a nested dict, so a plain merge would drop the defaults
    # for any kind the stored file predates.
    stored_events = data.get('events') if isinstance(data, dict) else None
    cfg['events'] = {
        **DEFAULTS['events'],
        **(stored_events if isinstance(stored_events, dict) else {}),
    }
    return cfg


def save(**fields) -> Path:
    """Merge ``fields`` into the stored config and write it atomically.

    🔴 Merging, not replacing. The settings page posts the notification
    card only; a replacing write would wipe whatever a future card adds
    next to it. It also means a caller may leave the Telegram token out
    to keep the stored one — see ``api_settings_notify``.
    """
    path = _config_path()
    cfg = load()

    for key in ('enabled', 'telegram_enabled'):
        if key in fields:
            cfg[key] = bool(fields[key])
    for key in ('topic', 'telegram_token', 'telegram_chat_id'):
        if key in fields and fields[key] is not None:
            cfg[key] = str(fields[key]).strip()
    if 'server' in fields:
        cfg['server'] = _normalize_server(fields['server'])
    if isinstance(fields.get('events'), dict):
        cfg['events'] = {
            kind: bool(fields['events'].get(kind, cfg['events'].get(kind, True)))
            for kind in EVENTS
        }

    tmp = path.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        # The file carries a bot token. Nobody but the service user needs
        # to read it.
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _normalize_server(server: str) -> str:
    s = (server or '').strip() or DEFAULTS['server']
    if not s.startswith(('http://', 'https://')):
        s = 'https://' + s
    return s.rstrip('/')


def _post(url: str, data: bytes, headers: dict) -> tuple[bool, str]:
    """One POST, every failure turned into (False, reason).

    🔴 Never raises. Every caller is a background loop that must keep
    running when a push server is down.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(2048).decode('utf-8', 'replace')
            if 200 <= resp.status < 300:
                return True, body
            return False, f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        # Telegram puts the useful half of the story in the body
        # ("chat not found", "Unauthorized") — an HTTP number alone
        # sends the user looking in the wrong place.
        try:
            detail = json.loads(e.read(2048).decode('utf-8', 'replace'))
            desc = detail.get('description')
            if desc:
                return False, f'HTTP {e.code}: {desc}'
        except Exception:
            pass
        return False, f'HTTP {e.code}'
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def send(topic: str, server: str, message: str, title: str | None = None) -> tuple[bool, str]:
    """Send one message via ntfy. Signature unchanged since v2.x."""
    topic = (topic or '').strip()
    if not topic:
        return False, 'topic_missing'
    headers = {'Content-Type': 'text/plain; charset=utf-8'}
    if title:
        # ntfy reads the title from a header, and a header may not carry
        # anything outside Latin-1 — an umlaut in the title would raise
        # before a single byte went out.
        headers['Title'] = title.encode('utf-8').decode('latin-1', 'ignore')
    ok, info = _post(
        f"{_normalize_server(server)}/{urllib.parse.quote(topic, safe='')}",
        message.encode('utf-8'),
        headers,
    )
    return ok, ('ok' if ok else info)


def send_telegram(token: str, chat_id: str, message: str,
                  title: str | None = None) -> tuple[bool, str]:
    """Send one message via a Telegram bot."""
    token = (token or '').strip()
    chat_id = (chat_id or '').strip()
    if not token:
        return False, 'token_missing'
    if not chat_id:
        return False, 'chat_id_missing'
    text = f"{title}\n{message}" if title else message
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': text,
        # No parse_mode: the text is whatever the app wrote, and Telegram
        # rejects the whole message when an unescaped underscore in a
        # vehicle name looks like markup to it.
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    ok, info = _post(
        f'https://api.telegram.org/bot{urllib.parse.quote(token, safe="")}/sendMessage',
        payload,
        {'Content-Type': 'application/x-www-form-urlencoded'},
    )
    return ok, ('ok' if ok else info)


#: Where ``notify_once`` remembers what it has already said. Next to the
#: config, not inside it: this is state the user never edits, and mixing
#: the two would mean a settings save could lose a suppression window.
STATE_PATH = Path(DATA_DIR) / 'notify_state.json'

#: Default silence after a trouble report. Long enough that a car offline
#: overnight produces one message, not forty.
REPEAT_AFTER_S = 6 * 3600


def _state() -> dict:
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _state_write(data: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        # Losing the memo means one duplicate message, which is a far
        # smaller problem than an exception in a background loop.
        pass


def notify_once(event: str, key: str, message: str, title: str | None = None,
                repeat_after_s: int = REPEAT_AFTER_S) -> dict:
    """Raise an event at most once per ``key`` within ``repeat_after_s``.

    🔴 A watchdog that reports every tick is a watchdog nobody reads. The
    car being unreachable is one piece of news, not one every ten
    minutes — so the same ``key`` stays silent until the window passes.
    Call ``notify_clear(key)`` when the condition goes away, so the next
    occurrence is reported immediately instead of waiting out the window.
    """
    import time
    st = _state()
    letzte = st.get(key, 0)
    jetzt = int(time.time())
    try:
        letzte = int(letzte)
    except (TypeError, ValueError):
        letzte = 0
    if jetzt - letzte < max(0, int(repeat_after_s)):
        return {'event': event, 'sent': [], 'failed': {}, 'skipped': ['too_soon']}
    ergebnis = notify(event, message, title)
    # Only remember it when something actually went out — otherwise a
    # dead push server would buy itself six hours of silence.
    if ergebnis.get('sent'):
        st[key] = jetzt
        _state_write(st)
    return ergebnis


def notify_clear(key: str) -> None:
    """Forget one ``notify_once`` key — call this when a problem is over."""
    st = _state()
    if key in st:
        st.pop(key, None)
        _state_write(st)


def notify(event: str, message: str, title: str | None = None) -> dict:
    """Raise one event on every channel the user switched on.

    Returns what happened per channel so a caller can log it; it never
    raises and never reports a failure for a channel that is simply off.
    """
    result = {'event': event, 'sent': [], 'failed': {}, 'skipped': []}
    if event not in EVENTS:
        result['failed']['*'] = f'unknown event: {event}'
        return result
    try:
        cfg = load()
    except Exception as e:  # pragma: no cover - defensive
        result['failed']['*'] = str(e)
        return result

    if not cfg['events'].get(event, True):
        result['skipped'].append('event_off')
        return result

    if cfg.get('enabled') and cfg.get('topic'):
        ok, info = send(cfg['topic'], cfg['server'], message, title)
        (result['sent'].append('ntfy') if ok
         else result['failed'].__setitem__('ntfy', info))
    if cfg.get('telegram_enabled') and cfg.get('telegram_token'):
        ok, info = send_telegram(cfg['telegram_token'], cfg['telegram_chat_id'],
                                 message, title)
        (result['sent'].append('telegram') if ok
         else result['failed'].__setitem__('telegram', info))
    if not result['sent'] and not result['failed']:
        result['skipped'].append('no_channel')
    return result
