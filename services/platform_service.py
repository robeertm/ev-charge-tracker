"""What can this installation actually do?

The settings page grew up on one kind of machine: a Debian VM where the
app was the only thing running, held its own TLS certificate, could ask
apt for security updates and could reboot the host. None of that is true
in a container, and some of it was never true on a Raspberry Pi install
either — but the page offered all of it anyway, so people got cards that
promised Debian security updates on a system without apt, and a reboot
button with nothing to reboot.

This module answers those questions by looking, not by guessing which
kind of install this is. "Is there a container here" is the wrong
question: a native install without sudo cannot reboot either, and a
container that one day ships a browser could fetch a token just fine. So
each answer is about the one capability it names.

🔴 Every check is cheap and must stay cheap: the settings page calls all
of them on every render. No subprocesses, no network — only the presence
of a file, which the kernel answers from cache.

Mirrors the shape of ``setup_service.luks_in_use()``, which has gated the
LUKS cards the same way since v3.0.94.
"""
import shutil
from pathlib import Path

#: Browsers the one-off Kia/Hyundai token fetch could drive. It needs a
#: real browser binary — Selenium alone is not enough, and the fetch
#: helper will happily pip-install Selenium into a container that has no
#: browser at all and then fail at the last step.
_BROWSERS = ('chromium', 'chromium-browser', 'google-chrome',
             'google-chrome-stable', 'firefox')


def can_system_update() -> bool:
    """True where the app can run Debian security updates.

    Needs the binary *and* a way to call it as root. The sudoers rule the
    installer writes is what makes that possible; without sudo the button
    is a button that returns an error.
    """
    return (Path('/usr/bin/unattended-upgrade').is_file()
            and shutil.which('sudo') is not None)


def can_reboot_host() -> bool:
    """True where the app can actually reboot the machine it runs on.

    A container has no `shutdown` and no init to talk to; rebooting it
    would mean restarting the container, which is not what the button
    says and not what the user wants.
    """
    if not shutil.which('sudo'):
        return False
    return any(Path(p).exists() for p in ('/sbin/shutdown', '/usr/sbin/shutdown'))


def can_browser_token() -> bool:
    """True where the one-off Kia/Hyundai browser token fetch can work.

    🔑 The question is the BROWSER, not the Python package. ``token_fetch``
    installs Selenium itself when it is missing — in a container that
    writes into a layer the next image pull throws away, and then fails
    anyway because there is nothing to drive.
    """
    return any(shutil.which(b) for b in _BROWSERS)


def own_tls_card_useful(headers) -> bool:
    """Is the app's own HTTPS setting worth showing on this install?

    🔴 Three wrong answers were tried before this one:

    * *the client is on the tailnet* — the original. It asked the wrong
      side: the same install showed the card to a LAN browser and hid it
      from a tailnet one, and behind a sidecar proxy the request arrives
      from a Docker address, so it came back for everyone.
    * *a certificate file exists* — measured wrong on a real install: a
      host migrated from a VM still carried ``data/ssl/server.crt`` from
      a life where it did serve its own TLS. Files that exist are not a
      service that runs.
    * *HTTPS is currently on* — that hides the only switch that turns it
      ON. A setting page may not lock its own door.

    What is actually true and cheap to know: whether this request came
    through a proxy. Something in front that forwards for us is also the
    thing terminating TLS — `tailscale serve`, nginx, a tunnel — and then
    the app's own certificate is never what anyone reaches it through.

    🔑 The failure direction is deliberate: no forwarding headers means
    the card is SHOWN. Hiding a setting someone needs is worse than
    showing one they do not.
    """
    try:
        hinter_proxy = any(headers.get(h) for h in
                           ('X-Forwarded-Proto', 'X-Forwarded-For', 'Forwarded'))
    except Exception:
        return True
    return not hinter_proxy


def in_container() -> bool:
    """Best-effort: are we inside a container?

    🔴 Not used to decide what the UI offers — the capability checks above
    do that, and they stay right when a container grows a capability or a
    bare-metal install loses one. This is here for the App-Info card, so
    somebody reading a bug report can tell which shape of install they are
    looking at.

    🔑 It forwards to ``runtime_env``, which calls itself "the one place
    that answers it" and means it: the update path picks its strategy
    from that answer. This function first carried its own copy of the
    detection, and the copy was already weaker — it missed containerd,
    kubepods and Podman, and it accepted any non-empty ``EV_IN_CONTAINER``
    where the real one wants ``1``. Two answers to one question is the
    whole bug; there is now one.
    """
    from services.runtime_env import in_container as _echt
    return _echt()


def capabilities(headers=None) -> dict:
    """Everything above in one dict, for the template and for /api.

    ``headers`` is the current request's headers where there is one; the
    HTTPS answer depends on how this very request arrived.
    """
    return {
        'system_update': can_system_update(),
        'reboot_host': can_reboot_host(),
        'browser_token': can_browser_token(),
        'own_tls': own_tls_card_useful(headers if headers is not None else {}),
        'container': in_container(),
    }
