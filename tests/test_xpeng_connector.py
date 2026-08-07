"""Offline unit tests for services/vehicle/connector_xpeng.py.

Pure parsing / selection logic only — no network. The Enode HTTP calls
(`_ensure_auth`, `_list_vehicles`) are never exercised here; we test the
static `_to_status` mapper, the environment resolver, vehicle selection,
and that the connector registers under the ``xpeng`` brand key.

Run with:  python3 tests/test_xpeng_connector.py
Exit code is non-zero if any check fails.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    from services.vehicle.connector_xpeng import (
        XpengConnector, _resolve_env,
    )
    from services.vehicle.registry import get_connector, get_available_brands
    from services.vehicle.feature_matrix import get_features

    fails = 0

    def check(name, cond):
        nonlocal fails
        print(('PASS' if cond else 'FAIL'), name)
        if not cond:
            fails += 1

    # --- environment resolver -------------------------------------------
    check('env default->production', _resolve_env('') == 'production')
    check('env EU->production', _resolve_env('EU') == 'production')
    check('env PROD->production', _resolve_env('prod') == 'production')
    check('env TEST->sandbox', _resolve_env('TEST') == 'sandbox')
    check('env SAND->sandbox', _resolve_env('sand') == 'sandbox')

    # --- registration ---------------------------------------------------
    keys = [b['key'] for b in get_available_brands()]
    check('xpeng registered', 'xpeng' in keys)
    conn = get_connector('xpeng', {'username': 'id', 'password': 'sec'})
    check('get_connector returns XpengConnector', isinstance(conn, XpengConnector))
    check('brand_name', XpengConnector.brand_name() == 'XPENG (via Enode)')
    cred_keys = {f['key'] for f in XpengConnector.credential_fields()}
    check('cred fields', cred_keys == {'username', 'password', 'region', 'vin'})

    # --- feature matrix -------------------------------------------------
    feats = get_features('xpeng')
    check('feat soc yes', feats['soc_range_odo'] == 'yes')
    check('feat location yes', feats['location'] == 'yes')
    check('feat soh no', feats['soh'] == 'no')

    # --- _to_status: charging vehicle with full telemetry ---------------
    veh = {
        'id': 'abc', 'vendor': 'XPENG', 'lastSeen': '2026-08-07T10:00:00Z',
        'information': {'vin': 'LXP123', 'brand': 'XPENG', 'model': 'G6',
                        'year': 2024, 'displayName': 'Mein G6'},
        'chargeState': {
            'batteryLevel': 71.6, 'range': 318, 'isCharging': True,
            'isPluggedIn': True, 'chargeRate': 11.04, 'batteryCapacity': 82.0,
            'chargeTimeRemaining': 95, 'powerDeliveryState': 'PLUGGED_IN:CHARGING',
            'lastUpdated': '2026-08-07T09:59:00Z',
        },
        'odometer': {'distance': 12345.7, 'lastUpdated': '2026-08-07T09:00:00Z'},
        'location': {'latitude': 52.52, 'longitude': 13.405,
                     'lastUpdated': '2026-08-07T09:30:00Z'},
    }
    s = XpengConnector._to_status(veh)
    check('soc rounded', s.soc_percent == 72)
    check('odo rounded', s.odometer_km == 12346)
    check('is_charging', s.is_charging is True)
    check('is_plugged', s.is_plugged_in is True)
    check('charge power', s.charge_power_kw == 11.04)
    check('range', s.estimated_range_km == 318)
    check('eta', s.est_charge_duration_min == 95)
    check('lat', s.location_lat == 52.52)
    check('lon', s.location_lon == 13.405)
    check('loc ts passed through', s.location_last_updated_at == '2026-08-07T09:30:00Z')
    check('name', s.vehicle_name == 'Mein G6')
    check('raw kept', s.raw_data is veh)

    # --- _to_status: powerDeliveryState fallback (no boolean flags) ------
    veh2 = {
        'vendor': 'XPENG', 'information': {'model': 'P7'},
        'chargeState': {'batteryLevel': 40, 'powerDeliveryState': 'PLUGGED_IN:STOPPED'},
    }
    s2 = XpengConnector._to_status(veh2)
    check('fallback plugged true', s2.is_plugged_in is True)
    check('fallback charging false', s2.is_charging is False)
    check('fallback name from model', s2.vehicle_name == 'XPENG P7')
    check('missing odo->None', s2.odometer_km is None)
    check('missing gps->None', s2.location_lat is None)

    # --- _to_status: unplugged ------------------------------------------
    s3 = XpengConnector._to_status(
        {'chargeState': {'batteryLevel': 88, 'powerDeliveryState': 'UNPLUGGED'}})
    check('unplugged not plugged', s3.is_plugged_in is False)
    check('unplugged not charging', s3.is_charging is False)

    # --- _pick_vehicle: VIN filter + XPENG preference -------------------
    conn.credentials['vin'] = 'LXP999'
    fleet = [
        {'vendor': 'TESLA', 'information': {'vin': 'TSLA1'}},
        {'vendor': 'XPENG', 'information': {'vin': 'LXP999'}},
        {'vendor': 'XPENG', 'information': {'vin': 'LXP000'}},
    ]
    check('pick by vin', conn._pick_vehicle(fleet)['information']['vin'] == 'LXP999')
    conn.credentials['vin'] = ''
    check('pick prefers xpeng', conn._pick_vehicle(fleet)['vendor'] == 'XPENG')
    check('pick first when no xpeng',
          conn._pick_vehicle([{'vendor': 'TESLA'}])['vendor'] == 'TESLA')
    try:
        conn._pick_vehicle([])
        check('empty raises', False)
    except RuntimeError:
        check('empty raises', True)

    print('\nRESULT:', 'ALL PASS' if fails == 0 else f'{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
