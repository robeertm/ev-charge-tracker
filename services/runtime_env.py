# -*- coding: utf-8 -*-
"""Am I running inside a container? — the one place that answers it.

This decides how an update is applied, and getting it wrong breaks the
update rather than merely making it ugly.

``updater.apply_update`` has two paths. Under systemd it swaps the files
**in this process** and lets the service come back. Everywhere else it
spawns a **detached helper** that waits for this process to die and then
does the swap from outside.

In a container the helper path cannot work. The app is PID 1, and when
PID 1 exits the container stops and takes every other process with it —
including a helper caught halfway through replacing files. The published
image has no systemd, no ``INVOCATION_ID`` and no ``/run/systemd/system``,
so it used to answer "not systemd" and go down exactly that path.

The private image behind the Ioniq 6 host does not hit this because its
entrypoint sets ``INVOCATION_ID`` by hand for this very reason. That is a
fix living in one machine's Dockerfile; the detection belongs in the app,
where every container install gets it.
"""
from __future__ import annotations

import os
from pathlib import Path

_cached = None


def in_container() -> bool:
    """True when this process runs inside a Docker/Podman container.

    Three signals, because none of them holds everywhere: Docker writes
    ``/.dockerenv``, Podman writes ``/run/.containerenv``, and both leave
    their runtime's name in PID 1's cgroup line. Checked once — the answer
    cannot change while the process lives.
    """
    global _cached
    if _cached is not None:
        return _cached
    _cached = _detect()
    return _cached


def _detect() -> bool:
    for marker in ('/.dockerenv', '/run/.containerenv'):
        try:
            if Path(marker).exists():
                return True
        except Exception:
            pass
    try:
        cgroup = Path('/proc/1/cgroup').read_text()
        if any(tag in cgroup for tag in ('docker', 'containerd', 'kubepods', 'libpod')):
            return True
    except Exception:
        pass
    # Escape hatch for a runtime none of the above recognises.
    return os.environ.get('EV_IN_CONTAINER', '') == '1'
