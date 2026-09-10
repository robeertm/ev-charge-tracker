# -*- coding: utf-8 -*-
"""Updating a container from inside the app, with no shell on the host.

v3.0.125 established that a container must not swap its own files: the
new code lands in the writable layer and the next ``docker compose pull``
throws it away. That was right, and it left the owner with a command to
type — which assumes they are sitting at the host. They may be on a
phone on another continent while the server hums away at home.

So a sibling container does the work and the app asks by dropping a
marker into a shared volume.

The property that makes this safe is not the marker's contents but their
irrelevance: everything the sibling may do is fixed in its script, so the
app has no Docker access at all and the worst anyone reaching the web UI
can cause is "update to the published image".
"""
import json
import pathlib
import re
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import container_update                            # noqa: E402


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(container_update, 'INBOX', tmp_path)
    return tmp_path


def test_without_a_sibling_there_is_no_button(inbox):
    """An empty mount is a half-finished setup. Promising a button for it
    would be worse than showing the command."""
    assert container_update.available() is False
    assert container_update.request('9.9.9') is False
    assert not list(inbox.iterdir())


def test_a_sibling_that_has_reported_once_counts_as_present(inbox):
    (inbox / container_update.STATUS).write_text('{"state": "idle"}')
    assert container_update.available() is True
    assert container_update.status() == {'state': 'idle'}


def test_the_request_is_written_atomically(inbox):
    (inbox / container_update.STATUS).write_text('{"state": "idle"}')
    assert container_update.request('3.0.126') is True
    daten = json.loads((inbox / container_update.REQUEST).read_text())
    assert daten['requested_version'] == '3.0.126'
    # Kein halbfertiges Zwischenprodukt zurückgelassen.
    assert not (inbox / (container_update.REQUEST + '.tmp')).exists()


def test_a_stale_marker_does_not_block_the_button_for_ever(inbox):
    (inbox / container_update.STATUS).write_text('{"state": "idle"}')
    marker = inbox / container_update.REQUEST
    marker.write_text('{}')
    assert container_update.pending() is True
    import os
    alt = time.time() - container_update.STALE_SECONDS - 60
    os.utime(marker, (alt, alt))
    assert container_update.pending() is False, (
        'a sibling that died would otherwise disable the button for good')


def test_unreadable_status_is_not_an_exception(inbox):
    (inbox / container_update.STATUS).write_text('nicht json')
    assert container_update.available() is True
    assert container_update.status() == {}


# ── The script's own guarantees, read as text ────────────────────────
SCRIPT = (ROOT / 'deploy' / 'updater.sh').read_text(encoding='utf-8')


def test_the_marker_is_never_interpolated_into_a_command():
    """The whole safety argument in one test.

    If the request file's contents ever reached a command line, a
    compromised app could run anything as the Docker daemon. So the
    script must only ever test for the file and delete it.
    """
    # The property is about the file's CONTENTS, not its path: naming the
    # path in a log line is fine, reading what is inside it is not. My
    # first version of this test asserted the shape of the line instead
    # of the thing that matters and flagged an echo.
    verboten = (
        r'cat\s+"?\$REQUEST',
        r'<\s*"?\$REQUEST',
        r'source\s+"?\$REQUEST',
        r'eval',
    )
    for zeile in SCRIPT.splitlines():
        code = zeile.split('#', 1)[0]
        for muster in verboten:
            treffer = re.search(muster, code)
            assert not treffer, (
                "the request file's contents reach a command: "
                + repr(zeile.strip()))


def test_the_recreate_keeps_no_deps():
    """Without --no-deps compose also recreates what the app depends on,
    which can include the very thing the updater is talking through."""
    assert 'up -d --no-deps' in SCRIPT


def test_the_request_is_removed_before_the_work_starts():
    """Otherwise a crash mid-pull spins on the same request for ever."""
    vor_rm = SCRIPT.index('rm -f "$REQUEST"')
    vor_pull = SCRIPT.index('docker compose pull')
    assert vor_rm < vor_pull


def test_the_compose_file_ships_the_sibling_and_its_volume():
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'ev-updater:' in compose
    assert 'ev-updater-inbox:/inbox' in compose
    assert 'ev-updater-inbox:/app/updater-inbox' in compose
    # Only the sibling gets the socket. The app must never mount it.
    app_block = compose.split('ev-updater:')[0]
    assert 'docker.sock' not in app_block, \
        'the app container must not have Docker access'


def test_the_installer_fetches_the_helper_script():
    """A machine set up before this existed gains the button by
    re-running the same one-liner it was installed with."""
    inst = (ROOT / 'deploy' / 'docker-install.sh').read_text(encoding='utf-8')
    assert 'deploy/updater.sh' in inst


def test_charging_is_checked_before_the_container_branch():
    """A container update recreates the container, which stops the sync
    loop exactly like a file swap does — so it must not skip the gate.

    Seen red: the first version of the container branch returned above
    this check and quietly took the gate away from container users.
    """
    quelle = (ROOT / 'app.py').read_text(encoding='utf-8')
    block = quelle[quelle.index('def api_update_install'):]
    block = block[:block.index('def ', 10)]
    assert block.index("'vehicle_charging'") < block.index('updates_by_image()'), \
        'the container path returns before the charging gate'


# ── The three flags, each one paid for by a failed live run ──────────
def test_the_compose_project_name_is_pinned():
    """Compose derives the project name from the working directory, and
    ours is a mount point — not the host directory the stack was created
    from. Unpinned it believes it is a different project and dies with

        Conflict. The container name "/ev-charge-tracker" is already in
        use ...

    while the app keeps running on the old version. That is what the
    first live run did.
    """
    assert '--project-name' in SCRIPT
    assert 'com.docker.compose.project' in SCRIPT


def test_the_project_directory_is_the_host_path():
    """The daemon resolves relative bind mounts against it, so it has to
    be the path on the host, not our mount point."""
    assert '--project-directory' in SCRIPT
    assert 'com.docker.compose.project.working_dir' in SCRIPT


def test_the_env_file_is_passed_explicitly():
    """--project-directory also moves where compose looks for '.env'.
    Without this the second live run died on

        required variable SECRET_KEY is missing a value
    """
    assert '--env-file' in SCRIPT


def test_a_missing_compose_label_is_reported_not_guessed():
    """Started outside compose there is nothing to pin to. Saying so
    beats letting the first update fail with a name conflict."""
    assert 'unconfigured' in SCRIPT
