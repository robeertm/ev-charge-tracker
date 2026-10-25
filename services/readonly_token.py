# -*- coding: utf-8 -*-
"""Create, revoke and inspect the read-only API token — from the shell.

    python3 -m services.readonly_token new      # prints the new token, once
    python3 -m services.readonly_token off      # switches the endpoint off
    python3 -m services.readonly_token status   # 'on' or 'off', never the token

In a container: ``docker exec <container> python3 -m services.readonly_token new``.

``new`` writes the token to stdout and NOTHING else, so it can be piped
straight into a file on another machine without ever appearing on a
screen. Every other message goes to stderr. A new token replaces the old
one immediately; the old one stops working.

Only the SHA-256 of the token is stored (AppConfig
``readonly_api_token_sha256``); the token itself cannot be shown again —
lost means ``new``.

This talks to the database file directly instead of starting the app:
starting the app would also start its background sync. The file is the one
the RUNNING app has open (read from ``/proc/1/fd`` in a container), not
merely the one this process would compute — a fallback data folder once made
the two differ without anyone noticing. No database is ever created here.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from datetime import datetime

from services.readonly_api import TOKEN_CREATED_KEY, TOKEN_HASH_KEY, hash_token

DB_NAME = 'ev_tracker.db'


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _db_of_running_app() -> str | None:
    """The ev_tracker.db the container's main process has open, if visible."""
    fd_dir = '/proc/1/fd'
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    found = set()
    for n in names:
        try:
            target = os.readlink(os.path.join(fd_dir, n))
        except OSError:
            continue
        if os.path.basename(target) == DB_NAME:
            found.add(target)
    return found.pop() if len(found) == 1 else None


def _configured_db() -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, DB_NAME)


def resolve_db_path() -> str:
    running = _db_of_running_app()
    configured = _configured_db()
    if running and os.path.realpath(running) != os.path.realpath(configured):
        _err(f"note: the running app uses {running} (not {configured}); using that one")
        return running
    return running or configured


def _connect(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise SystemExit(f"error: no database at {path} — is this the app's data folder?")
    conn = sqlite3.connect(path, timeout=30)
    ok = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_config'").fetchone()
    if not ok:
        conn.close()
        raise SystemExit(f"error: {path} has no app_config table — not this app's database")
    return conn


def _set(conn, key, value):
    conn.execute(
        "INSERT INTO app_config(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def cmd_new(conn) -> int:
    token = secrets.token_urlsafe(32)
    with conn:
        _set(conn, TOKEN_HASH_KEY, hash_token(token))
        _set(conn, TOKEN_CREATED_KEY, datetime.now().isoformat(timespec='seconds'))
    sys.stdout.write(token + '\n')
    sys.stdout.flush()
    _err("read-only API token created; the previous one (if any) no longer works")
    return 0


def cmd_off(conn) -> int:
    with conn:
        conn.execute("DELETE FROM app_config WHERE key IN (?, ?)",
                     (TOKEN_HASH_KEY, TOKEN_CREATED_KEY))
    print('off')
    return 0


def cmd_status(conn) -> int:
    row = conn.execute("SELECT value FROM app_config WHERE key = ?",
                       (TOKEN_HASH_KEY,)).fetchone()
    if row and (row[0] or '').strip():
        created = conn.execute("SELECT value FROM app_config WHERE key = ?",
                               (TOKEN_CREATED_KEY,)).fetchone()
        print('on' + (f" (since {created[0]})" if created and created[0] else ''))
    else:
        print('off')
    return 0


COMMANDS = {'new': cmd_new, 'off': cmd_off, 'status': cmd_status}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in COMMANDS:
        _err("usage: python3 -m services.readonly_token new|off|status")
        return 2
    conn = _connect(resolve_db_path())
    try:
        return COMMANDS[argv[0]](conn)
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
