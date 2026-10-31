"""The commit identity check (scripts/check_commit_identity.py) keeps private
addresses out of the public history.  Placeholder addresses only."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("check_commit_identity", ROOT / "scripts" / "check_commit_identity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_noreply_addresses_pass():
    m = _mod()
    assert m.problem("Robert Manuwald", "robeertm@users.noreply.github.com") is None
    assert m.problem("GitHub", "noreply@github.com") is None
    assert m.problem("Release Bot", "noreply@github.com") is None


def test_maintainer_with_a_personal_mailbox_is_refused():
    m = _mod()
    assert m.problem("Robert Manuwald", "someone@example.com")
    assert m.problem("Robert", "git@example.org")
    assert m.problem("Anyone", "robeertm@example.net")


def test_machine_placeholders_are_refused():
    m = _mod()
    assert m.problem("CI Agent", "agent@ci")
    assert m.problem("Bot", "bot@build.local")
    assert m.problem("Job", "job@localhost")
    assert m.problem("Empty", "")


def test_outside_contributors_are_left_alone():
    m = _mod()
    assert m.problem("Jane Doe", "jane@example.com") is None
    assert m.problem("contributor", "12345+contributor@users.noreply.github.com") is None


def test_pre_push_ranges():
    m = _mod()
    z = m.ZERO
    a, b = "a" * 40, "b" * 40
    assert m._pre_push_ranges([f"refs/heads/main {a} refs/heads/main {b}\n"]) == [[f"{b}..{a}"]]
    assert m._pre_push_ranges([f"refs/heads/x {a} refs/heads/x {z}\n"]) == [[a, "--not", "--remotes"]]
    assert m._pre_push_ranges([f"(delete) {z} refs/heads/x {b}\n"]) == []
