# -*- coding: utf-8 -*-
"""Ask the sibling container to pull a new image and recreate us.

A container cannot replace its own image from inside — the process doing
the replacing is the one being replaced. v3.0.125 therefore stopped the
in-app file swap in containers, because it landed in the writable layer
and a later ``docker compose pull`` silently threw it away, putting the
app back on the old version with nothing to say so.

That was correct but left the owner with a shell command, which assumes
they are sitting at the host. They may be on a phone on another
continent while the server hums away at home.

So a small sibling container does the work, and this module is how the
app asks: it writes a marker into a shared volume and reads back the
status the sibling writes there.

🔑 **The marker is a trigger, not an instruction.** Nothing the app
writes ends up in a command — everything the sibling may do is fixed in
``deploy/updater.sh``. So the app has no Docker access at all, and the
worst anyone reaching the web UI can cause is "update to the published
image".

When no sibling is installed (an older compose file, or a hand-rolled
setup) ``available()`` is False and the UI shows the manual command
instead. Nothing here fails loudly for people who never opted in.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Matches the mount point in docker-compose.yml.
INBOX = Path(os.environ.get('EV_UPDATER_INBOX', '/app/updater-inbox'))
REQUEST = 'update.request'
STATUS = 'update.status'
LOG = 'update.log'

# A request that old cannot still be in flight — the sibling checks
# every few seconds and a pull takes a minute or two, not an hour.
STALE_SECONDS = 30 * 60


def available() -> bool:
    """Is an updater sibling wired up and running?

    Two conditions, and both matter. The directory means the volume is
    mounted; the status file means something on the other side is alive
    and has written to it. A mounted-but-empty volume is a half-finished
    setup, and promising a button for it would be worse than showing the
    command.
    """
    try:
        return INBOX.is_dir() and (INBOX / STATUS).is_file()
    except OSError:
        return False


def status() -> dict:
    """What the sibling last reported. ``{}`` when there is nothing."""
    try:
        raw = (INBOX / STATUS).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def pending() -> bool:
    """Is a request waiting to be picked up?

    A leftover marker from a sibling that died would otherwise block the
    button for ever, so anything older than ``STALE_SECONDS`` does not
    count as pending.
    """
    f = INBOX / REQUEST
    try:
        if not f.is_file():
            return False
        alter = datetime.now(timezone.utc).timestamp() - f.stat().st_mtime
        return alter < STALE_SECONDS
    except OSError:
        return False


def request(version: str = '') -> bool:
    """Drop the marker. Returns False when there is nobody to read it.

    ``version`` is recorded for the log and for the UI to show; it is
    deliberately NOT passed to the sibling as an instruction.
    """
    if not available():
        return False
    try:
        inhalt = json.dumps({
            'requested_at': datetime.now(timezone.utc).isoformat(),
            'requested_version': str(version or ''),
        })
        tmp = INBOX / (REQUEST + '.tmp')
        tmp.write_text(inhalt, encoding='utf-8')
        # Rename, so the sibling never sees a half-written file.
        tmp.replace(INBOX / REQUEST)
        logger.info('container update requested (version %s)', version or '?')
        return True
    except OSError as e:
        logger.warning('could not write the update request: %s', e)
        return False


def log_tail(zeilen: int = 40) -> str:
    """The sibling's log, for when an update fails."""
    try:
        text = (INBOX / LOG).read_text(encoding='utf-8', errors='replace')
    except (OSError, UnicodeDecodeError):
        return ''
    return '\n'.join(text.splitlines()[-zeilen:])
