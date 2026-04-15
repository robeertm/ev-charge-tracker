"""OS-level security update status + manual trigger via unattended-upgrades.

Deliberately narrow: we do NOT expose full apt-get access to the web UI.
Only the Debian `unattended-upgrades` tool, which is configured on the VM
to pull from `${distro_id}:${distro_codename}-security` only. That's the
sudoers contract — even if the web login is compromised, the attacker
can at most trigger a security-patch run, not install arbitrary packages.

The heavy work (running unattended-upgrade) happens in a background
thread so the HTTP request returns immediately. Status is kept in a
module-level dict behind a Lock.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

# ── background-job state ───────────────────────────────────────────
_state_lock = threading.Lock()
_state: dict = {
    'running': False,
    'started_at': None,   # ISO8601
    'finished_at': None,  # ISO8601
    'exit_code': None,    # int or None
    'log': '',            # stdout+stderr
    'last_action': None,  # 'check' | 'apply' | None
}

REBOOT_FLAG = Path('/var/run/reboot-required')
UU_LOG = Path('/var/log/unattended-upgrades/unattended-upgrades.log')


def _set_state(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def get_job_state() -> dict:
    with _state_lock:
        return dict(_state)


# ── status readers (cheap, no sudo) ────────────────────────────────
def reboot_required() -> bool:
    return REBOOT_FLAG.is_file()


def unattended_upgrades_available() -> bool:
    """True if the `unattended-upgrade` binary exists on this system."""
    return Path('/usr/bin/unattended-upgrade').is_file()


def read_last_run() -> dict:
    """Parse the tail of /var/log/unattended-upgrades/unattended-upgrades.log
    to find when the last automatic run happened and how many packages it
    upgraded. This is best-effort — if the log is missing, unreadable, or
    the format changes, we just return empty fields.

    The log lives at a root:adm-owned path with mode 640, so the ev-tracker
    user usually cannot read it directly. That is NOT a failure — we fall
    back to "never" gracefully and rely on the `pending_count` field from
    the dry-run instead.
    """
    result = {'last_run': None, 'last_upgraded': 0}
    try:
        # Tail the last ~200 lines — plenty for the most recent run
        with open(UU_LOG, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-200:]
    except (FileNotFoundError, PermissionError, OSError):
        return result

    ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
    for line in reversed(lines):
        m = ts_re.match(line)
        if m:
            result['last_run'] = m.group(1)
            break

    # Count lines like "Packages that will be upgraded: foo bar baz"
    # or "Unattended-upgrades: Packages that were upgraded: ..."
    for line in reversed(lines):
        if 'Packages that were upgraded' in line or 'packages upgraded' in line.lower():
            nums = re.findall(r'\b(\d+)\b', line)
            if nums:
                try:
                    result['last_upgraded'] = int(nums[0])
                except ValueError:
                    pass
            break
    return result


def count_pending_security_updates() -> int:
    """Use apt-get --just-print to list what unattended-upgrade WOULD do
    on a dry run. Runs under sudo because the APT lists are readable to
    root only on some configs.
    """
    if not unattended_upgrades_available():
        return 0
    try:
        proc = subprocess.run(
            ['sudo', '-n', '/usr/bin/unattended-upgrade', '--dry-run', '-v'],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or '') + '\n' + (proc.stderr or '')
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0

    # Look for lines like:
    #   "Packages that will be upgraded: foo bar baz"
    #   "pkgs that look like they should be upgraded: ..."
    for line in out.splitlines():
        low = line.lower()
        if 'packages that will be upgraded' in low or 'look like they should' in low:
            pkgs = line.split(':', 1)[-1].strip()
            if not pkgs:
                return 0
            return len(pkgs.split())
    return 0


# ── status endpoint payload ────────────────────────────────────────
def get_status() -> dict:
    last = read_last_run()
    return {
        'available': unattended_upgrades_available(),
        'last_run': last['last_run'],
        'last_upgraded': last['last_upgraded'],
        'pending_count': count_pending_security_updates(),
        'reboot_required': reboot_required(),
        'job': get_job_state(),
    }


# ── background job runner ──────────────────────────────────────────
def _run_apply() -> None:
    started = datetime.now().isoformat(timespec='seconds')
    _set_state(
        running=True, started_at=started, finished_at=None,
        exit_code=None, log='', last_action='apply',
    )
    try:
        proc = subprocess.run(
            ['sudo', '-n', '/usr/bin/unattended-upgrade', '-v'],
            capture_output=True, text=True, timeout=30 * 60,
        )
        log = (proc.stdout or '') + (proc.stderr or '')
        _set_state(
            running=False,
            finished_at=datetime.now().isoformat(timespec='seconds'),
            exit_code=proc.returncode,
            log=log[-20_000:],  # cap to 20 KB
        )
    except subprocess.TimeoutExpired as e:
        _set_state(
            running=False,
            finished_at=datetime.now().isoformat(timespec='seconds'),
            exit_code=124,
            log=f'Timeout nach 30 Minuten:\n{e}',
        )
    except Exception as e:
        _set_state(
            running=False,
            finished_at=datetime.now().isoformat(timespec='seconds'),
            exit_code=-1,
            log=f'Fehler: {type(e).__name__}: {e}',
        )


def start_apply() -> bool:
    """Start unattended-upgrade in a background thread. Returns False if
    a job is already running (caller should show a friendly error)."""
    with _state_lock:
        if _state['running']:
            return False
        _state['running'] = True  # reserve the slot early
    threading.Thread(target=_run_apply, daemon=True).start()
    return True


def schedule_reboot(delay_seconds: int = 5) -> tuple[bool, str]:
    """Schedule a reboot via sudo shutdown -r. Returns (ok, message)."""
    def _do():
        time.sleep(max(1, delay_seconds))
        try:
            subprocess.run(
                ['sudo', '-n', '/sbin/shutdown', '-r', 'now'],
                timeout=10,
            )
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()
    return True, f'Reboot in {delay_seconds} Sekunden geplant.'
