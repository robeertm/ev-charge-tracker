# -*- coding: utf-8 -*-
"""Restarting the app, on every kind of host it runs on.

There were three copies of this routine in app.py — backup import,
connector install, factory reset — and only the third had learned that
``sudo -n systemctl restart`` does nothing in a container. The other two
called it, swallowed the failure and answered "App startet neu …" to a
browser whose app was not going to restart at all. For the connector
install that was the whole bug the user saw: pip had really installed the
package, but the registry is populated at import time, so without a fresh
interpreter the brand stayed unknown.

So: one function, three callers.

The order matters.

1. **systemd**, when the sudoers rule is there. This is the native path on
   the VMs, and it is the only one that re-reads the unit file — a unit
   whose environment or user changed needs a real restart, not a re-exec.

2. **``os.execv`` otherwise.** This is the universal fallback and it does
   not need a supervisor, a restart policy or an init system: the process
   replaces itself with a fresh interpreter, which is exactly what the
   callers are after (module-level ``register(...)`` calls run again, so
   a freshly installed connector appears). It works inside a container,
   under ``start.sh`` on a laptop, and under a supervisor.

   ``os._exit(0)`` — what the factory-reset path did — only works where
   something outside restarts us. In ``docker run`` without a restart
   policy it leaves the user with a stopped container and no app, which
   is a worse outcome than the silent no-op it replaced.

One thing PEP 446 does NOT do for us: Werkzeug's server marks its
listening socket inheritable (it hands the descriptor to the reloader
that way). Left alone, the replacement process inherits a socket already
bound to 7654 and dies on "Address already in use" — proven by running
it, not by reading about it. So every descriptor is flagged
close-on-exec first; see _seal_descriptors().
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

SERVICE_NAME = 'ev-tracker.service'


def systemd_restart_available() -> bool:
    """True when this host can restart the app through systemd.

    Asks sudo itself (``-n``, never prompt) instead of guessing from
    ``/run/systemd/system``: the sudoers rule is the thing that actually
    decides, and a host can have systemd but not the rule.
    """
    if not sys.platform.startswith('linux'):
        return False
    try:
        r = subprocess.run(
            ['sudo', '-n', '-l', '/bin/systemctl', 'restart', SERVICE_NAME],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def restart_now(reason: str = '') -> None:
    """Restart this process. Does not return when it succeeds."""
    if reason:
        logger.info(f"Restarting app: {reason}")
    try:
        r = subprocess.run(
            ['sudo', '-n', '/bin/systemctl', 'restart', SERVICE_NAME],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            # systemd is on its way to SIGTERM us. Give it a moment; if it
            # turns out the unit was not actually running under systemd
            # (a sudo shim in some images answers 0 too), fall through to the
            # re-exec below rather than sitting here for good.
            time.sleep(3.0)
    except Exception as exc:
        logger.info(f"systemd restart unavailable ({exc}) — re-exec instead")

    logger.info("Re-executing %s %s", sys.executable, sys.argv)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    _seal_descriptors()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _seal_descriptors() -> None:
    """Mark every inherited descriptor close-on-exec, except stdio.

    The listening socket is the one that matters: Werkzeug sets it
    inheritable, so without this the replacement process finds port 7654
    already bound by a socket it inherited from itself and exits with
    "Address already in use" — which in a container means the app is
    simply gone.

    Flags are set rather than the descriptors closed, so if the exec
    fails for some other reason this process is still alive and serving.
    stdin/stdout/stderr (0-2) are left alone; the new process needs them
    and the container's log stream hangs off them.
    """
    try:
        fds = [int(name) for name in os.listdir('/proc/self/fd')]
    except Exception:
        # No procfs (macOS, BSD). A bounded sweep — the real limit can be
        # 1 048 576, and walking that costs more than it can ever find.
        try:
            hard = os.sysconf('SC_OPEN_MAX')
        except Exception:
            hard = 4096
        fds = list(range(3, min(int(hard), 4096)))

    for fd in fds:
        if fd < 3:
            continue
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        except OSError:
            pass                      # already closed, or not ours


def schedule_restart(delay: float = 1.0, reason: str = '') -> None:
    """Restart after ``delay`` seconds, so the HTTP response flushes first.

    The caller answers the browser and *then* the app goes away. Without
    the delay the socket dies mid-response and the user sees a connection
    error instead of "installed".
    """
    def _run():
        time.sleep(delay)
        try:
            restart_now(reason)
        except Exception as exc:                  # pragma: no cover
            logger.error(f"Restart failed: {exc}")

    threading.Thread(target=_run, daemon=True, name='app-restart').start()
