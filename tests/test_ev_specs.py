"""Offline unit tests for services/vehicle/ev_specs_service.py.

No Flask/DB needed — the service only reads the bundled ev_specs.json.
Run with:  python3 tests/test_ev_specs.py
Exit code is non-zero if any check fails.
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_service():
    path = os.path.join(ROOT, 'services', 'vehicle', 'ev_specs_service.py')
    spec = importlib.util.spec_from_file_location('ev_specs_service', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    svc = _load_service()
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(('PASS' if cond else 'FAIL'), name)
        if not cond:
            fails += 1

    # bundle is present and non-trivial
    meta = svc.dataset_meta()
    check('bundle license', meta.get('license') == 'CDLA-Permissive-2.0')
    check('bundle has version', bool(meta.get('source_version')))

    # blank query returns nothing (caller decides when to ask)
    check('empty query -> []', svc.search('') == [])
    check('whitespace query -> []', svc.search('   ') == [])

    # every returned row carries a usable capacity — the whole point
    ioniq = svc.search('ioniq 5')
    check('ioniq 5 found', len(ioniq) > 0)
    check('ioniq net_kwh present', all(r.get('net_kwh') for r in ioniq))
    check('ioniq 72.6 present',
          any(abs((r.get('net_kwh') or 0) - 72.6) < 0.05 for r in ioniq))

    # multi-token AND match (make + model fragment)
    enyaq = svc.search('skoda enyaq')
    check('skoda enyaq found', len(enyaq) > 0)
    check('enyaq all skoda',
          all('koda' in (r.get('make', '')) for r in enyaq))  # Škoda/Skoda

    # limit is honoured
    many = svc.search('e', limit=5)
    check('limit honoured', len(many) <= 5)

    # public rows never leak the internal search haystack
    check('no _hay leak', all('_hay' not in r for r in ioniq))
    check('only known keys', all(
        set(r).issubset({'make', 'model', 'variant', 'net_kwh', 'ac_kw', 'dc_kw'})
        for r in ioniq))

    # nonsense query is empty, not an error
    check('nonsense -> []', svc.search('zzzxxxnope') == [])

    # bundle itself is valid JSON with the advertised shape
    with open(os.path.join(ROOT, 'services', 'vehicle', 'ev_specs.json')) as fh:
        doc = json.load(fh)
    check('count matches', doc['vehicle_count'] == len(doc['vehicles']))
    check('all rows have make/model/net_kwh',
          all(r.get('make') and r.get('model') and r.get('net_kwh')
              for r in doc['vehicles']))

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
