# -*- coding: utf-8 -*-
"""No legacy ``Query.get()`` left — in the source and at runtime.

SQLAlchemy 2.0 flags ``Model.query.get(x)`` with a ``LegacyAPIWarning`` on
every call, which filled the logs. Every lookup by primary key now goes
through ``db.session.get(Model, x)`` and ``db.get_or_404(Model, x)``.

Two checks keep it that way:

* **source** — no ``.query.get(`` / ``.query.get_or_404(`` /
  ``query(...).get(`` anywhere in the repository's Python files;
* **runtime** — the real app on a throwaway database, with the legacy and
  deprecation warnings turned into errors, serves the routes and runs the
  service functions that used to call ``Query.get()``. A warning that some
  ``try/except`` would swallow is still recorded and fails the test.

The runtime part runs in a subprocess because ``config.DATA_DIR`` is fixed
at import time (same pattern as test_readonly_api.py).
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIS = os.path.abspath(__file__)

LEGACY = re.compile(
    r'\.query\s*\.\s*get(?:_or_404)?\s*\('          # Model.query.get( / get_or_404(
    r'|\bquery\([^()]*(?:\([^()]*\)[^()]*)*\)\s*\.\s*get\s*\('  # session.query(M).get(
)
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', 'worktrees', 'venv', '.venv',
             'env', 'site-packages'}


def _python_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith('.venv')
                       and not os.path.exists(os.path.join(dirpath, d, 'pyvenv.cfg'))]
        for name in filenames:
            if name.endswith('.py'):
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) != THIS:
                    yield path


def test_no_legacy_query_get_in_source():
    hits = []
    for path in _python_files():
        with open(path, encoding='utf-8', errors='replace') as f:
            for n, line in enumerate(f, 1):
                if LEGACY.search(line):
                    hits.append('%s:%d: %s' % (os.path.relpath(path, ROOT), n, line.strip()))
    assert not hits, 'legacy Query.get() still in use:\n' + '\n'.join(hits)


def test_the_source_check_would_catch_it():
    for bad in ('Vehicle.query.get(1)', 'Charge.query.get_or_404(cid)',
                'db.session.query(Vehicle).get(vid)', '_m().ParkingEvent.query.get(pe.id)'):
        assert LEGACY.search(bad), bad
    for good in ('db.session.get(Vehicle, 1)', 'db.get_or_404(Charge, cid)',
                 "request.args.get('vehicle_id')", "data.get('query')",
                 'Vehicle.query.filter_by(id=1).first()'):
        assert not LEGACY.search(good), good


_SCRIPT = r"""
import json, sys, warnings
sys.path.insert(0, %(root)r)
import sqlalchemy.exc as sa_exc

caught = []
_orig_warn = warnings.warn

def _recording_warn(message, category=None, stacklevel=1, **kw):
    # Record before delegating: under the 'error' filter the warning turns
    # into an exception, which app code may catch and swallow.
    cat = category or (type(message) if isinstance(message, Warning) else UserWarning)
    text = str(message)
    if (isinstance(cat, type) and issubclass(cat, (sa_exc.LegacyAPIWarning,
                                                    sa_exc.SADeprecationWarning))
            or 'NULL primary key' in text):
        caught.append('%%s: %%s' %% (getattr(cat, '__name__', cat), text[:200]))
    return _orig_warn(message, category, stacklevel + 1, **kw)

warnings.warn = _recording_warn
warnings.simplefilter('error', sa_exc.LegacyAPIWarning)
warnings.simplefilter('error', sa_exc.SADeprecationWarning)

from datetime import date, datetime
from app import create_app
from models.database import (db, AppConfig, Vehicle, Charge, MaintenanceEntry,
                             WallboxCharge)
app = create_app()
client = app.test_client()
codes = {}

with app.app_context():
    AppConfig.set('vehicle_sync_enabled', 'false')
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.name = 'Car'; v.brand = 'Kia'; v.model = 'Niro EV'; v.auto_sync = False
    db.session.commit()
    c = Charge(vehicle_id=v.id, date=date(2026, 10, 24), charge_hour=22,
               charge_end_hour=3, kwh_loaded=21.5, soc_from=40, soc_to=75,
               operator='Zuhause / privat', eur_per_kwh=0.31)
    db.session.add(c)
    db.session.flush()
    t0 = int(datetime(2026, 10, 24, 22, 0).timestamp())
    wc = WallboxCharge(source_id='test', device_key='k', start_ts=t0,
                       end_ts=t0 + 5 * 3600, energy_kwh=22.0, charge_id=c.id)
    db.session.add(wc)
    db.session.flush()
    c.wallbox_charge_id = wc.id
    m = MaintenanceEntry(vehicle_id=v.id, date=date(2026, 9, 1),
                         item_type='inspection')
    db.session.add(m)
    db.session.commit()
    vid, cid, wcid, mid = v.id, c.id, wc.id, m.id

    # Token for the read-only API, the way the CLI stores it.
    from services import readonly_api as ro
    AppConfig.set(ro.TOKEN_HASH_KEY, ro.hash_token('t' * 43))

for path in ('/', '/settings', '/edit/%%d' %% cid, '/edit/999999',
             '/api/charges/%%d' %% cid, '/api/charges/999999',
             '/api/wallbox/charge/%%d' %% cid, '/api/wallbox/candidates/%%d' %% wcid,
             '/api/maintenance/%%d' %% mid, '/api/maintenance/999999',
             '/api/vehicle/%%d/remote' %% vid, '/api/vehicle/999999/remote',
             '/api/vehicles/%%d/battery-health' %% vid, '/api/vehicles/999999/battery-health',
             '/api/sync/audit'):
    codes[path] = client.get(path).status_code
# The first '/' of a session sends an unconnected car to the settings once;
# the second one renders the dashboard.
codes['dashboard'] = client.get('/').status_code
codes['readonly'] = client.get('/api/readonly/vehicle',
                               headers={'Authorization': 'Bearer ' + 't' * 43}).status_code
for thg_id in (None, '', 'abc', '999999'):
    form = {'action': 'delete_thg'}
    if thg_id is not None:
        form['thg_id'] = thg_id
    codes['delete_thg=%%r' %% thg_id] = client.post('/settings', data=form).status_code

services = {}
with app.app_context():
    from services import maintenance_service, stats_service, sync_audit
    from services import battery_cert_service, shelly_link, trip_reconcile
    from services import readonly_api as ro
    services['update_none'] = maintenance_service.update_entry(None, notes='x')
    services['delete_missing'] = maintenance_service.delete_entry(999999)
    services['delete_none'] = maintenance_service.delete_entry(None)
    services['appconfig_none'] = AppConfig.get(None, 'dflt')
    services['soh_baseline'] = stats_service.get_soh_baseline(vid)
    services['audit'] = sync_audit.audit([vid, None, 999999])['total']
    services['cert_none'] = battery_cert_service.compute_battery_health(None)
    services['cert_missing'] = battery_cert_service.compute_battery_health(999999)
    services['cert'] = battery_cert_service.compute_battery_health(vid) is not None
    services['specs'] = shelly_link._default_specs(vid) is not None
    services['trip_info'] = trip_reconcile._brand_supports_trip_info(vid)
    services['charge_times'] = ro._charge_times(db.session.get(Charge, cid)) is not None

print(json.dumps({'codes': codes, 'services': services, 'caught': caught}, default=str))
"""


def test_no_legacy_warning_at_runtime():
    d = tempfile.mkdtemp(prefix='evct-qget-')
    env = dict(os.environ, EV_DATA_DIR=d, SECRET_KEY='qget-test')
    proc = subprocess.run([sys.executable, '-c', _SCRIPT % {'root': ROOT}], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [ln for ln in proc.stdout.strip().split('\n') if ln.startswith('{')]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(lines[-1])

    assert out['caught'] == [], out['caught']

    codes = out['codes']
    for path, code in codes.items():
        assert code != 500, (path, code, proc.stderr[-3000:])
    for path, code in codes.items():
        if path.endswith('999999') or path.endswith('999999/remote'):
            assert code == 404, (path, code)
    for prefix in ('/edit/', '/api/charges/', '/api/wallbox/charge/',
                   '/api/wallbox/candidates/', '/api/maintenance/', '/api/vehicle/'):
        found = [p for p in codes if p.startswith(prefix) and '999999' not in p]
        assert found and all(codes[p] == 200 for p in found), (prefix, codes)
    assert codes['dashboard'] == 200, codes
    assert codes['/settings'] == 200, codes
    assert codes['/api/sync/audit'] == 200, codes
    assert codes['/api/vehicles/999999/battery-health'] == 404, codes
    assert codes['readonly'] == 200, codes

    s = out['services']
    assert s['update_none'] is None
    assert s['delete_missing'] is False
    assert s['delete_none'] is False
    assert s['appconfig_none'] == 'dflt'
    assert s['audit'] == 0
    assert s['cert_none'] is None and s['cert_missing'] is None
    assert s['cert'] is True
    assert s['specs'] is True
    assert s['charge_times'] is True
