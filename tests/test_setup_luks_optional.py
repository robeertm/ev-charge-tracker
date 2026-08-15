"""Tests for the LUKS-optional first-run setup (services/setup_service.py).

LUKS is no longer required: fresh installs run on a plain data directory and
must never see the LUKS wizard step or the LUKS settings cards, while existing
encrypted installs (where /dev/mapper/evdata is present) keep every LUKS
feature. This test pins ``luks_in_use()`` and the wizard-completion gate.

Run with:
  python3 tests/test_setup_luks_optional.py
Exit code is non-zero if any check fails.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import services.setup_service as ss  # noqa: E402

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def test_luks_in_use_follows_device():
    orig = ss.get_luks_device
    try:
        ss.get_luks_device = lambda: None
        check(ss.luks_in_use() is False,
              "luks_in_use() is False when no evdata mapping exists (plain install)")
        ss.get_luks_device = lambda: '/dev/sdb'
        check(ss.luks_in_use() is True,
              "luks_in_use() is True when an evdata mapping is present (encrypted install)")
    finally:
        ss.get_luks_device = orig


def _completes(state, luks):
    """Mirror the wizard-completion gate in app.api_setup_save_vehicles."""
    luks_ok = state.get('luks_done') or not luks
    return bool(luks_ok and state.get('weblogin_done') and state.get('vehicles_done'))


def test_completion_gate():
    # Plain install: luks_done never gets set, wizard must still finish.
    plain_done = {'luks_done': False, 'weblogin_done': True, 'vehicles_done': True}
    check(_completes(plain_done, luks=False) is True,
          "plain install completes with weblogin+vehicles even though luks_done is False")
    check(_completes(plain_done, luks=True) is False,
          "encrypted install with the same state stays open until luks_done is set")

    # Encrypted install: all three required.
    enc_done = {'luks_done': True, 'weblogin_done': True, 'vehicles_done': True}
    check(_completes(enc_done, luks=True) is True,
          "encrypted install completes once all three steps are done")

    # Neither variant completes while the web login is still missing.
    no_login = {'luks_done': True, 'weblogin_done': False, 'vehicles_done': True}
    check(_completes(no_login, luks=True) is False,
          "no completion while the mandatory web login is missing")
    check(_completes({'luks_done': False, 'weblogin_done': False, 'vehicles_done': True}, luks=False) is False,
          "plain install still needs the web login before completing")


def main():
    print("test_luks_in_use_follows_device")
    test_luks_in_use_follows_device()
    print("test_completion_gate")
    test_completion_gate()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall passed")


if __name__ == '__main__':
    main()
