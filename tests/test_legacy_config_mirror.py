# -*- coding: utf-8 -*-
"""A vehicle's brand lives in two places. They must move together.

``vehicles.api_brand`` drives the background sync. A flat set of
``vehicle_api_*`` keys in AppConfig drives the single-vehicle code paths
— the dashboard's live status among them.

The Škoda changeover form wrote only the row. The result was an install
where the background sync correctly used the official API while the
dashboard kept calling the API Škoda is switching off, and **each looked
right on its own**: the settings page showed the new brand, the sync
records showed new-API data, and only the newest record — written by the
dashboard — was still from the old connector.

These tests run the app in a subprocess against a throwaway data
directory, because ``config.DATA_DIR`` is resolved at import time and a
pytest process cannot change it afterwards.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(script: str) -> dict:
    """Execute a snippet inside the app, with its own empty database."""
    data_dir = tempfile.mkdtemp(prefix='evct-mirror-')
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='mirror-test')
    proc = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    last = [ln for ln in proc.stdout.strip().split('\n') if ln.startswith('{')]
    assert last, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(last[-1])


_PREAMBLE = """
import json, sys
sys.path.insert(0, %r)
from app import create_app, mirror_primary_vehicle_to_legacy_config
from models.database import db, AppConfig, Vehicle
app = create_app()
""" % ROOT


def test_the_mirror_moves_the_brand_into_the_legacy_keys():
    out = _run(_PREAMBLE + """
with app.app_context():
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.api_brand = 'skoda_api'; v.api_username = ''
    v.api_password = 'a-key'; v.api_vin = 'TMBTESTVIN000001'
    db.session.commit()
    AppConfig.set('vehicle_api_brand', 'skoda')
    AppConfig.set('vehicle_api_username', 'old-account@example.com')
    changed = mirror_primary_vehicle_to_legacy_config(v)
    db.session.commit()
    print(json.dumps({
        'changed': bool(changed),
        'brand': AppConfig.get('vehicle_api_brand', ''),
        'vin': AppConfig.get('vehicle_api_vin', ''),
        'user': AppConfig.get('vehicle_api_username', ''),
    }))
""")
    assert out['changed'] is True
    assert out['brand'] == 'skoda_api', 'the legacy key kept the retiring brand'
    assert out['vin'] == 'TMBTESTVIN000001'
    assert out['user'] == '', (
        'the account name from the retiring access is still stored — it no '
        'longer unlocks anything and only makes the install look configured')


def test_the_mirror_never_erases_a_stored_password_it_was_not_given():
    """The fleet form does not resend a stored password. Mirroring an
    empty value would quietly log the vehicle out."""
    out = _run(_PREAMBLE + """
with app.app_context():
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.api_brand = 'kia'; v.api_password = None
    db.session.commit()
    AppConfig.set('vehicle_api_password', 'still-valid')
    mirror_primary_vehicle_to_legacy_config(v)
    db.session.commit()
    print(json.dumps({'pw': AppConfig.get('vehicle_api_password', '')}))
""")
    assert out['pw'] == 'still-valid'


def test_an_install_that_already_drifted_repairs_itself_on_the_next_start():
    """Anyone who used the changeover form before this fix has the two
    out of step. Nobody should have to notice that themselves."""
    data_dir = tempfile.mkdtemp(prefix='evct-selfheal-')
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='mirror-test')

    drift = _PREAMBLE + """
with app.app_context():
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.api_brand = 'skoda_api'; v.api_password = 'a-key'
    v.api_vin = 'TMBTESTVIN000001'
    db.session.commit()
    AppConfig.set('vehicle_api_brand', 'skoda')      # the drift
    print(json.dumps({'before': AppConfig.get('vehicle_api_brand', '')}))
"""
    p1 = subprocess.run([sys.executable, '-c', drift], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=180)
    assert p1.returncode == 0, p1.stderr[-2000:]
    assert '"before": "skoda"' in p1.stdout

    check = _PREAMBLE + """
with app.app_context():
    print(json.dumps({'after': AppConfig.get('vehicle_api_brand', '')}))
"""
    p2 = subprocess.run([sys.executable, '-c', check], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=180)
    assert p2.returncode == 0, p2.stderr[-2000:]
    after = json.loads([l for l in p2.stdout.strip().split('\n')
                        if l.startswith('{')][-1])
    assert after['after'] == 'skoda_api', (
        'the next start did not repair the drift: ' + str(after))
