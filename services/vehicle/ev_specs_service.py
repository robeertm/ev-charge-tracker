"""Battery-spec lookup for the vehicle form, backed by open-ev-data.

open-ev-data (https://github.com/open-ev-data/open-ev-data-dataset) is a
CDLA-Permissive, community-maintained dataset of EV specifications. It's the
second resource from the ``awesome-ev-charging`` list that fits this app —
Open Charge Map (``openchargemap_service``) was the first. Where OCM answers
"which operator did I charge at", this answers "how big is my battery and how
fast does it charge on AC?" — exactly the two numbers (``battery_kwh``,
``max_ac_kw``) a user otherwise has to look up by hand when adding a car.

Those numbers matter because every charge's kWh is derived from the SoC delta
times ``battery_kwh``; a wrong capacity skews every cost/CO2/loss figure.

Design notes:
  * Fully offline — reads the committed slim bundle ``ev_specs.json`` (see
    ``build_ev_specs.py``). No network at runtime, in keeping with the app's
    no-cloud stance. If the bundle is missing/corrupt, search returns ``[]``
    and the form simply doesn't offer suggestions.
  * The dataset is *advisory*: it pre-fills the form, the user still edits and
    saves. We never overwrite a stored vehicle from it.
"""
from __future__ import annotations

import json
import logging
import os
import unicodedata
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), 'ev_specs.json')


def _fold(text: str) -> str:
    """Lowercase and strip diacritics so ``skoda`` matches ``Škoda`` and
    ``citroen`` matches ``Citroën`` — users don't type the accents."""
    decomposed = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).lower()


@lru_cache(maxsize=1)
def _load() -> dict:
    """Load and cache the bundle once. Never raises — a broken/absent file
    degrades to an empty dataset so the vehicle form keeps working."""
    try:
        with open(_DATA_PATH, encoding='utf-8') as fh:
            doc = json.load(fh)
        rows = doc.get('vehicles', [])
        # Precompute a lowercased haystack per row for cheap substring search.
        for row in rows:
            row['_hay'] = _fold(' '.join(
                str(row.get(k, '')) for k in ('make', 'model', 'variant')))
        return {'meta': {k: doc.get(k) for k in
                         ('source', 'source_version', 'license', 'source_repo')},
                'vehicles': rows}
    except FileNotFoundError:
        logger.warning('ev_specs.json not bundled; spec lookup disabled')
        return {'meta': {}, 'vehicles': []}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning('ev_specs.json unreadable (%s); spec lookup disabled', exc)
        return {'meta': {}, 'vehicles': []}


def dataset_meta() -> dict:
    """Provenance for the UI attribution line (source/version/license)."""
    return dict(_load()['meta'])


def _public(row: dict) -> dict:
    return {k: row[k] for k in ('make', 'model', 'variant', 'net_kwh',
                                'gross_kwh', 'ac_kw', 'dc_kw') if k in row}


def search(query: Optional[str], limit: int = 40) -> list[dict]:
    """Return up to ``limit`` spec rows whose make/model/variant contain every
    whitespace-separated token in ``query`` (case-insensitive, AND-matched).
    Empty/blank query returns ``[]`` — the caller decides when to ask."""
    rows = _load()['vehicles']
    if not query or not query.strip():
        return []
    tokens = _fold(query).split()
    hits = [r for r in rows if all(tok in r['_hay'] for tok in tokens)]
    return [_public(r) for r in hits[:max(1, limit)]]
