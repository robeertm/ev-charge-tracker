#!/usr/bin/env python3
"""Regenerate ``ev_specs.json`` from the upstream open-ev-data release.

open-ev-data (https://github.com/open-ev-data/open-ev-data-dataset,
CDLA-Permissive-2.0) publishes a fully-compiled ~3 MB JSON of every EV it
tracks — dozens of fields per car (chemistry, dimensions, performance …).
This app only needs four of them to help a user fill the vehicle form:
usable battery kWh and the AC/DC charge-power caps. So instead of bundling
the whole 3 MB release we extract a slim ~125 kB table here and commit that.

Dev-time tool only — needs network, run it manually to refresh the bundle:

    python -m services.vehicle.build_ev_specs            # latest release
    python -m services.vehicle.build_ev_specs v1.24.0    # pin a version

The runtime (``ev_specs_service.py``) never calls this; it just reads the
committed ``ev_specs.json``. Keeping the extractor in-repo means the next
person can re-derive the bundle instead of reverse-engineering it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

RELEASE_JSON = (
    'https://github.com/open-ev-data/open-ev-data-dataset'
    '/releases/download/{version}/open-ev-data-{version}.json'
)
LATEST_API = ('https://api.github.com/repos/open-ev-data/'
              'open-ev-data-dataset/releases/latest')
OUT_PATH = os.path.join(os.path.dirname(__file__), 'ev_specs.json')


def _num(value):
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _label(entry: dict) -> str:
    """Human, roughly-unique variant label: ``2024 · Long Range · Sportback``.
    Drops the generic "Base" trim and collapses trim==variant duplicates so
    an RS Enyaq reads ``2023 · RS`` rather than ``2023 · RS · RS``."""
    parts: list[str] = []
    year = entry.get('year')
    if year:
        parts.append(str(year))
    trim = entry.get('trim') or {}
    if trim.get('name') and (trim.get('slug') or '').lower() != 'base':
        parts.append(trim['name'])
    variant = entry.get('variant') or {}
    if variant.get('name') and variant['name'] not in parts:
        parts.append(variant['name'])
    return ' · '.join(parts)


def build(version: str | None = None) -> dict:
    if not version:
        with urllib.request.urlopen(LATEST_API, timeout=30) as resp:
            version = json.load(resp)['tag_name']
    url = RELEASE_JSON.format(version=version)
    with urllib.request.urlopen(url, timeout=90) as resp:
        src = json.load(resp)

    rows: list[dict] = []
    seen: set[tuple] = set()
    for entry in src.get('vehicles', []):
        make = (entry.get('make') or {}).get('name')
        model = (entry.get('model') or {}).get('name')
        if not make or not model:
            continue
        batt = entry.get('battery') or {}
        net = _num(batt.get('pack_capacity_kwh_net')) or \
            _num(batt.get('pack_capacity_kwh_gross'))
        if not net:
            continue  # useless for a kWh-from-SoC tracker without a capacity
        charging = entry.get('charging') or {}
        ac = _num((charging.get('ac') or {}).get('max_power_kw'))
        dc = _num((charging.get('dc') or {}).get('max_power_kw'))
        label = _label(entry)
        dedup = (make, model, label, net, ac, dc)
        if dedup in seen:
            continue
        seen.add(dedup)
        row = {'make': make, 'model': model, 'net_kwh': net}
        if label:
            row['variant'] = label
        if ac is not None:
            row['ac_kw'] = ac
        if dc is not None:
            row['dc_kw'] = dc
        rows.append(row)

    rows.sort(key=lambda r: (r['make'].lower(), r['model'].lower(),
                             r.get('variant', '')))
    return {
        'source': 'open-ev-data',
        'source_repo': 'https://github.com/open-ev-data/open-ev-data-dataset',
        'source_version': version,
        'license': 'CDLA-Permissive-2.0',
        'note': ('Slim extract — only usable battery kWh + AC/DC charge power '
                 'are kept. See docs/ev-data-sources-evaluation.md. '
                 'Regenerate with services/vehicle/build_ev_specs.py.'),
        'vehicle_count': len(rows),
        'vehicles': rows,
    }


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else None
    doc = build(version)
    with open(OUT_PATH, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(',', ':'))
    print(f"wrote {OUT_PATH}: {doc['vehicle_count']} vehicles "
          f"from open-ev-data {doc['source_version']}")


if __name__ == '__main__':
    main()
