# -*- coding: utf-8 -*-
"""Automatic or manual updates — and what the interface may say about it.

Where updates arrive on their own (a release moves the ``latest`` tag
after the smoke test, and a watcher container recreates this one), a
"version X is available" banner and a push notification are pure noise:
they announce something nobody has to act on, and they train the owner to
ignore the one channel that should matter.

So an install can declare how it is updated:

``EV_UPDATE_MODE=auto``
    Updates install themselves. No notification, no banner, no install
    button. The settings page still names the new version and links the
    release notes — knowing is useful, being nagged is not.

``manual`` (the default)
    Unchanged behaviour: the notification, the banner and the button.

🔑 **The environment is the authority, not a stored setting.** How an
install receives updates is a property of how it was deployed, which
lives in the compose file next to the image tag — the same reasoning as
``EV_UPDATE_HOLD``. A stored value can still set it where there is no
compose file to edit, but it never overrules the environment.

🔑 **This decides what is SAID, never what is allowed.** The charging
guard (``services.charge_gate``) and the update hold
(``services.update_hold``) are untouched by it: an automatic install that
is charging still waits, and a held install still refuses. Mixing "how
loud are we" with "may this happen" is how a display preference ends up
disabling a protection.
"""
import os

ENV_MODE = 'EV_UPDATE_MODE'
CONFIG_KEY = 'update_mode'
AUTOMATIC = 'auto'
MANUAL = 'manual'


def _normalise(wert: str) -> str:
    w = (wert or '').strip().lower()
    if w in ('auto', 'automatic', 'automatisch', 'watchtower', 'image'):
        return AUTOMATIC
    if w in ('manual', 'manuell', 'button', 'click'):
        return MANUAL
    return ''


def mode() -> str:
    """``'auto'`` or ``'manual'``. Unknown values fall back to manual:
    a typo must not silently switch off the notice somebody relies on."""
    aus_umgebung = _normalise(os.environ.get(ENV_MODE, ''))
    if aus_umgebung:
        return aus_umgebung
    try:
        from models.database import AppConfig
        gespeichert = _normalise(AppConfig.get(CONFIG_KEY, '') or '')
    except Exception:
        gespeichert = ''
    return gespeichert or MANUAL


def is_automatic() -> bool:
    return mode() == AUTOMATIC
