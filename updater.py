"""Auto-updater for EV Charge Tracker via GitHub Releases."""
import os
import sys
import json
import shutil
import zipfile
import logging
import tempfile
import urllib.request
from config import Config

logger = logging.getLogger(__name__)
GITHUB_API = f"https://api.github.com/repos/{Config.GITHUB_REPO}/releases/latest"


def _parse_version(v):
    """Parse 'X.Y.Z' (or 'X.Y.Z-suffix') into a tuple of ints for comparison.
    Returns (0,) on parse failure so it sorts as oldest."""
    try:
        core = v.split('-', 1)[0]  # strip pre-release suffix
        return tuple(int(p) for p in core.split('.'))
    except (ValueError, AttributeError):
        return (0,)


def _is_newer(latest, current):
    """Return True only if `latest` is strictly newer than `current`."""
    return _parse_version(latest) > _parse_version(current)


def check_for_update():
    """Check GitHub for a newer release. Returns (new_version, download_url) or (None, None)."""
    try:
        req = urllib.request.Request(GITHUB_API, headers={'User-Agent': 'EV-Charge-Tracker'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        latest = data.get('tag_name', '').lstrip('v')
        current = Config.APP_VERSION

        if latest and _is_newer(latest, current):
            zip_url = data.get('zipball_url', '')
            return latest, zip_url
        return None, None

    except Exception as e:
        logger.error(f"Update check failed: {e}")
        return None, None


def apply_update(zip_url, new_version):
    """Download and apply an update from GitHub."""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        backup_dir = os.path.join(app_dir, f'backup_v{Config.APP_VERSION}')

        logger.info(f"Downloading update v{new_version}...")
        tmp = tempfile.mktemp(suffix='.zip')
        urllib.request.urlretrieve(zip_url, tmp)

        # Create backup of current version (exclude data/)
        logger.info("Creating backup...")
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        os.makedirs(backup_dir)
        for item in os.listdir(app_dir):
            if item in ('data', 'backup_*', '__pycache__', '.git'):
                continue
            src = os.path.join(app_dir, item)
            dst = os.path.join(backup_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        # Extract update
        logger.info("Applying update...")
        with zipfile.ZipFile(tmp, 'r') as zf:
            members = zf.namelist()
            # GitHub zips have a top-level directory
            prefix = members[0] if members else ''
            for member in members:
                if member == prefix:
                    continue
                rel_path = member[len(prefix):]
                if not rel_path or rel_path.startswith('data/'):
                    continue  # Skip data directory
                target = os.path.join(app_dir, rel_path)
                if member.endswith('/'):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, 'wb') as dst:
                        dst.write(src.read())

        os.unlink(tmp)
        logger.info(f"Update to v{new_version} complete! Restart the app.")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print(f"Current version: {Config.APP_VERSION}")
    new_ver, url = check_for_update()
    if new_ver:
        print(f"New version available: {new_ver}")
        if input("Apply update? (y/N): ").strip().lower() == 'y':
            if apply_update(url, new_ver):
                print("Update applied! Please restart the application.")
            else:
                print("Update failed. Check logs.")
    else:
        print("You're running the latest version.")
