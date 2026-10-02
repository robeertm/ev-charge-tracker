"""Tests for the two notification channels (services/notify_service.py).

Notifications used to be one thing: a push via ntfy when the VM came up
waiting for a LUKS passphrase. They are now two channels (ntfy and
Telegram) and three switchable kinds of event, and the config file every
existing install already has must keep working across that change.

What this pins:
  * the three original keys still load, and a file that predates the new
    ones gets sensible defaults rather than a KeyError,
  * ``save()`` merges instead of replacing — in particular, posting the
    settings card without a token keeps the stored token,
  * ``notify()`` sends only on enabled channels, only for enabled event
    kinds, and refuses an event name it does not know,
  * neither sender raises on a dead server or on a title with an umlaut
    (ntfy carries the title in an HTTP header, which is Latin-1 only).

Run with:
  python3 tests/test_notify_channels.py
Exit code is non-zero if any check fails.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The module resolves its config path from DATA_DIR at import time, so the
# temp directory has to be in place before the import.
_TMP = tempfile.mkdtemp(prefix='ev-notify-test-')
os.environ['EV_DATA_DIR'] = _TMP

import services.notify_service as ns  # noqa: E402

# Never let the test touch a real /var/lib/ev-tracker on the machine it
# runs on.
ns.PRIMARY_PATH = ns.Path(_TMP) / 'nonexistent-dir' / 'notify.json'
ns.FALLBACK_PATH = ns.Path(_TMP) / 'notify.json'

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def schreibe_roh(data):
    """Put a config file on disk the way an older version would have."""
    with open(ns.FALLBACK_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def lies_roh():
    with open(ns.FALLBACK_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


print("\n== defaults and backward compatibility ==")
if ns.FALLBACK_PATH.exists():
    ns.FALLBACK_PATH.unlink()
cfg = ns.load()
check(cfg['enabled'] is False and cfg['server'] == 'https://ntfy.sh',
      "a missing file yields the documented defaults")
check(set(cfg['events']) == set(ns.EVENTS),
      "every known event kind has a default")

# A file from before this change: only the three original keys.
schreibe_roh({'enabled': True, 'topic': 'alt-topic', 'server': 'https://ntfy.sh'})
cfg = ns.load()
check(cfg['topic'] == 'alt-topic' and cfg['enabled'] is True,
      "a pre-Telegram config file still loads")
check(cfg['telegram_enabled'] is False and cfg['telegram_token'] == '',
      "the new keys default instead of raising")
check(cfg['events']['charge'] is True,
      "events default to on when the file predates them")

# A file that knows only one event kind must still answer for the others.
schreibe_roh({'enabled': True, 'topic': 'x', 'events': {'charge': False}})
cfg = ns.load()
check(cfg['events']['charge'] is False and cfg['events']['update'] is True,
      "a partial events block is filled up, not replaced")


print("\n== save() merges rather than replaces ==")
schreibe_roh(dict(ns.DEFAULTS))
ns.save(enabled=True, topic='mein-topic', server='ntfy.example')
ns.save(telegram_enabled=True, telegram_token='123:AAA', telegram_chat_id='42')
cfg = ns.load()
check(cfg['topic'] == 'mein-topic',
      "saving the Telegram half keeps the ntfy half")
check(cfg['server'] == 'https://ntfy.example',
      "a server without a scheme is normalised, and survives")
check(cfg['telegram_token'] == '123:AAA' and cfg['telegram_chat_id'] == '42',
      "the Telegram fields are stored")

# The settings page never receives the token, so it cannot send it back.
ns.save(telegram_enabled=True, telegram_chat_id='99')
cfg = ns.load()
check(cfg['telegram_token'] == '123:AAA',
      "a save without a token keeps the stored one")
check(cfg['telegram_chat_id'] == '99',
      "the chat id is still updated in that same save")

ns.save(events={'charge': False, 'trouble': True, 'update': False})
cfg = ns.load()
check(cfg['events'] == {'charge': False, 'trouble': True, 'update': False},
      "event switches round-trip")


print("\n== notify() gating ==")
_gesendet = []


def _falscher_post(url, data, headers):
    _gesendet.append(url)
    return True, '{"ok":true}'


ns._post = _falscher_post

schreibe_roh({'enabled': True, 'topic': 'topic', 'server': 'https://ntfy.sh',
              'telegram_enabled': True, 'telegram_token': 'tok',
              'telegram_chat_id': '1',
              'events': {'charge': True, 'trouble': False, 'update': True}})

_gesendet.clear()
res = ns.notify('charge', 'a charge finished')
check(sorted(res['sent']) == ['ntfy', 'telegram'] and len(_gesendet) == 2,
      "an enabled event goes out on both enabled channels")

_gesendet.clear()
res = ns.notify('trouble', 'something broke')
check(res['sent'] == [] and 'event_off' in res['skipped'] and not _gesendet,
      "a switched-off event sends nothing at all")

_gesendet.clear()
res = ns.notify('nonsense', 'should not go out')
check(res['sent'] == [] and '*' in res['failed'] and not _gesendet,
      "an unknown event kind is refused, not delivered")

schreibe_roh({'enabled': False, 'telegram_enabled': False})
_gesendet.clear()
res = ns.notify('charge', 'nobody listening')
check(res['sent'] == [] and 'no_channel' in res['skipped'] and not _gesendet,
      "with no channel enabled nothing is attempted")

# A channel switched on but left unconfigured must not be attempted either:
# an empty topic would POST to the server root.
schreibe_roh({'enabled': True, 'topic': '', 'telegram_enabled': True,
              'telegram_token': ''})
_gesendet.clear()
res = ns.notify('charge', 'half-configured')
check(not _gesendet, "an enabled but unconfigured channel is not called")


print("\n== senders refuse bad input without touching the network ==")
_versuche = []


def _post_zaehlt(url, data, headers):
    _versuche.append(url)
    return True, ''


ns._post = _post_zaehlt
_versuche.clear()
ok, info = ns.send('', 'https://ntfy.sh', 'x')
check(ok is False and info == 'topic_missing' and not _versuche,
      "ntfy without a topic fails before the request")
ok, info = ns.send_telegram('', '1', 'x')
check(ok is False and info == 'token_missing' and not _versuche,
      "Telegram without a token fails before the request")
ok, info = ns.send_telegram('tok', '', 'x')
check(ok is False and info == 'chat_id_missing' and not _versuche,
      "Telegram without a chat id fails before the request")


print("\n== an umlaut in the title must not blow up the ntfy call ==")
_kopf = {}


def _post_merkt_kopf(url, data, headers):
    _kopf.update(headers)
    return True, ''


ns._post = _post_merkt_kopf
try:
    ok, _ = ns.send('topic', 'https://ntfy.sh', 'body', title='Ladung beendet — 42 kWh')
    check(ok is True, "a title with an em dash is accepted")
    _kopf['Title'].encode('latin-1')
    check(True, "the title header survives Latin-1 encoding")
except Exception as e:  # pragma: no cover
    check(False, f"the title raised: {e}")


print("\n== notify_once keeps a watchdog from shouting ==")
ns.STATE_PATH = ns.Path(_TMP) / 'notify_state.json'
if ns.STATE_PATH.exists():
    ns.STATE_PATH.unlink()
schreibe_roh({'enabled': True, 'topic': 'topic', 'server': 'https://ntfy.sh',
              'events': {'charge': True, 'trouble': True, 'update': True}})

_gesendet.clear()
ns._post = _falscher_post
ns.notify_once('trouble', 'vehicle_api', 'car unreachable')
check(len(_gesendet) == 1, "the first occurrence is reported")

ns.notify_once('trouble', 'vehicle_api', 'car unreachable')
ns.notify_once('trouble', 'vehicle_api', 'car unreachable')
check(len(_gesendet) == 1, "repeats inside the window stay silent")

ns.notify_once('trouble', 'etwas_anderes', 'a different problem')
check(len(_gesendet) == 2, "a different key is a different piece of news")

ns.notify_clear('vehicle_api')
ns.notify_once('trouble', 'vehicle_api', 'car unreachable again')
check(len(_gesendet) == 3, "after notify_clear the next occurrence is reported")

# 🔴 A dead push server must not buy itself six hours of silence.
def _post_scheitert(url, data, headers):
    _gesendet.append(url)
    return False, 'HTTP 500'


ns.notify_clear('kaputt')
ns._post = _post_scheitert
_gesendet.clear()
ns.notify_once('trouble', 'kaputt', 'first try')
ns.notify_once('trouble', 'kaputt', 'second try')
check(len(_gesendet) == 2,
      "a failed send is not remembered as delivered")
ns._post = _falscher_post

# A switched-off event must not be remembered either — otherwise turning
# it back on would start with a stale suppression window.
schreibe_roh({'enabled': True, 'topic': 'topic',
              'events': {'charge': True, 'trouble': False, 'update': True}})
ns.notify_clear('aus')
_gesendet.clear()
ns.notify_once('trouble', 'aus', 'nobody wants this')
schreibe_roh({'enabled': True, 'topic': 'topic',
              'events': {'charge': True, 'trouble': True, 'update': True}})
ns.notify_once('trouble', 'aus', 'now they do')
check(len(_gesendet) == 1,
      "switching an event back on does not inherit a suppression window")


print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
