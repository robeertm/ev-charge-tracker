#!/usr/bin/env python3
"""Fail when pip-audit finds a known vulnerability in the installed packages.

Used by .github/workflows/promote.yml against the published image before
`:latest` moves. Accepted findings live in security-exceptions.json, each with
`id`, `reason` (>= 10 characters) and `until` (YYYY-MM-DD, at most one year
ahead); an expired or malformed exception does not count.

    python scripts/audit_installed.py [--input pip-audit.json]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXC = os.path.join(HERE, "..", "security-exceptions.json")


def load_exceptions(path=EXC, today=None):
    today = today or datetime.date.today()
    ok = set()
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get("exceptions", [])
    except (OSError, ValueError):
        return ok
    for e in items:
        try:
            until = datetime.date.fromisoformat(str(e.get("until", "")))
        except ValueError:
            continue
        if len(str(e.get("reason", "")).strip()) < 10 or not e.get("id"):
            continue
        if until < today or (until - today).days > 366:
            continue
        ok.add(str(e["id"]))
    return ok


def findings(report, accepted):
    out = []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []):
            ids = {v.get("id", "")} | set(v.get("aliases", []))
            if ids & accepted:
                continue
            out.append("%s %s: %s" % (dep.get("name"), dep.get("version"), v.get("id")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="existing pip-audit JSON instead of running it")
    a = ap.parse_args()
    if a.input:
        with open(a.input, encoding="utf-8") as f:
            report = json.load(f)
    else:
        p = subprocess.run([sys.executable, "-m", "pip_audit", "--local", "-f", "json", "--progress-spinner", "off"],
                           capture_output=True, text=True)
        try:
            report = json.loads(p.stdout)
        except ValueError:
            print("pip-audit gave no result:", (p.stderr or "")[-300:])
            return 2
    bad = findings(report, load_exceptions())
    for b in bad:
        print("VULNERABLE", b)
    print("audit: %d finding(s)" % len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
