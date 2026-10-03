# -*- coding: utf-8 -*-
"""Nothing in this repository may name a real installation or a real place.

This is a public repository. Comments written while chasing a bug on a
real machine kept its hostname — and in one case the saved location
labels of that machine's trip log, which were a street and a district.
That is other people's data, published, in a file nobody rereads.

Two checks, because the two kinds of leak are found differently:

**Hostnames** are structural. Every host in this project is named
``ev-<something>``, and the legitimate ``something``s are a short,
knowable list: the service, its data, its helpers. Anything else is a
machine belonging to somebody. A new legitimate name has to be added to
the list below, which is the point — it makes somebody decide.

**Place names and people's names** are not structural; nothing about
``Ponytruppe`` looks different from ``Parkplatz``. So the list of them
lives in a file **outside this repository**, which is the only way a
blocklist does not itself publish what it blocks:

    ~/.ev-tracker-private-names      one term per line, # for comments

Without that file this half is skipped. For anyone who is not the
maintainer that is the right answer — it is a guard for whoever knows
which words are real.

Run with:
  python3 tests/test_no_private_names.py
Exit code is non-zero if any check fails.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: ``ev-`` names that belong to the project rather than to a machine.
#: Matched as prefixes, so ``ev-tracker-data`` passes under ``ev-tracker``.
ERLAUBT = (
    'ev-tracker', 'ev-charge', 'ev-charging', 'ev-data', 'ev-unlock',
    'ev-provision', 'ev-updater', 'ev-update', 'ev-luks', 'ev-notify',
    'ev-front', 'ev-station', 'ev-trouble',
    # Placeholders used in the docs and in all six translations to show
    # what a hostname looks like. Deliberately not real.
    'ev-my-name', 'ev-mein-name', 'ev-meine-vm', 'ev-mi-nombre',
    'ev-mon-nom', 'ev-mio-nome', 'ev-mijn-naam',
)

BLOCKLISTE = Path(os.path.expanduser('~/.ev-tracker-private-names'))

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def verfolgte_dateien():
    """Only what git publishes.

    🔑 Asking the filesystem would sweep in the maintainer's own
    untracked scratch files and report leaks that are not published —
    and a guard that cries wolf gets switched off. git is the authority
    on what leaves this machine.
    """
    aus = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'],
                         capture_output=True, text=True, check=True)
    return [n for n in aus.stdout.split('\0') if n]


def textdateien():
    for name in verfolgte_dateien():
        p = ROOT / name
        try:
            yield name, p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue


print("\n== no hostname of a real installation ==")
token = re.compile(r'\bev-[a-z][a-z0-9-]{1,20}')
fremd = []
for name, text in textdateien():
    for nr, zeile in enumerate(text.split('\n'), 1):
        for t in token.findall(zeile):
            if not any(t.startswith(e) for e in ERLAUBT):
                fremd.append(f'{name}:{nr}: {t}')
check(not fremd,
      "every ev-* name belongs to the project"
      + ("" if not fremd else f" — found: {fremd[:8]}"))
if fremd:
    print("  (a legitimate new name goes into ERLAUBT above, on purpose: "
          "that way somebody decides rather than nobody noticing)")

print("\n== no real place or person from an installation ==")
if not BLOCKLISTE.is_file():
    print(f"  (no {BLOCKLISTE} — this half is for whoever knows which "
          "words are real, and is skipped without it)")
else:
    begriffe = [z.strip() for z in
                BLOCKLISTE.read_text(encoding='utf-8').split('\n')
                if z.strip() and not z.lstrip().startswith('#')]
    check(bool(begriffe), f"{BLOCKLISTE.name} lists at least one term")
    treffer = []
    for name, text in textdateien():
        if name == 'tests/test_no_private_names.py':
            continue  # it would only ever find itself
        klein = text.lower()
        for b in begriffe:
            if b.lower() in klein:
                nr = klein[:klein.index(b.lower())].count('\n') + 1
                # The term itself is NOT printed — this output ends up in
                # logs and pasted into chats.
                treffer.append(f'{name}:{nr}')
    check(not treffer,
          f"none of the {len(begriffe)} private terms appears"
          + ("" if not treffer else f" — at: {sorted(set(treffer))[:8]}"))

print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
