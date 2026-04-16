"""Auto-updater for EV Charge Tracker via GitHub Releases.

Two-stage flow that actually rolls out a working install:

1. ``check_for_update()`` queries the GitHub releases API and compares
   semver tuples (so downgrades are never offered, ``2.10.0`` > ``2.9.0``).
2. ``apply_update()`` downloads the release source zip into a staging
   directory, then spawns ``updater_helper.py`` as a fully detached
   process. The helper waits for the running Flask process to exit,
   swaps files (preserving ``venv/``, ``data/``, ``.git/``),
   refreshes the venv via ``pip install -r requirements.txt``, and
   restarts the app via the platform start script.

The detour through ``updater_helper.py`` is necessary because the
running Flask process cannot safely overwrite its own ``app.py`` /
templates while it is still serving requests.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)

GITHUB_API = f"https://api.github.com/repos/{Config.GITHUB_REPO}/releases/latest"
USER_AGENT = "EV-Charge-Tracker"


# ── Version comparison ────────────────────────────────────────────────

def _parse_version(v: str) -> tuple:
    """Parse 'X.Y.Z' (or 'X.Y.Z-suffix') into a tuple of ints for comparison.
    Returns (0,) on parse failure so it sorts as oldest."""
    try:
        core = v.split('-', 1)[0]  # strip pre-release suffix
        return tuple(int(p) for p in core.split('.'))
    except (ValueError, AttributeError):
        return (0,)


def _is_newer(latest: str, current: str) -> bool:
    """Return True only if `latest` is strictly newer than `current`."""
    return _parse_version(latest) > _parse_version(current)


# ── GitHub release lookup ─────────────────────────────────────────────

def _github_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def check_for_update() -> Tuple[Optional[str], Optional[str]]:
    """Check GitHub for a strictly newer release.

    Returns ``(new_version, download_url)`` or ``(None, None)``.
    """
    try:
        data = _github_get_json(GITHUB_API)
        latest = (data.get('tag_name') or '').lstrip('v')
        if latest and _is_newer(latest, Config.APP_VERSION):
            zip_url = data.get('zipball_url') or ''
            return latest, zip_url
        return None, None
    except Exception as e:
        logger.error(f"Update check failed: {e}")
        return None, None


# ── Install flow ──────────────────────────────────────────────────────

def _app_dir() -> Path:
    return Path(__file__).resolve().parent


def _staging_root() -> Path:
    return _app_dir() / 'updates' / 'staging'


def _download_zip(url: str, dst: Path) -> None:
    """Stream-download a release zip from GitHub to `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60.0) as resp, open(dst, 'wb') as fh:
        shutil.copyfileobj(resp, fh)


def _extract_and_unwrap(zip_path: Path, staging: Path) -> Path:
    """Extract `zip_path` into `staging`. If the archive contains a single
    top-level directory (the standard layout for GitHub source zips), return
    that directory as the new staging root."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(staging)

    entries = [p for p in staging.iterdir() if p.name != '.DS_Store']
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


def _running_under_systemd() -> bool:
    """True if the current process was launched by systemd (Linux service).

    Under systemd the spawn-helper approach breaks: the helper ends up in
    the same cgroup as the service, so when the service exits to allow the
    file-swap, systemd kills the helper along with it. On systemd we do the
    swap inline instead and rely on `Restart=always` to bring the app back.
    """
    if os.name == 'nt':
        return False
    if os.environ.get('INVOCATION_ID'):
        return True
    try:
        return Path('/run/systemd/system').is_dir()
    except Exception:
        return False


# Files/dirs never overwritten by an inline swap.
_EXCLUDE_NAMES = {
    'venv', '.venv', 'data', 'logs', '.git', '.github',
    '__pycache__', 'updates',
}


def _is_excluded(name: str) -> bool:
    if name in _EXCLUDE_NAMES:
        return True
    if name.startswith('backup_'):
        return True
    if name.endswith('.pyc') or name == '.DS_Store':
        return True
    return False


def _inline_swap(staging_root: Path, new_version: str = '') -> bool:
    """Swap files from staging into the app dir in the current process.

    Safe because Python already holds the source as bytecode in memory —
    overwriting the .py files on disk doesn't affect the running interpreter.
    The running process is expected to exit shortly after so a supervisor
    (systemd) restarts it with the new code.

    Before touching files we snapshot the current versions into
    ``updates/backup_pre_v<OLD>/`` and write UPDATE_PENDING.json so the
    boot-time rollback guard (``pre_boot_rollback_check``) can revert on
    repeated crashes.
    """
    app_dir = _app_dir()
    # Backup first, swap second. If backup fails we bail out rather
    # than apply an un-rollbackable update.
    try:
        from services.update_service import (
            create_pre_update_backup, write_pending_marker,
        )
        backup_dir = create_pre_update_backup(staging_root, Config.APP_VERSION)
        write_pending_marker(
            old_version=Config.APP_VERSION,
            new_version=new_version or '?',
            backup_dir=backup_dir,
        )
    except Exception as e:
        logger.error(f'pre-update backup failed — aborting update: {e}')
        return False

    try:
        for item in staging_root.iterdir():
            name = item.name
            if _is_excluded(name):
                continue
            dst = app_dir / name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst)
    except Exception as e:
        logger.error(f"Inline file swap failed: {e}")
        return False

    # Refresh venv dependencies synchronously. We have to block here because
    # the caller will exit() right after — if pip runs in a background thread
    # it gets killed along with the process.
    req = app_dir / 'requirements.txt'
    venv_py = None
    for vname in ('venv', '.venv'):
        cand = app_dir / vname / 'bin' / 'python'
        if cand.exists():
            venv_py = cand
            break
    if venv_py and req.exists():
        logger.info("Running pip install -r requirements.txt (inline)…")
        try:
            subprocess.run(
                [str(venv_py), '-m', 'pip', 'install', '-r', str(req)],
                check=False,
                timeout=300,
            )
        except Exception as e:
            logger.warning(f"pip install failed (continuing): {e}")

    # Clean up staging
    try:
        shutil.rmtree(staging_root)
    except Exception:
        pass

    return True


def _spawn_helper(staging_root: Path, new_version: str = '') -> None:
    """Launch updater_helper.py fully detached from this process.

    Prefers the helper script from the **staging** directory (the new
    release) if it exists, so improvements to the helper take effect on
    the very first update that ships them — without needing a second
    update cycle to install them.
    """
    app_dir = _app_dir()
    staging_helper = staging_root / 'updater_helper.py'
    helper_path = staging_helper if staging_helper.exists() else app_dir / 'updater_helper.py'
    helper = [
        sys.executable, str(helper_path),
        '--app-dir', str(app_dir),
        '--staging-dir', str(staging_root),
        '--wait-pid', str(os.getpid()),
        '--update-deps', '1',
        '--restart', '1',
        '--old-version', Config.APP_VERSION,
        '--new-version', new_version or '?',
    ]
    log_path = app_dir / 'updates' / 'updater.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, 'a', buffering=1)
    log_fh.write(f"\n[updater] launching helper for staging={staging_root}\n")

    if os.name == 'nt':
        creationflags = (
            getattr(subprocess, 'DETACHED_PROCESS', 0)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        )
        subprocess.Popen(
            helper,
            cwd=str(app_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=creationflags,
            close_fds=False,
        )
    else:
        subprocess.Popen(
            helper,
            cwd=str(app_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )


def apply_update(zip_url: str, new_version: str) -> bool:
    """Download a release ZIP, stage it, then hand off to ``updater_helper``.

    Returns True if the helper was successfully spawned. The caller is
    responsible for shutting down the Flask process shortly afterwards
    so the helper can swap files.
    """
    try:
        app_dir = _app_dir()
        upd_dir = app_dir / 'updates'
        upd_dir.mkdir(parents=True, exist_ok=True)
        zip_path = upd_dir / f'v{new_version}.zip'

        logger.info(f"Downloading update v{new_version} from {zip_url}")
        _download_zip(zip_url, zip_path)

        staging = _staging_root()
        logger.info(f"Extracting to {staging}")
        staging_root = _extract_and_unwrap(zip_path, staging)
        logger.info(f"Staging root: {staging_root}")

        # Sanity check: staging root should at least contain app.py
        if not (staging_root / 'app.py').exists():
            logger.error("Staging root missing app.py — aborting update")
            return False

        if _running_under_systemd():
            logger.info("Detected systemd — applying update inline, will exit for restart")
            if not _inline_swap(staging_root, new_version=new_version):
                return False
            return True

        logger.info("Spawning updater_helper")
        _spawn_helper(staging_root, new_version=new_version)
        return True
    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


# ── CLI entry point ───────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print(f"Current version: {Config.APP_VERSION}")
    new_ver, url = check_for_update()
    if new_ver:
        print(f"New version available: {new_ver}")
        if input("Apply update? (y/N): ").strip().lower() == 'y':
            if apply_update(url, new_ver):
                print("Update staged. The app will restart automatically.")
                print("Stop the running Flask process now if it isn't already shutting down.")
            else:
                print("Update failed. Check logs in updates/updater.log")
    else:
        print("You're running the latest version.")
