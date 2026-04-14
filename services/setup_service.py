"""First-run setup wizard for VM deployments.

When the app is provisioned on a fresh VM (via /usr/local/bin/ev-provision),
the provisioning script drops a marker file at `SETUP_MARKER` to tell the app
that the end user still needs to change the temporary LUKS passphrase. On the
next visit, a `before_request` hook redirects to the /setup wizard, which
handles the passphrase change through the browser instead of making the user
SSH in and run `cryptsetup luksChangeKey` by hand.

This module is deliberately Linux- and systemd-specific. On any non-VM host
(e.g. a developer laptop running the app directly), the marker file will never
exist and the module stays out of the way.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Marker dropped by ev-provision at the end of VM provisioning.
# Lives on the LUKS-encrypted data volume, so it's naturally scoped to
# "this VM is still in first-run state after a fresh provision".
SETUP_MARKER = Path('/srv/ev-data/.setup_pending')

# Name of the LUKS mapping created by ev-provision.
LUKS_MAPPING = 'evdata'


def is_setup_pending() -> bool:
    """Cheap check: does the first-run marker file exist?"""
    try:
        return SETUP_MARKER.is_file()
    except Exception:
        return False


def get_luks_device() -> Optional[str]:
    """Return the underlying block device for the `evdata` LUKS mapping.

    Parses the output of `cryptsetup status evdata` to find the `device:` line.
    Returns something like `/dev/sdb` or `None` if the mapping isn't open.
    """
    try:
        result = subprocess.run(
            ['cryptsetup', 'status', LUKS_MAPPING],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('device:'):
                return line.split(':', 1)[1].strip()
    except Exception as e:
        logger.warning(f"cryptsetup status failed: {e}")
    return None


def change_luks_passphrase(old_passphrase: str, new_passphrase: str) -> tuple[bool, str]:
    """Change the LUKS passphrase on the data device.

    The app runs as `ev-tracker` which has a sudoers NOPASSWD entry for
    `/sbin/cryptsetup luksChangeKey` (installed by ev-provision).

    Returns (ok, message).
    """
    if not old_passphrase or not new_passphrase:
        return False, 'Passphrase darf nicht leer sein.'
    if len(new_passphrase) < 6:
        return False, 'Neue Passphrase muss mindestens 6 Zeichen haben.'

    device = get_luks_device()
    if not device:
        return False, 'LUKS-Device nicht gefunden. Ist das Volume entsperrt?'

    # cryptsetup luksChangeKey reads old passphrase from stdin first, then
    # new passphrase. --batch-mode suppresses the "are you sure" prompt.
    stdin_data = f"{old_passphrase}\n{new_passphrase}\n".encode()
    try:
        result = subprocess.run(
            ['sudo', '-n', '/sbin/cryptsetup', 'luksChangeKey', '--batch-mode', device],
            input=stdin_data,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, 'cryptsetup hat nicht rechtzeitig geantwortet.'
    except FileNotFoundError:
        return False, 'cryptsetup nicht installiert.'

    if result.returncode == 0:
        logger.info(f"LUKS passphrase changed successfully on {device}")
        return True, 'LUKS-Passphrase geändert.'

    err = (result.stderr or b'').decode(errors='replace').strip()
    # Map the most common failure to a user-friendly German message.
    if 'No key available' in err or 'No usable keyslot' in err:
        return False, 'Die aktuelle (temporäre) Passphrase stimmt nicht.'
    logger.error(f"cryptsetup luksChangeKey failed: {err}")
    return False, f'Fehler beim Ändern: {err[:200]}'


def complete_setup() -> bool:
    """Remove the setup marker so future visits skip the wizard."""
    try:
        if SETUP_MARKER.is_file():
            SETUP_MARKER.unlink()
        return True
    except Exception as e:
        logger.error(f"Failed to remove setup marker: {e}")
        return False
