# -*- coding: utf-8 -*-
"""Hold updates on this one installation, until a named condition is met.

Three gates already stand between a click on "install now" and a file
swap, and each of them answers a question about *this moment*: is a car
charging (``updater._vehicle_is_charging_from_sqlite``), and do the
files even live where a swap would survive (``updater.updates_by_image``).

This one answers a different kind of question — one only the operator
can answer: *this particular machine must not move to a newer version
yet.* A host part-way through a migration is the case it was written
for: the new release is correct for every other install and wrong for
this one, and "wrong" here means a user clicks a button and their
system stops working.

🔑 There is deliberately no way to lift a hold from the web UI. The
whole point is that it survives somebody pressing the button, and a
switch next to the button is not a hold — it is a two-click update.
Lifting it takes filesystem access (delete the file) or a change to the
compose file (drop the variable), which is exactly the person who is
allowed to decide.

Where the hold is recorded
--------------------------
``UPDATE_HOLD.json`` next to ``app.py`` — the application directory,
**not** ``data/``. Two reasons, both learned the hard way:

* A LUKS install keeps ``data/`` on an encrypted volume. While that
  volume is locked the directory is not there at all, so a hold written
  into it would be invisible at exactly the moment the machine is in an
  odd state.
* ``data/`` is what travels when a host is migrated. The app directory
  is what stays with the installation.

The name is in all three update exclude lists, so an update that does
run (after the hold is lifted, or past ``until``) cannot delete it.

Conditions
----------
``until: "forever"``
    Only removing the file lifts it. The default, and the right answer
    whenever the reason is not something software can observe.

``until: "container"``
    Lifts itself once the app really is running in a container
    (``runtime_env.in_container``). For a VM being replaced by a
    container this matters: the data directory is copied over to the
    new container, and if the hold lived there it would arrive with it
    and freeze the thing it was meant to wait for. Recording the
    *condition* rather than the verdict means the file can travel
    anywhere and still be right.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

HOLD_FILE = 'UPDATE_HOLD.json'

#: Reason string in the environment instead of a file — the natural
#: shape for a container, where the compose file is the place things are
#: configured and the filesystem is thrown away on every recreate.
ENV_REASON = 'EV_UPDATE_HOLD'
ENV_UNTIL = 'EV_UPDATE_HOLD_UNTIL'

CONDITIONS = ('forever', 'container')

_FALLBACK_REASON = (
    'Updates are held on this installation. The file recording why could '
    'not be read — see UPDATE_HOLD.json in the application directory.'
)


def _app_dir() -> Path:
    """Directory the app is installed in — the parent of this services/ dir."""
    return Path(__file__).resolve().parent.parent


def hold_path() -> Path:
    return _app_dir() / HOLD_FILE


def _condition_met(until: str) -> bool:
    """Has the condition the hold waits for come true?

    Unknown conditions are never met. A typo in the file must not open
    the gate — see the fail-closed note in ``status()``.
    """
    if until == 'container':
        try:
            from services.runtime_env import in_container
            return in_container()
        except Exception:
            logger.warning('could not decide the container condition',
                           exc_info=True)
            return False
    return False


def _normalise(reason: str, until: str, source: str, extra: dict) -> dict:
    if until not in CONDITIONS:
        logger.warning(
            "update hold names an unknown condition %r — treating it as "
            "'forever'. Known: %s", until, ', '.join(CONDITIONS))
        until = 'forever'
    eintrag = {
        'reason': reason or _FALLBACK_REASON,
        'until': until,
        'source': source,
    }
    for schluessel in ('set_at', 'set_by'):
        if extra.get(schluessel):
            eintrag[schluessel] = str(extra[schluessel])
    return eintrag


def status() -> dict | None:
    """The hold in force, or ``None`` when updates may proceed.

    🔴 This fails **closed**: a hold file that exists but cannot be read
    or parsed still holds, with a reason saying so. That is the opposite
    of how ``platform_service.own_tls_card_useful`` treats an
    unanswerable question, and the difference is what each mistake
    costs. Showing a settings card nobody needs is untidy; letting an
    update through that somebody deliberately blocked breaks a running
    system. When the evidence is unreadable, the gate that protects
    stays shut.
    """
    umgebung = os.environ.get(ENV_REASON, '').strip()
    if umgebung:
        eintrag = _normalise(umgebung,
                             os.environ.get(ENV_UNTIL, 'forever').strip() or 'forever',
                             'env', {})
        if _condition_met(eintrag['until']):
            return None
        return eintrag

    pfad = hold_path()
    try:
        if not pfad.is_file():
            return None
    except OSError:
        return None

    try:
        rohdaten = json.loads(pfad.read_text(encoding='utf-8'))
        if not isinstance(rohdaten, dict):
            raise ValueError('not a JSON object')
    except Exception:
        logger.warning('%s exists but could not be read — holding updates '
                       'anyway', pfad, exc_info=True)
        return _normalise('', 'forever', 'file', {})

    eintrag = _normalise(str(rohdaten.get('reason', '')).strip(),
                         str(rohdaten.get('until', 'forever')).strip(),
                         'file', rohdaten)
    if _condition_met(eintrag['until']):
        logger.info('update hold lifted: the %r condition is met',
                    eintrag['until'])
        return None
    return eintrag


def active() -> bool:
    return status() is not None


def describe() -> str:
    """One line for a log message. Empty when no hold is in force."""
    eintrag = status()
    if not eintrag:
        return ''
    return f"{eintrag['reason']} (until={eintrag['until']}, " \
           f"source={eintrag['source']})"
