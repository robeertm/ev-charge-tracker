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
    """True if the current process was launched by systemd (Linux service)."""
    if os.name == 'nt':
        return False
    if os.environ.get('INVOCATION_ID'):
        return True
    try:
        return Path('/run/systemd/system').is_dir()
    except Exception:
        return False


def updates_by_image() -> bool:
    """True when this installation is updated by replacing its image.

    A container's application code lives in the image, not in a volume:
    only ``/app/data`` is declared as one. An in-app update writes the
    new files into the container's **writable layer**, which is exactly
    the layer ``docker compose pull`` throws away when it recreates the
    container. Measured on the published image:

        3.0.124 → in-app update → 3.0.125 → ``docker restart`` → 3.0.125
                                          → container recreated → 3.0.124

    with no message anywhere, and the updater's own
    ``updates/backup_pre_*`` rollback copy gone with it. So the app
    reported a version that a perfectly routine container operation
    silently took back — and a user who then followed the documented
    image-update path ended up *behind* where they thought they were.

    The Dockerfile already says this about connectors ("a connector
    pip-installed from the running app lands in the writable layer,
    which `docker compose pull` throws away"). It is just as true of the
    app's own code, and this is where that is acted on.

    So a container is told the truth instead: the update exists, here is
    what changed, and it arrives with the next image. The check itself
    keeps running — knowing a new version is out is useful either way.
    """
    from services.runtime_env import in_container
    return in_container()


def swaps_inline() -> bool:
    """True when the update must be applied in THIS process, not by a helper.

    Two situations, one answer.

    **systemd**: the detached helper lands in the same cgroup as the
    service, so when the service exits to let the swap happen, systemd
    kills the helper along with it.

    **A container**: worse. The app is normally PID 1, and when PID 1
    exits the container stops and every other process in it is killed —
    including a helper that is halfway through replacing files. The
    detached-helper path cannot work in a container at all, and the
    public image has no systemd, no INVOCATION_ID and no
    /run/systemd/system, so this predicate used to answer False there and
    send it down exactly that path.

    That is the difference between the published image and the private
    one behind the Ioniq 6 host: the private image sets INVOCATION_ID by
    hand in its entrypoint, precisely so this returns True. Container
    users of the published image had no such luck — which is why the
    detection belongs in the app, not in someone's Dockerfile.

    After an inline swap the caller re-executes the process
    (services.restart_service), so this needs no supervisor and no
    restart policy either.
    """
    if _running_under_systemd():
        return True
    try:
        from services.runtime_env import in_container
        return in_container()
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
    # A native install keeps its interpreter in a virtualenv next to the
    # app; a container has none and installs into the image's own
    # site-packages. This used to look ONLY for the virtualenv and skip
    # the whole dependency step without a word when there wasn't one —
    # so in a container an update that shipped a new dependency (a new
    # vehicle connector, say) installed nothing at all, and the app came
    # back reporting the new version while missing the thing the version
    # was about. sys.executable is the right answer in both cases.
    venv_py = None
    for vname in ('venv', '.venv'):
        cand = app_dir / vname / 'bin' / 'python'
        if cand.exists():
            venv_py = cand
            break
    pip_py = str(venv_py) if venv_py else sys.executable
    if req.exists():
        logger.info(f"Running pip install -r requirements.txt via {pip_py} (inline)…")
        try:
            subprocess.run(
                [pip_py, '-m', 'pip', 'install', '-r', str(req)],
                check=False,
                timeout=300,
            )
        except Exception as e:
            logger.warning(f"pip install failed (continuing): {e}")

    # Vehicle connectors — best effort, ONE LINE AT A TIME.
    #
    # Not `-r` as a whole: hyundai-kia-connect-api needs Python >=3.12, so
    # on a Raspberry Pi OS bookworm box (3.11) a single unsatisfiable line
    # would abort the file and cost the user every OTHER brand as well.
    # Installed line by line, that box simply keeps the brands it can run.
    #
    # The container does not come through here — its connectors are baked
    # into the image (Dockerfile) and arrive with the next image pull.
    vreq = app_dir / 'requirements-vehicles.txt'
    if vreq.exists():
        try:
            lines = [ln.split('#')[0].strip()
                     for ln in vreq.read_text().splitlines()]
            wanted = [ln for ln in lines if ln]
            logger.info(f"Refreshing {len(wanted)} vehicle connectors (best effort)…")
            for spec in wanted:
                try:
                    # NOT --upgrade. On a machine that already has a
                    # working connector, --upgrade would pull the newest
                    # release of it during a routine app update and could
                    # change how a live car sync behaves — a blast radius
                    # nobody asked for. Without the flag pip still
                    # upgrades whenever the SPEC demands it (e.g.
                    # hyundai-kia-connect-api>=4.26.5 against an older
                    # one), which is exactly when an upgrade is intended.
                    r = subprocess.run(
                        [pip_py, '-m', 'pip', 'install', spec],
                        check=False, capture_output=True, timeout=300,
                    )
                    if r.returncode != 0:
                        logger.info(f"  skipped {spec} (not installable here)")
                except Exception as e:
                    logger.info(f"  skipped {spec}: {e}")
        except Exception as e:
            logger.warning(f"vehicle connector refresh failed (continuing): {e}")

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


def _vehicle_is_charging_from_sqlite() -> bool:
    """Best-effort check: read latest VehicleSync.is_charging directly
    from SQLite, without needing a Flask app context. Returns False on
    any error (missing DB, unknown schema) so the gate defaults to
    *allow update* — a read failure must never brick a release.
    """
    try:
        import sqlite3
        from config import DATA_DIR
        db_path = os.path.join(DATA_DIR, 'ev_tracker.db')
        if not os.path.exists(db_path):
            return False
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT is_charging FROM vehicle_sync ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def apply_update(zip_url: str, new_version: str, force: bool = False,
                 allow_in_container: bool = False) -> bool:
    """Download a release ZIP, stage it, then hand off to ``updater_helper``.

    Returns True if the helper was successfully spawned. The caller is
    responsible for shutting down the Flask process shortly afterwards
    so the helper can swap files.

    Container gate: refuses outright when this install updates by image
    (see ``updates_by_image``), because the swap would be thrown away
    with the writable layer the next time the container is recreated.
    The gate sits here as well as in the route on purpose — the route
    is where today's caller lives, this is where tomorrow's will. The
    CLI entry point below passes ``allow_in_container=True``: someone
    who has opened a shell inside the container and typed "y" has said
    plainly that they want it anyway, and it is their own throwaway
    layer to spend.

    Charging gate: when ``force`` is False (default), refuses to apply
    the update while the vehicle is actively charging. Restarting
    mid-charge breaks the sync loop briefly and can miss the
    charge-end transition the app normally logs automatically. Pass
    ``force=True`` to bypass (emergency path). ``force`` deliberately
    does NOT lift the container gate: charging is a matter of timing,
    an image-based install is a matter of where the files live.
    """
    if not allow_in_container and updates_by_image():
        logger.warning(
            f"apply_update(v{new_version}) refused: this installation is "
            "updated by pulling a new container image. A file swap here "
            "would be discarded when the container is next recreated."
        )
        return False
    if not force and _vehicle_is_charging_from_sqlite():
        logger.warning(
            f"apply_update(v{new_version}) refused: vehicle is currently charging. "
            "Pass force=True to bypass."
        )
        return False
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

        if swaps_inline():
            logger.info("Applying update inline (systemd or container), will restart after")
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
            # Von Hand in der Shell des Containers: ausdruecklich erlaubt.
            if apply_update(url, new_ver, allow_in_container=True):
                print("Update staged. The app will restart automatically.")
                print("Stop the running Flask process now if it isn't already shutting down.")
            else:
                print("Update failed. Check logs in updates/updater.log")
    else:
        print("You're running the latest version.")
