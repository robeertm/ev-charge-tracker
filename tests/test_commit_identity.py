"""The commit identity check (scripts/check_commit_identity.py) keeps private
addresses out of the public history.  Placeholder addresses only."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("check_commit_identity", ROOT / "scripts" / "check_commit_identity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_noreply_addresses_pass():
    m = _mod()
    assert m.problem("Maintainer", m.MAINTAINER_EMAIL) is None
    assert m.problem("Maintainer", "1234+" + m.MAINTAINER_EMAIL) is None
    assert m.problem("GitHub", "noreply@github.com") is None
    assert m.problem("Release Bot", "noreply@github.com") is None


def test_maintainer_handle_with_a_personal_mailbox_is_refused():
    m = _mod()
    assert m.problem("robeertm", "someone@example.com")
    assert m.problem("Anyone", m.MAINTAINER_HANDLE + "@example.net")


def test_names_from_the_private_file_are_refused(tmp_path):
    f = tmp_path / "names"
    f.write_text("# comment\nJane Maintainer\n", encoding="utf-8")
    old = os.environ.get("EV_MAINTAINER_FILE")
    os.environ["EV_MAINTAINER_FILE"] = str(f)
    try:
        m = _mod()
        assert m.problem("Jane Maintainer", "someone@example.com")
        assert m.problem("Jane Maintainer", "jane@users.noreply.github.com") is None
        assert m.problem("Other Person", "other@example.com") is None
    finally:
        if old is None:
            os.environ.pop("EV_MAINTAINER_FILE")
        else:
            os.environ["EV_MAINTAINER_FILE"] = old


def test_without_the_file_only_the_handle_counts(tmp_path):
    old = os.environ.get("EV_MAINTAINER_FILE")
    os.environ["EV_MAINTAINER_FILE"] = str(tmp_path / "missing")
    try:
        m = _mod()
        assert m.problem("Jane Maintainer", "someone@example.com") is None
        assert m.problem("robeertm", "someone@example.com")
    finally:
        if old is None:
            os.environ.pop("EV_MAINTAINER_FILE")
        else:
            os.environ["EV_MAINTAINER_FILE"] = old


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
