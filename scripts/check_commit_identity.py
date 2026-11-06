#!/usr/bin/env python3
"""Refuse commits whose author or committer would publish a private address.

This repository is public, and every commit carries two identities (author and
committer) that GitHub shows to anyone.  Commits by the maintainer and by the
maintainer's automation must use the GitHub no-reply address, never a personal
mailbox, a company domain or a machine-local placeholder such as
``agent@host`` or ``bot@box.local``.

Rules, checked for author AND committer of every commit in the given range:

* ``*@users.noreply.github.com`` and ``noreply@github.com`` are always fine.
* An address whose domain has no dot, or ends in ``.local`` / ``.localdomain``
  / ``localhost``, is refused: it is a machine placeholder from some tool.
* An identity that names the maintainer by GitHub handle, or by one of the
  names listed in the optional file ``~/.ev-tracker-maintainer`` (one name per
  line, ``#`` for comments; kept outside the repository on purpose), is
  refused unless it uses a GitHub no-reply address.  Without that file only the
  handle is checked, so CI runs need nothing private.
* Everyone else (outside contributors) is left alone: their address is theirs.

Usage::

    python scripts/check_commit_identity.py origin/main..HEAD
    python scripts/check_commit_identity.py --pre-push origin <url> < refs   # git hook

Install the hook once per clone::

    git config core.hooksPath .githooks

The same check runs in CI on every push (``.github/workflows/commit-identity.yml``).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MAINTAINER_HANDLE = "robeertm"
MAINTAINER_EMAIL = f"{MAINTAINER_HANDLE}@users.noreply.github.com"
NOREPLY_DOMAIN = "users.noreply.github.com"
#: Optional, outside the repository: further names that identify the maintainer.
NAMES_FILE = "~/.ev-tracker-maintainer"
ZERO = "0" * 40


def maintainer_names() -> set:
    """The handle plus names from the optional private file, lower-cased."""
    names = {MAINTAINER_HANDLE}
    path = Path(os.environ.get("EV_MAINTAINER_FILE") or os.path.expanduser(NAMES_FILE))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return names
    names |= {ln.strip().lower() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}
    return names


def problem(name: str, email: str) -> Optional[str]:
    """Return why this identity must not be published, or None when it is fine."""
    n = (name or "").strip().lower()
    e = (email or "").strip().lower()
    if e == "noreply@github.com" or e.endswith("@" + NOREPLY_DOMAIN):
        return None
    domain = e.rsplit("@", 1)[-1] if "@" in e else ""
    if not domain or "." not in domain or domain == "localhost" or domain.endswith(
        (".local", ".localdomain", ".localhost")
    ):
        return "machine-local address"
    names_maintainer = n in maintainer_names() or MAINTAINER_HANDLE in e.split("@", 1)[0]
    if names_maintainer:
        return "maintainer must use the GitHub no-reply address"
    return None


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def commits(rev_args: Iterable[str]) -> List[Tuple[str, str, str, str, str]]:
    out = _git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", *rev_args)
    return [tuple(line.split("\x00")) for line in out.splitlines() if line]  # type: ignore[misc]


def check(rev_args: Iterable[str]) -> List[str]:
    bad = []
    for sha, an, ae, cn, ce in commits(rev_args):
        for role, name, email in (("author", an, ae), ("committer", cn, ce)):
            why = problem(name, email)
            if why:
                bad.append(f"{sha[:10]} {role} {name} <{email}>: {why}")
    return bad


def _pre_push_ranges(lines: Iterable[str]) -> List[List[str]]:
    ranges = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == ZERO:  # deleting a ref
            continue
        if remote_sha == ZERO:
            ranges.append([local_sha, "--not", "--remotes"])
        else:
            ranges.append([f"{remote_sha}..{local_sha}"])
    return ranges


def main(argv: List[str]) -> int:
    if argv and argv[0] == "--pre-push":
        ranges = _pre_push_ranges(sys.stdin)
    else:
        ranges = [argv or ["@{upstream}..HEAD"]]
    bad: List[str] = []
    for r in ranges:
        try:
            bad += check(r)
        except subprocess.CalledProcessError as exc:  # unknown remote sha etc.
            print(f"commit identity check: cannot read {' '.join(r)}: {exc.stderr.strip()}", file=sys.stderr)
            return 1
    if bad:
        print("Refusing: these commits would publish a private or placeholder identity:", file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        print(
            f"Fix: git config user.email {MAINTAINER_EMAIL}\n"
            "     then rewrite the commits (e.g. git rebase -r <base> --exec 'git commit --amend --no-edit --reset-author').",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
