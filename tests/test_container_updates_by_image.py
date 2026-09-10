# -*- coding: utf-8 -*-
"""In a container, the app must not update itself by swapping files.

A customer asked whether the in-app update button is safe on the
ghcr.io image, saying that other projects (Nextcloud among them) update
only by pulling a new image. Measured on the published image before
answering, rather than reasoned about:

    3.0.124  →  in-app update  →  3.0.125  →  docker restart      →  3.0.125
                                           →  container recreated →  3.0.124

The application code lives in the image; only ``/app/data`` is a volume.
An in-app update therefore writes into the container's writable layer,
which is precisely the layer ``docker compose pull`` throws away — and
the updater's own ``updates/backup_pre_*`` rollback copy went with it.
The app reported a version that a routine container operation silently
took back, and a user who then followed the documented image-update path
ended up behind where they thought they were.

So the update check still runs and still shows what changed, and the
installation itself is refused with a reason. The gate sits in the route
AND in ``apply_update``: a button that is merely missing from a page is
not a safety property.
"""
import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater                                                   # noqa: E402
from services import runtime_env                                 # noqa: E402


@pytest.fixture
def im_container(monkeypatch):
    monkeypatch.setattr(runtime_env, '_cached', True, raising=False)
    monkeypatch.setattr(runtime_env, 'in_container', lambda: True)
    yield


@pytest.fixture
def nicht_im_container(monkeypatch):
    monkeypatch.setattr(runtime_env, 'in_container', lambda: False)
    yield


def test_a_container_says_it_updates_by_image(im_container):
    assert updater.updates_by_image() is True


def test_a_native_install_does_not(nicht_im_container):
    assert updater.updates_by_image() is False


def test_apply_update_refuses_in_a_container(im_container, monkeypatch):
    """Refused before anything is downloaded, so no half-written tree."""
    gerufen = []
    monkeypatch.setattr(updater, '_download_zip',
                        lambda *a, **k: gerufen.append(a))
    assert updater.apply_update('http://example.invalid/x.zip', '9.9.9') is False
    assert not gerufen, 'it started downloading before refusing'


def test_force_does_not_lift_the_container_gate(im_container, monkeypatch):
    """``force`` exists for the charging gate — a matter of timing. Where
    the files live is not a matter of timing."""
    gerufen = []
    monkeypatch.setattr(updater, '_download_zip',
                        lambda *a, **k: gerufen.append(a))
    assert updater.apply_update('http://example.invalid/x.zip', '9.9.9',
                                force=True) is False
    # False alone would also come out of a download that simply failed —
    # what has to be true is that it never started.
    assert not gerufen


def test_a_shell_inside_the_container_may_still_insist(im_container, monkeypatch):
    """The CLI passes allow_in_container: somebody who opened a shell and
    typed "y" has said plainly that it is their layer to spend."""
    schritte = []
    monkeypatch.setattr(updater, '_download_zip',
                        lambda *a, **k: schritte.append('download'))
    monkeypatch.setattr(updater, '_extract_and_unwrap',
                        lambda *a, **k: pathlib.Path('/nonexistent'))
    monkeypatch.setattr(updater, 'swaps_inline', lambda: True)
    monkeypatch.setattr(updater, '_inline_swap', lambda *a, **k: True)
    monkeypatch.setattr(updater, '_vehicle_is_charging_from_sqlite', lambda: False)
    updater.apply_update('http://example.invalid/x.zip', '9.9.9',
                         allow_in_container=True)
    assert schritte == ['download']


def test_the_route_refuses_too_and_not_only_the_page():
    """Checked as text: importing app.py needs a database and a config,
    and this is a statement about the code, not about a running app."""
    quelle = (ROOT / 'app.py').read_text(encoding='utf-8')
    stelle = quelle.index("def api_update_install")
    # Bis zur naechsten Funktion, nicht bis zu einer Zeichenzahl: der
    # Block ist gewachsen und ein fester Ausschnitt haette den Riegel
    # verloren, ohne dass sich am Riegel etwas geaendert hat.
    block = quelle[stelle:]
    block = block[:block.index('    @app.route', 10)]
    assert 'updates_by_image()' in block
    assert "'updates_by_image'" in block


def test_the_check_still_reports_what_is_available():
    """Knowing a new version exists is useful either way — only the
    install path differs. A container user who is told nothing would
    have to watch the repository by hand."""
    quelle = (ROOT / 'app.py').read_text(encoding='utf-8')
    stelle = quelle.index("def api_update_check")
    block = quelle[stelle:stelle + 1500]
    assert "'update_available'" in block
    assert "'by_image'" in block
    assert "'release_url'" in block
