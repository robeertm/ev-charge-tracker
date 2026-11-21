"""The six language files must stay in step (I1).

Same keys everywhere, no empty values, the same placeholders in every
language. A gap shows the user a bare key or a half-filled sentence.
"""
import json
import os
import re

import pytest

DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'translations')
LANGS = ['en', 'de', 'fr', 'es', 'nl', 'it']
_PH = re.compile(r'\{[^{}]*\}|%\([^)]*\)[sd]|%[sd]')


def _load(lang):
    with open(os.path.join(DIR, f'{lang}.json'), encoding='utf-8') as fh:
        return json.load(fh)


def test_exactly_six_language_files():
    files = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith('.json'))
    assert files == sorted(LANGS)


@pytest.mark.parametrize('lang', LANGS[1:])
def test_same_keys_as_english(lang):
    ref, cur = set(_load('en')), set(_load(lang))
    assert not ref - cur, f'{lang} lacks: {sorted(ref - cur)[:20]}'
    assert not cur - ref, f'{lang} has extra: {sorted(cur - ref)[:20]}'


@pytest.mark.parametrize('lang', LANGS)
def test_no_empty_values(lang):
    empty = [k for k, v in _load(lang).items()
             if not isinstance(v, str) or not v.strip()]
    assert not empty, f'{lang} empty/non-string: {empty[:20]}'


@pytest.mark.parametrize('lang', LANGS[1:])
def test_same_placeholders_as_english(lang):
    ref, cur = _load('en'), _load(lang)
    bad = [k for k in ref if k in cur
           and sorted(_PH.findall(ref[k])) != sorted(_PH.findall(cur[k]))]
    assert not bad, f'{lang} placeholder mismatch: {bad[:20]}'
