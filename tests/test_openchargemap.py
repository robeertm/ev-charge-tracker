"""Offline unit tests for services/openchargemap_service.py pure logic.

No Flask/DB needed: models.database is stubbed so the module imports
standalone. Run with:  python3 tests/test_openchargemap.py
Exit code is non-zero if any check fails.
"""
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_service():
    stub = types.ModuleType('models.database')

    class _Q:
        def filter_by(self, **k): return self
        def first(self): return None

    class _M:
        query = _Q()

    stub.db = types.SimpleNamespace(
        session=types.SimpleNamespace(add=lambda x: None, commit=lambda: None))
    stub.OcmCache = _M
    stub.AppConfig = types.SimpleNamespace(get=lambda k, d=None: d)
    pkg = types.ModuleType('models')
    pkg.database = stub
    sys.modules['models'] = pkg
    sys.modules['models.database'] = stub

    path = os.path.join(ROOT, 'services', 'openchargemap_service.py')
    spec = importlib.util.spec_from_file_location('ocm', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ocm = _load_service()
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(('PASS' if cond else 'FAIL'), name)
        if not cond:
            fails += 1

    # haversine: ~0.001° latitude ≈ 111 m
    d = ocm._haversine_m(52.5200, 13.4050, 52.5210, 13.4050)
    check('haversine ~111m', 110 < d < 113)

    # operator name extraction + placeholder rejection
    check('op title', ocm._operator_from_poi({'OperatorInfo': {'Title': 'IONITY'}}) == 'IONITY')
    check('op unknown->None', ocm._operator_from_poi({'OperatorInfo': {'Title': '(Unknown Operator)'}}) is None)
    check('op missing->None', ocm._operator_from_poi({'OperatorInfo': None}) is None)
    check('op no info->None', ocm._operator_from_poi({}) is None)

    # highest connector power
    check('max power', ocm._max_power_kw({'Connections': [{'PowerKW': 50}, {'PowerKW': 150}, {'PowerKW': None}]}) == 150)
    check('no power', ocm._max_power_kw({'Connections': []}) is None)

    # pick_nearest chooses the closest POI regardless of list order
    pois = [
        {'AddressInfo': {'Title': 'Far', 'Latitude': 52.60, 'Longitude': 13.50},
         'OperatorInfo': {'Title': 'EnBW'}},
        {'AddressInfo': {'Title': 'IONITY Mitte', 'Latitude': 52.5201, 'Longitude': 13.4050},
         'OperatorInfo': {'Title': 'IONITY'}, 'Connections': [{'PowerKW': 350}]},
    ]
    res = ocm.pick_nearest(pois, 52.5200, 13.4050)
    check('nearest operator', res['operator'] == 'IONITY')
    check('nearest title', res['title'] == 'IONITY Mitte')
    check('nearest power', res['power_kw'] == 350)
    check('nearest close dist', res['distance_m'] < 30)
    check('pick empty->None', ocm.pick_nearest([], 0, 0) is None)

    # distance gate over a fake cache row
    class Row:
        def __init__(self, op, dist, poi):
            self.operator = op
            self.title = 'X'
            self.distance_m = dist
            self.raw_json = json.dumps(poi) if poi else None

    gate_in = ocm._apply_distance_gate(Row('IONITY', 120, {'Connections': [{'PowerKW': 150}]}), 150)
    check('gate within', bool(gate_in) and gate_in['operator'] == 'IONITY' and gate_in['power_kw'] == 150)
    check('gate outside->None', ocm._apply_distance_gate(Row('IONITY', 400, None), 150) is None)
    check('gate no-station->None', ocm._apply_distance_gate(Row(None, None, None), 150) is None)

    print('\nRESULT:', 'ALL PASS' if fails == 0 else f'{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
