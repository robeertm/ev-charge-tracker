"""Update-safety service: backup + auto-rollback on failed updates.

When an update goes sideways (new code crashes on boot, a migration
throws, a dependency is missing after pip install) the user currently
has a bricked app. This module runs a small state machine on every
boot so the *previous* working version gets restored automatically.

## State machine

Three file markers live in ``updates/``:

- ``backup_pre_v<OLD>/`` — directory snapshot of every file the update
  is about to overwrite. Created **before** the file swap.
- ``UPDATE_PENDING.json`` — written at the same moment. Contains the
  old/new versions, backup_dir path, an ``attempts`` counter, and a
  wall-clock timestamp.
- ``LAST_ROLLBACK.json`` — written once a rollback has fired, for the
  UI to surface a one-line explanation.

On every call to ``pre_boot_rollback_check()``:

1. No marker → clean boot, nothing to do.
2. Marker present, ``attempts >= MAX_ATTEMPTS`` → assume the new code
   is broken. Restore the backup, write ``LAST_ROLLBACK.json``,
   ``os._exit`` so the supervisor restarts us with the old files.
3. Otherwise → bump ``attempts`` and spawn a daemon thread that
   deletes the marker after ``VERIFICATION_DELAY_S`` seconds of
   uninterrupted runtime. If the app crashes before that timer fires
   (OOM, segfault, unhandled exception at startup) the marker stays,
   ``attempts`` increments on the next boot, and we eventually hit
   case 2.

That last detail is what makes the mechanism platform-agnostic: it
relies on nothing more than supervisor-restarts-on-crash. Works under
systemd (``Restart=always``) just as well as under Terminal + nohup.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Files / dirs the update flow must never touch. Kept in sync with the
# exclude list in updater.py / updater_helper.py so all three paths
# agree on what belongs to the user, not the release.
EXCLUDE_NAMES = {
    'venv', '.venv', 'data', 'logs', '.git', '.github',
    '__pycache__', 'updates',
}

MARKER_FILE = 'UPDATE_PENDING.json'
ROLLBACK_LOG = 'LAST_ROLLBACK.json'
BACKUP_PREFIX = 'backup_pre_v'

# Three boot attempts before we decide the new code really is broken.
# The rationale is: a healthy boot clears the marker within
# VERIFICATION_DELAY_S, so reaching attempts=3 means the app died three
# times before that timer could fire — no transient flake explains that.
MAX_ATTEMPTS = 3
VERIFICATION_DELAY_S = 60
MAX_KEPT_BACKUPS = 3


def _app_dir() -> Path:
    """Directory the app is installed in (the parent of this services/ dir)."""
    return Path(__file__).resolve().parent.parent


def _updates_dir() -> Path:
    d = _app_dir() / 'updates'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path() -> Path:
    return _updates_dir() / MARKER_FILE


def _rollback_log_path() -> Path:
    return _updates_dir() / ROLLBACK_LOG


# ── Backup creation ───────────────────────────────────────────────────

def _is_excluded(name: str) -> bool:
    return (name in EXCLUDE_NAMES
            or name.startswith('backup_')
            or name.endswith('.pyc')
            or name == '.DS_Store')


def create_pre_update_backup(staging_root: Path, old_version: str) -> Path:
    """Snapshot every file the staged update would overwrite.

    Only the files that *also* exist in the staging tree are copied —
    new files (added by the release) have nothing to back up, and
    unrelated files stay untouched. The result is a minimal
    backup_pre_v<OLD>/ directory suitable for a reverse swap later.
    """
    app_dir = _app_dir()
    staging_root = Path(staging_root)
    backup_dir = _updates_dir() / f'{BACKUP_PREFIX}{old_version}'
    # Wipe any stale backup under the same name so a retry of the same
    # update version doesn't mingle old and new snapshots.
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / 'VERSION.txt').write_text(f'{old_version}\n', encoding='utf-8')

    copied = 0
    for item in staging_root.iterdir():
        name = item.name
        if _is_excluded(name):
            continue
        src = app_dir / name
        if not src.exists():
            continue  # release adds a new file, no backup needed
        dst = backup_dir / name
        try:
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            copied += 1
        except Exception as e:
            logger.warning(f'pre-update backup: failed to snapshot {name}: {e}')

    logger.info(f'pre-update backup created at {backup_dir} ({copied} items)')
    _prune_old_backups()
    return backup_dir


def _prune_old_backups() -> None:
    """Keep at most ``MAX_KEPT_BACKUPS`` pre-update snapshots. Oldest
    (by mtime) are pruned first so the newest few always remain as a
    safety net when the user hits several updates in a row."""
    try:
        candidates = sorted(
            (p for p in _updates_dir().iterdir()
             if p.is_dir() and p.name.startswith(BACKUP_PREFIX)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in candidates[MAX_KEPT_BACKUPS:]:
            shutil.rmtree(stale, ignore_errors=True)
    except Exception as e:
        logger.debug(f'backup prune failed (non-fatal): {e}')


# ── Pending marker ────────────────────────────────────────────────────

def write_pending_marker(
    old_version: str,
    new_version: str,
    backup_dir: Path,
) -> None:
    """Create UPDATE_PENDING.json so the next boot knows a verification
    is in flight."""
    data = {
        'old_version':   old_version,
        'new_version':   new_version,
        'backup_dir':    str(backup_dir),
        'written_at':    datetime.now().isoformat(timespec='seconds'),
        'attempts':      0,
    }
    _marker_path().write_text(json.dumps(data, indent=2), encoding='utf-8')


def _read_marker() -> Optional[dict]:
    p = _marker_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        # Corrupt marker is indistinguishable from absent for our purposes.
        try:
            p.unlink()
        except OSError:
            pass
        return None


def _clear_marker() -> None:
    try:
        _marker_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f'could not clear UPDATE_PENDING marker: {e}')


# ── Backup restoration ────────────────────────────────────────────────

def restore_backup(backup_dir: Path) -> int:
    """Swap every file in ``backup_dir`` back into the app dir. Returns
    the count of restored entries so the caller can log something
    meaningful. Silent on individual failures — rollback is best-effort,
    partial restoration is still better than nothing."""
    app_dir = _app_dir()
    restored = 0
    for item in Path(backup_dir).iterdir():
        if item.name == 'VERSION.txt':
            continue
        if _is_excluded(item.name):
            continue
        dst = app_dir / item.name
        try:
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                if dst.exists():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                shutil.copy2(item, dst)
            restored += 1
        except Exception as e:
            logger.error(f'rollback: failed to restore {item.name}: {e}')
    return restored


def _record_rollback(marker: dict, reason: str, restored: int) -> None:
    data = {
        'rolled_back_at':    datetime.now().isoformat(timespec='seconds'),
        'rolled_back_from':  marker.get('new_version'),
        'restored_to':       marker.get('old_version'),
        'reason':            reason,
        'restored_items':    restored,
    }
    try:
        _rollback_log_path().write_text(json.dumps(data, indent=2), encoding='utf-8')
    except OSError as e:
        logger.warning(f'could not write LAST_ROLLBACK marker: {e}')


def read_last_rollback() -> Optional[dict]:
    """Used by the settings UI to show a one-liner when the app was
    automatically rolled back on the last boot."""
    p = _rollback_log_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


def clear_last_rollback() -> None:
    """Acknowledge the rollback banner (called from settings UI)."""
    try:
        _rollback_log_path().unlink()
    except FileNotFoundError:
        pass


# ── The boot hook ─────────────────────────────────────────────────────

def _spawn_verification_timer(delay_s: int = VERIFICATION_DELAY_S) -> None:
    """Start a daemon thread that clears the pending marker after
    ``delay_s`` seconds. If the app crashes before the timer fires the
    marker survives, and on the next boot ``attempts`` grows."""
    def _clear():
        try:
            time.sleep(delay_s)
            _clear_marker()
            logger.info(f'update verification window elapsed; marker cleared')
        except Exception as e:
            logger.warning(f'verification timer: {e}')
    t = threading.Thread(target=_clear, daemon=True, name='update-verify')
    t.start()


def pre_boot_rollback_check() -> None:
    """Inspect UPDATE_PENDING.json at startup and decide whether to
    continue booting or roll back first. Call this as the very first
    step in ``create_app()`` — BEFORE db.create_all() or any migration.

    Three outcomes, each with a log line so the operator can trace what
    happened after the fact:

    - No marker → normal boot path.
    - Marker present, attempts < MAX_ATTEMPTS → increment and start a
      verification timer that clears the marker after
      ``VERIFICATION_DELAY_S`` seconds of successful runtime.
    - Marker present, attempts >= MAX_ATTEMPTS → restore the backup and
      ``os._exit(0)`` so the supervisor restarts us with the old code.
    """
    marker = _read_marker()
    if not marker:
        return

    attempts = int(marker.get('attempts', 0))
    backup_dir = Path(marker.get('backup_dir', ''))
    new_v = marker.get('new_version', '?')
    old_v = marker.get('old_version', '?')

    if attempts >= MAX_ATTEMPTS:
        reason = f'boot failed {attempts}× after update to v{new_v}'
        logger.error(f'rollback triggered: {reason}')
        if backup_dir.exists():
            restored = restore_backup(backup_dir)
            _record_rollback(marker, reason, restored)
            _clear_marker()
            logger.error(
                f'rolled back to v{old_v} ({restored} items restored). '
                f'Exiting so the supervisor restarts with the previous version.'
            )
        else:
            # No backup → nothing we can do. Still clear the marker so
            # we don't loop forever on something unrollable.
            reason += ' (backup_dir missing; no rollback possible)'
            _record_rollback(marker, reason, 0)
            _clear_marker()
            logger.error(reason)
        # os._exit (not sys.exit) to avoid Flask atexit handlers messing
        # with the freshly restored files.
        os._exit(0)

    # Attempt <= threshold: bump counter, schedule verification.
    marker['attempts'] = attempts + 1
    marker['last_attempt_at'] = datetime.now().isoformat(timespec='seconds')
    try:
        _marker_path().write_text(json.dumps(marker, indent=2), encoding='utf-8')
    except OSError as e:
        logger.warning(f'could not update attempt counter: {e}')
    logger.info(
        f'update to v{new_v} in verification (attempt {marker["attempts"]}/{MAX_ATTEMPTS}); '
        f'marker clears after {VERIFICATION_DELAY_S}s of uptime.'
    )
    _spawn_verification_timer()
