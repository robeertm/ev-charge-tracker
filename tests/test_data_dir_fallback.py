"""The data folder is never silently swapped for an empty database (v3.0.156).

On 2026-10-14 Watchtower recreated a live container, and at that moment
SQLite's write test in the data folder failed once. ``config.py`` then
switched the app to an EMPTY database in the home directory — silently:
the owner saw a fresh install with a seeded "Mein Auto", the real data sat
untouched next to it, and anything entered meanwhile would have vanished
with the next recreate (the home directory lives inside the container).

Each case runs ``import config`` in a fresh interpreter, with SQLite's
connect patched to fail as often as the case needs.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUN = r'''
import json, os, sqlite3, sys, time
sys.path.insert(0, %(root)r)
fails = {'left': %(fails)d}
real = sqlite3.connect
def flaky(path, *a, **kw):
    if str(path).endswith('_test.db') and fails['left'] != 0:
        fails['left'] -= 1
        raise sqlite3.OperationalError('disk I/O error (simulated)')
    return real(path, *a, **kw)
sqlite3.connect = flaky
time.sleep = lambda s: None          # the retries' waits, not their count
try:
    import config
    print(json.dumps({'data_dir': config.DATA_DIR}))
except SystemExit as e:
    print(json.dumps({'exit': str(e)}))
'''


def _run(tmp_path, fails, with_db):
    data = tmp_path / 'data'
    data.mkdir()
    if with_db:
        (data / 'ev_tracker.db').write_bytes(b'')
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, EV_DATA_DIR=str(data), HOME=str(home))
    r = subprocess.run([sys.executable, '-c', RUN % {'root': ROOT, 'fails': fails}],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1]), str(data), str(home)


def test_a_passing_write_test_keeps_the_data_folder(tmp_path):
    out, data, _ = _run(tmp_path, 0, with_db=True)
    assert out['data_dir'] == data


def test_a_moment_of_failure_is_retried_not_answered_with_an_empty_database(tmp_path):
    out, data, _ = _run(tmp_path, 3, with_db=True)
    assert out['data_dir'] == data


def test_a_folder_with_a_database_is_never_swapped_for_an_empty_one(tmp_path):
    out, _, home = _run(tmp_path, -1, with_db=True)       # fails for good
    assert 'exit' in out and 'Refusing to start' in out['exit']
    assert not os.path.exists(os.path.join(home, '.ev-charge-tracker', 'ev_tracker.db'))


def test_a_folder_without_a_database_still_falls_back_at_once(tmp_path):
    """Installs on a filesystem SQLite cannot use have always lived on the
    fallback — that keeps working, with no waiting."""
    out, _, home = _run(tmp_path, 1, with_db=False)
    assert out['data_dir'] == os.path.join(home, '.ev-charge-tracker')
