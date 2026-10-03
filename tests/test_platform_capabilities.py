"""The settings page must offer only what this install can actually do.

Cards used to be written for one shape of machine — a Debian VM that held
its own certificate, could ask apt for security updates and could reboot
itself. On a container none of that is true, and the page offered it
anyway: a security-updates card on a system without apt, a reboot button
with nothing to reboot, and a "fetch token" button that would pip-install
Selenium into a layer the next image pull throws away and then fail for
want of a browser.

``services/platform_service.py`` answers each of those questions on its
own terms. This pins the answers, and in particular the one that was got
wrong three times: whether the app's own HTTPS card is worth showing.

Run with:
  python3 tests/test_platform_capabilities.py
Exit code is non-zero if any check fails.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import services.platform_service as ps  # noqa: E402

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


print("\n== the HTTPS card: shown unless something in front does TLS ==")
check(ps.own_tls_card_useful({}) is True,
      "no forwarding headers: the card is shown")
check(ps.own_tls_card_useful({'X-Forwarded-Proto': 'https'}) is False,
      "a proxy that terminated TLS: the card is hidden")
# 🔑 192.0.2.x is the documentation range (TEST-NET-1) and is used on
# purpose: the value is irrelevant here — the check is about the header
# being present at all — and a real-looking address in a public
# repository is a thing a leak scanner has to flag, because it cannot
# tell an example from somebody's actual machine.
check(ps.own_tls_card_useful({'X-Forwarded-For': '192.0.2.1'}) is False,
      "any forwarding proxy at all: the card is hidden")
check(ps.own_tls_card_useful({'User-Agent': 'x'}) is True,
      "unrelated headers do not hide it")

# 🔴 The failure direction matters more than the happy path: a settings
# page that hides a switch somebody needs is worse than one that shows a
# switch they do not. Anything unreadable must therefore answer "show".


class _Kaputt:
    def get(self, _):
        raise RuntimeError('headers unreadable')


check(ps.own_tls_card_useful(_Kaputt()) is True,
      "unreadable headers fall back to showing the card")


print("\n== the capability checks answer about themselves ==")
caps = ps.capabilities({})
for key in ('system_update', 'reboot_host', 'browser_token', 'own_tls', 'container'):
    check(key in caps, f"capabilities() answers about {key}")
check(all(isinstance(v, bool) for v in caps.values()),
      "every answer is a plain bool the template can test")

# These two depend on the machine running the test, so pin the LOGIC
# rather than the result: both need sudo, and nothing has sudo unless it
# is really there.
import shutil  # noqa: E402

if shutil.which('sudo') is None:
    check(ps.can_system_update() is False,
          "without sudo there is no security-update button")
    check(ps.can_reboot_host() is False,
          "without sudo there is no reboot button")
else:
    print("  (this machine has sudo — the no-sudo branch is covered on CI "
          "and in the container, where it was measured as False)")

print("\n== the browser question is about the browser ==")
# token_fetch installs Selenium itself, so asking about the package would
# answer "yes" everywhere and mean nothing.
check(ps.can_browser_token() == any(shutil.which(b) for b in ps._BROWSERS),
      "it answers exactly: is there a browser binary")

print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
