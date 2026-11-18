# -*- coding: utf-8 -*-
"""Every dependency is capped, and the Kia/Hyundai SDK floor is 5.2.1.

On 10 Nov 2026 a routine image rebuild moved the Kia/Hyundai SDK from 4.x
to 5.x because its line only had a floor. 5.2.0 then reported "charging"
on every cached status with a remaining-charge time, which held the
charging guard shut on the affected install for a week. These tests keep
both halves: the floor excludes 5.2.0, and no line can jump a major
version unannounced (see scripts/check_dependency_bounds.py).
"""
import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _modul():
    spec = importlib.util.spec_from_file_location(
        'check_dependency_bounds', ROOT / 'scripts' / 'check_dependency_bounds.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_jede_abhaengigkeit_hat_eine_obergrenze():
    probleme = _modul().static_problems()
    assert not probleme, probleme


def test_die_pruefung_erkennt_eine_fehlende_obergrenze():
    m = _modul()
    assert m.has_upper_bound('foo>=1.0,<2')
    assert m.has_upper_bound('foo==1.2.3')
    assert not m.has_upper_bound('foo>=1.0')
    assert not m.has_upper_bound('foo')


def test_das_dockerfile_wird_mitgeprueft():
    namen = {re.split(r'[<>=!~ ]', s)[0] for _, s in _modul()._from_dockerfile()}
    assert {'dacite', 'httpx', 'tenacity'} <= namen, namen


def _kia_zeile():
    req = (ROOT / 'requirements-vehicles.txt').read_text()
    return next(ln.split('#')[0].strip() for ln in req.splitlines()
                if ln.split('#')[0].strip().startswith('hyundai-kia-connect-api'))


def test_die_kia_bibliothek_schliesst_5_2_0_aus_und_6_auch():
    from packaging.specifiers import SpecifierSet
    s = SpecifierSet(_kia_zeile().split('hyundai-kia-connect-api', 1)[1])
    assert '5.2.0' not in s
    assert '5.2.1' in s and '5.9.0' in s
    assert '6.0.0' not in s


def test_der_einbau_knopf_nimmt_dieselbe_grenze():
    from services.vehicle.catalog import PACKAGES
    kia = next(p for p in PACKAGES['hyundai-kia'] if p.startswith('hyundai-kia-connect-api'))
    assert kia == _kia_zeile()


def test_der_promote_lauf_prueft_das_abbild_mit_den_anschluss_tests():
    """The CI half: before :latest moves, the published image runs the
    bounds check against what it resolved, and the connector tests."""
    wf = (ROOT / '.github' / 'workflows' / 'promote.yml').read_text()
    assert 'check_dependency_bounds.py --installed' in wf
    assert 'test_unknown_location_newer_feed.py' in wf
    assert wf.index('check_dependency_bounds.py --installed') < wf.index('imagetools create -t')
