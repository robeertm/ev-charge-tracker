# -*- coding: utf-8 -*-
"""A vehicle added after container start must bring the sync service up.

The background sync used to be started only at boot. On a fresh instance
the first vehicle is created afterwards, so the dashboard showed
``bg_loop_not_yet_ticked`` until the container was restarted. Saving a
vehicle with credentials and auto-sync now starts the service if it is
not already running - once, even under concurrent saves.
"""
import os
import sys
import tempfile
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def env(monkeypatch):
    d = tempfile.mkdtemp(prefix='evct-syncstart-')
    os.environ['EV_DATA_DIR'] = d
    os.environ.setdefault('SECRET_KEY', 'syncstart-test')
    for mod in [m for m in list(sys.modules) if m.startswith(('app', 'config', 'models', 'services'))]:
        sys.modules.pop(mod, None)
    from services.vehicle import sync_service as ss
    started = []
    stop = threading.Event()

    def fake_loop(*a, **k):
        started.append(threading.current_thread().name)
        stop.wait(5)

    monkeypatch.setattr(ss, '_sync_loop', fake_loop)
    monkeypatch.setattr(ss, '_nightly_maintenance_loop', lambda *a, **k: stop.wait(5))
    monkeypatch.setattr(ss, '_sync_watchdog_loop', lambda *a, **k: stop.wait(5))
    monkeypatch.setattr(ss, '_sync_running', False)
    from app import create_app
    app = create_app()
    # create_app may have started it already (nothing configured: it must not)
    assert ss.is_running() is False
    yield app, ss, started
    stop.set()
    ss._sync_running = False


FORM = {'name': 'First car', 'api_brand': 'kia', 'api_username': 'a@example.com',
        'api_password': 'x', 'auto_sync': '1', 'ajax': '1'}


def test_first_vehicle_after_boot_starts_sync(env):
    app, ss, started = env
    r = app.test_client().post('/vehicles/save', data=FORM)
    assert r.status_code == 200, r.data
    assert ss.is_running() is True
    from models.database import AppConfig
    with app.app_context():
        assert AppConfig.get('vehicle_sync_enabled') == 'true'


def test_second_save_does_not_start_twice(env):
    app, ss, started = env
    c = app.test_client()
    c.post('/vehicles/save', data=FORM)
    c.post('/vehicles/save', data=dict(FORM, name='Second car'))
    import time; time.sleep(0.2)
    assert started.count('sync-loop') == 1


def test_concurrent_start_spawns_one_set(env):
    app, ss, started = env
    with app.app_context():
        from models.database import Vehicle, db
        v = Vehicle.query.first() or Vehicle(name='c')
        v.api_brand, v.api_username, v.auto_sync, v.is_archived = 'kia', 'a@example.com', True, False
        db.session.add(v)
        db.session.commit()
    res = []
    ts = [threading.Thread(target=lambda: res.append(ss.start_sync(app))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    import time; time.sleep(0.2)
    assert res.count(True) == 1
    assert started.count('sync-loop') == 1


def test_vehicle_without_credentials_or_autosync_does_not_start(env):
    app, ss, started = env
    c = app.test_client()
    c.post('/vehicles/save', data={'name': 'No creds', 'ajax': '1'})
    c.post('/vehicles/save', data=dict(FORM, auto_sync='0', name='Manual'))
    assert ss.is_running() is False
