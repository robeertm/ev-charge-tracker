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
