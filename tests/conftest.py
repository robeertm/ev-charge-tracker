# -*- coding: utf-8 -*-
"""Shared test setup."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _homeassistant_memory_in_tmp(tmp_path, monkeypatch):
    """The Home Assistant connector remembers the previous poll in a file
    under the data folder. Every test gets an empty one of its own, so a
    test never reads what another test (or the real data folder) left."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from services.vehicle import connector_homeassistant as H
    except Exception:                      # pragma: no cover - import trouble shows elsewhere
        yield
        return
    monkeypatch.setattr(H, 'STATE_DIR', str(tmp_path / 'homeassistant'))
    yield


@pytest.fixture
def fresh_app_modules():
    """Import ``app`` / ``config`` / ``models`` (… ) afresh — and undo it.

    A few files need a clean import because ``config`` reads ``EV_DATA_DIR``
    when it is imported. They used to pop the modules from ``sys.modules``
    and leave them popped. Every file that ran later then held the OLD
    ``models.database`` (imported at collection time) while the code under
    test imported the NEW one inside its functions — two SQLAlchemy
    instances, and "The current Flask app is not registered with this
    'SQLAlchemy' instance". Popping ``services`` as well split
    ``services.units`` in two, so a test switching to miles switched a
    module ``t()`` no longer read. Both showed up only in the full run,
    depending on file order.

    Usage: ``fresh_app_modules(('app', 'config', 'models'), EV_DATA_DIR=d)``.
    On teardown every module imported meanwhile is dropped, the originals
    (and their attributes on the parent packages) are put back, and the
    environment variables are restored.
    """
    saved = dict(sys.modules)
    saved_env = {}

    def purge(prefixes, **env):
        for key, value in env.items():
            saved_env.setdefault(key, os.environ.get(key))
            os.environ[key] = value
        prefixes = tuple(prefixes)
        for name in [m for m in list(sys.modules) if m.startswith(prefixes)]:
            sys.modules.pop(name, None)

    yield purge

    for name in [m for m in list(sys.modules) if m not in saved]:
        mod = sys.modules.pop(name, None)
        parent, _, child = name.rpartition('.')
        if parent in saved and getattr(saved[parent], child, None) is mod:
            try:
                delattr(saved[parent], child)
            except AttributeError:
                pass
    sys.modules.update(saved)
    for name, mod in saved.items():
        parent, _, child = name.rpartition('.')
        if parent in saved and child in getattr(saved[parent], '__dict__', {}):
            if saved[parent].__dict__[child] is not mod and \
                    isinstance(saved[parent].__dict__[child], type(sys)):
                setattr(saved[parent], child, mod)
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
