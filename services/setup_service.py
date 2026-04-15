"""First-run setup wizard for VM deployments.

When the app is provisioned on a fresh VM (via /usr/local/bin/ev-provision),
the provisioning script drops a marker file at `SETUP_MARKER` to tell the app
that the end user still needs to go through the two-step first-run wizard:

    1. Change the temporary LUKS passphrase to something only the end user
       knows. The admin's ev-provision temp passphrase becomes invalid.
    2. Create a Web-UI login (username + password). This is mandatory; the
       wizard cannot complete without it, and the new credentials are wired
       straight into `services/auth_service.set_credentials()`, which enables
       the auth guard for every subsequent request.

Progress across steps is tracked in a small JSON state file next to the
marker, so a mid-wizard reload doesn't reset the user to the first step.

Note: the wizard does NOT change the ev-tracker Unix login password (the
thing an SSH client would use). That password stays at whatever
`ev-provision` set, so the admin retains SSH access to the VM for
maintenance. The end user has no terminal path into the box and therefore
does not need a shell password at all.

This module is deliberately Linux-specific. On any non-VM host (e.g. a
developer laptop running the app directly), the marker file will never
exist and the module stays out of the way.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Marker dropped by ev-provision at the end of VM provisioning.
# Lives on the LUKS-encrypted data volume, so it's naturally scoped to
# "this VM is still in first-run state after a fresh provision".
SETUP_MARKER = Path('/srv/ev-data/.setup_pending')

# Tracks which wizard steps have been completed, so a reload after a partial
# run skips already-finished steps.
SETUP_STATE_FILE = Path('/srv/ev-data/.setup_state.json')

# Name of the LUKS mapping created by ev-provision.
LUKS_MAPPING = 'evdata'


def is_setup_pending() -> bool:
    """True only on a freshly-provisioned Linux VM that still needs first-run setup.

    Guarded twice:
    1. Platform must be Linux — the wizard shells out to `cryptsetup` and
       `chpasswd`, which don't exist on macOS/Windows. Running on those
       platforms (e.g. developer laptop) should never trigger the wizard.
    2. `/srv/ev-data/.setup_pending` must exist — the marker is dropped by
       `ev-provision` at the end of VM provisioning and cleared by
       `complete_setup()` after the user finishes the wizard.
    """
    if not sys.platform.startswith('linux'):
        return False
    try:
        return SETUP_MARKER.is_file()
    except Exception:
        return False


def get_luks_device() -> Optional[str]:
    """Return the underlying block device for the `evdata` LUKS mapping.

    Resolves `/dev/mapper/evdata` to its kernel dm-N name, then reads the
    parent device from `/sys/block/dm-N/slaves/`. This works for any user
    because /sys is world-readable — `cryptsetup status` would need to open
    /dev/mapper/evdata directly, which is root:disk 660 on Debian and denies
    the unprivileged `ev-tracker` app user.
    """
    try:
        dm_path = Path(f'/dev/mapper/{LUKS_MAPPING}')
        if not dm_path.exists():
            return None
        dm_kernel_name = os.path.basename(os.path.realpath(dm_path))
        slaves_dir = Path(f'/sys/block/{dm_kernel_name}/slaves')
        if not slaves_dir.is_dir():
            return None
        slaves = sorted(
            p.name for p in slaves_dir.iterdir() if not p.name.startswith('.')
        )
        if slaves:
            return f'/dev/{slaves[0]}'
    except Exception as e:
        logger.warning(f"get_luks_device via sysfs failed: {e}")
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


# ── Wizard state tracking ────────────────────────────────────────────

def _default_state() -> dict:
    return {'luks_done': False, 'weblogin_done': False}


def load_state() -> dict:
    """Return the wizard progress state, with all keys always present."""
    state = _default_state()
    try:
        if SETUP_STATE_FILE.is_file():
            loaded = json.loads(SETUP_STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update({k: bool(v) for k, v in loaded.items() if k in state})
    except Exception as e:
        logger.warning(f"load_state failed, using defaults: {e}")
    return state


def save_state(state: dict) -> None:
    try:
        SETUP_STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.error(f"save_state failed: {e}")


def mark_step_done(step: str) -> None:
    state = load_state()
    if step in state:
        state[step] = True
        save_state(state)


def complete_setup() -> bool:
    """Remove the setup marker and state file so future visits skip the wizard."""
    try:
        if SETUP_MARKER.is_file():
            SETUP_MARKER.unlink()
        if SETUP_STATE_FILE.is_file():
            SETUP_STATE_FILE.unlink()
        return True
    except Exception as e:
        logger.error(f"Failed to remove setup marker: {e}")
        return False
