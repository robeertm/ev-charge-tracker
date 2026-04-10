"""Standalone updater helper for EV Charge Tracker.

Runs as a detached subprocess after the main app has triggered an update.
Waits for the parent Flask process to exit, swaps files from a staging
directory into the app directory, refreshes Python dependencies in the
venv, and re-launches the app via the platform start script.

This file is intentionally dependency-free (stdlib only) so it can run
even if the venv is broken or Flask is missing.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List


# Files / directories that must NEVER be overwritten by an update.
EXCLUDE_NAMES = {
    'venv',           # python virtualenv created by start.command
    '.venv',
    'data',           # sqlite database lives here
    'logs',
    '.git',
    '.github',
    '__pycache__',
    'updates',        # staging area used by the updater itself
    'backup_*',
}


def _ensure_executable(p: Path) -> None:
    """Ensure a script is executable (macOS/Linux)."""
    try:
        if not p.exists():
            return
        mode = p.stat().st_mode
        p.chmod(mode | 0o111)
    except Exception:
        pass


def _spawn_detached(cmd: List[str], cwd: Path) -> None:
    """Spawn a process fully detached from the current session."""
    try:
        if os.name == 'nt':
            subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=False,
            )
        else:
            subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as e:
        print(f"[updater] spawn failed: {e}", file=sys.stderr)


def _clear_quarantine(target: Path) -> None:
    """Best-effort remove macOS Gatekeeper quarantine attributes."""
    try:
        if sys.platform != 'darwin':
            return
        subprocess.run(['xattr', '-dr', 'com.apple.quarantine', str(target)], check=False)
    except Exception:
        pass


def _restart_app(app_dir: Path) -> None:
    """Re-launch the app as a fully-detached background daemon.

    The challenge on macOS: Terminal.app sends SIGHUP to *every* process in
    its session when the window closes — even processes that called setsid.
    To survive that, the new Python process must be wrapped in ``nohup``
    (which sets SIG_IGN for SIGHUP) AND placed in its own session.

    We bypass start.sh entirely because the redundant pip install + browser
    open + ``set -e`` shell pitfalls add ~10 s of latency and several ways
    to fail silently. The helper already ran pip install with the new
    requirements.txt, so the venv is ready.
    """
    # Give the kernel a moment to release the listening port.
    try:
        time.sleep(2.0)
    except Exception:
        pass

    log_path = app_dir / 'updates' / 'restart.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = open(log_path, 'a', buffering=1)
    except Exception:
        log_fh = None

    def _log(msg):
        if log_fh:
            log_fh.write(f"[restart {time.strftime('%H:%M:%S')}] {msg}\n")

    _log(f"=== restart attempt for {app_dir} ===")

    py = _venv_python(app_dir)
    app_py = app_dir / 'app.py'
    _log(f"venv python: {py}")
    _log(f"app.py exists: {app_py.exists()}")

    if py and app_py.exists():
        # Preferred path: nohup wrap so SIGHUP from Terminal close is ignored,
        # plus start_new_session so we leave the parent's process group entirely.
        nohup = '/usr/bin/nohup' if os.path.exists('/usr/bin/nohup') else 'nohup'
        if os.name == 'nt':
            cmd = [str(py), str(app_py)]
        else:
            cmd = [nohup, str(py), str(app_py)]
        _log(f"spawning: {' '.join(cmd)}")

        # Strip Werkzeug reloader env vars: they propagate from the dying
        # Flask parent through this updater chain and crash the freshly-
        # spawned Flask with EBADF when it tries to inherit the dead FD.
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith('WERKZEUG_')}

        try:
            if os.name == 'nt':
                p = subprocess.Popen(
                    cmd,
                    cwd=str(app_dir),
                    env=clean_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh or subprocess.DEVNULL,
                    stderr=log_fh or subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=False,
                )
            else:
                p = subprocess.Popen(
                    cmd,
                    cwd=str(app_dir),
                    env=clean_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh or subprocess.DEVNULL,
                    stderr=log_fh or subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            _log(f"spawned PID {p.pid}, waiting 4s for startup…")
            # Wait a bit, then verify the process is still alive AND something
            # is listening on the port.
            time.sleep(4.0)
            alive = (p.poll() is None)
            _log(f"PID {p.pid} alive after 4s: {alive}")
            if alive:
                _check_port_listening(7654, log_fh)
                _log("restart successful")
                return
            else:
                _log(f"PID {p.pid} died with exit code {p.returncode}")
        except Exception as e:
            _log(f"spawn exception: {e}")

    # Fallback: spawn start script
    _log("falling back to start script")
    if os.name == 'nt':
        start = app_dir / 'start.bat'
        if start.exists():
            subprocess.Popen(['cmd', '/c', 'start', '', str(start)], cwd=str(app_dir))
        return

    candidates = ['start.command', 'start.sh']
    start = None
    for name in candidates:
        cand = app_dir / name
        if cand.exists():
            start = cand
            break
    if start is None:
        _log("no start script found")
        return

    _ensure_executable(start)
    _ensure_executable(app_dir / 'start.command')
    _ensure_executable(app_dir / 'start.sh')
    _clear_quarantine(app_dir)

    _log(f"fallback spawn: nohup bash {start}")
    try:
        nohup = '/usr/bin/nohup' if os.path.exists('/usr/bin/nohup') else 'nohup'
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith('WERKZEUG_')}
        subprocess.Popen([nohup, '/bin/bash', str(start)], cwd=str(app_dir),
                         env=clean_env,
                         stdin=subprocess.DEVNULL,
                         stdout=log_fh or subprocess.DEVNULL,
                         stderr=log_fh or subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        time.sleep(2.0)
    except Exception as e:
        _log(f"fallback spawn failed: {e}")


def _check_port_listening(port, log_fh):
    """Best-effort check that something has bound the port."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        result = s.connect_ex(('127.0.0.1', port))
        s.close()
        if log_fh:
            log_fh.write(f"[restart {time.strftime('%H:%M:%S')}] port {port} listening: {result == 0}\n")
    except Exception:
        pass


def _wait_for_pid(pid: int, timeout_s: float = 30.0) -> None:
    """Block until the process with `pid` exits, or timeout."""
    if pid <= 0:
        return
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if os.name != 'nt':
                os.kill(pid, 0)  # signal 0 = "still alive?"
            time.sleep(0.25)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        except Exception:
            return
    # Timeout: try to proceed anyway


def _safe_rmtree(p: Path) -> None:
    try:
        if p.exists():
            shutil.rmtree(p)
    except Exception as e:
        print(f"[updater] rmtree failed for {p}: {e}", file=sys.stderr)


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        _safe_rmtree(dst)
    shutil.copytree(src, dst)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _venv_python(app_dir: Path) -> Path | None:
    """Locate the venv python interpreter for the app, if any."""
    for vname in ('venv', '.venv'):
        v = app_dir / vname
        if not v.exists():
            continue
        if os.name == 'nt':
            py = v / 'Scripts' / 'python.exe'
        else:
            py = v / 'bin' / 'python'
        if py.exists():
            return py
    return None


def _is_excluded(name: str) -> bool:
    if name in EXCLUDE_NAMES:
        return True
    if name.startswith('backup_'):
        return True
    if name.endswith('.pyc') or name == '.DS_Store':
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--app-dir', required=True)
    ap.add_argument('--staging-dir', default='',
                    help='Staging dir for an update. Omit for restart-only mode.')
    ap.add_argument('--wait-pid', type=int, default=0)
    ap.add_argument('--update-deps', type=int, default=1)
    ap.add_argument('--restart', type=int, default=1)
    args = ap.parse_args()

    app_dir = Path(args.app_dir).resolve()
    staging = Path(args.staging_dir).resolve() if args.staging_dir else None

    print(f"[updater] app_dir={app_dir}", file=sys.stderr)
    if staging:
        print(f"[updater] staging={staging}", file=sys.stderr)
    else:
        print("[updater] restart-only mode (no staging dir)", file=sys.stderr)
    print(f"[updater] waiting for pid {args.wait_pid}", file=sys.stderr)
    _wait_for_pid(int(args.wait_pid or 0))

    # File swap is skipped entirely in restart-only mode
    if staging is not None:
        if not staging.exists():
            print(f"[updater] staging dir missing: {staging}", file=sys.stderr)
            return 1

        print("[updater] swapping files…", file=sys.stderr)
        for item in staging.iterdir():
            name = item.name
            if _is_excluded(name):
                continue
            dst = app_dir / name
            try:
                if item.is_dir():
                    _copy_tree(item, dst)
                else:
                    _copy_file(item, dst)
            except Exception as e:
                print(f"[updater] failed to copy {name}: {e}", file=sys.stderr)

        # Update Python dependencies
        if int(args.update_deps or 0) == 1:
            req = app_dir / 'requirements.txt'
            py = _venv_python(app_dir)
            if py and req.exists():
                print("[updater] running pip install -r requirements.txt…", file=sys.stderr)
                try:
                    subprocess.run(
                        [str(py), '-m', 'pip', 'install', '-r', str(req)],
                        check=False,
                    )
                except Exception as e:
                    print(f"[updater] pip install failed: {e}", file=sys.stderr)

        # GitHub source zips strip the POSIX exec bit, so start scripts
        # come out non-executable. Restore the bit so `./start.sh` works
        # on Linux/macOS after an update. (No-op on Windows.)
        if os.name != 'nt':
            for script in ('start.sh', 'start.command'):
                _ensure_executable(app_dir / script)

        # Clean up staging now that the install is complete
        _safe_rmtree(staging)

    # Restart
    if int(args.restart or 0) == 1:
        print("[updater] restarting app…", file=sys.stderr)
        try:
            _restart_app(app_dir)
        except Exception as e:
            print(f"[updater] restart failed: {e}", file=sys.stderr)
            return 2

    print("[updater] done.", file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
