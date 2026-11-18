#!/usr/bin/env python3
"""Every dependency carries an upper bound — and the image honours it.

Why: a rebuilt image used to install whatever was newest. On 10 Nov 2026
the Kia/Hyundai SDK jumped a major version that way, without anybody
choosing it, and its new status format broke place names in the trip log
and held the charging guard shut. The rule since then:

* every requirement (requirements.txt, requirements-vehicles.txt, the
  extra names in the Dockerfile and the install button's catalog) is
  capped below the next major version — for 0.x below the next minor,
  which is what semver calls a breaking step there;
* ``--installed`` (run inside the built image, see the promote workflow)
  prints the versions the build actually resolved and fails when one of
  them lies outside its specifier.

Moving a cap is a deliberate change: edit the line, and the promote run
for that release runs the connector tests against the image built from it.

    python3 scripts/check_dependency_bounds.py            # static check
    python3 scripts/check_dependency_bounds.py --installed
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

_NAME = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$')


def _from_requirements(path):
    out = []
    for ln in (ROOT / path).read_text(encoding='utf-8').splitlines():
        spec = ln.split('#')[0].strip()
        if spec:
            out.append((path, spec))
    return out


def _from_dockerfile():
    """Quoted specs on `pip install` lines (the requirement files are
    read separately). A bare unquoted name there is also reported."""
    text = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    # Join continuation lines so one RUN is one string.
    text = re.sub(r'\\\n', ' ', text)
    out = []
    for ln in text.splitlines():
        if 'pip install' not in ln:
            continue
        for part in re.split(r'&&|;', ln):
            if 'pip install' not in part:
                continue
            woerter = part.split('pip install', 1)[1].split()
            ueberspringen = False
            for w in woerter:
                if ueberspringen:
                    ueberspringen = False
                    continue
                if w in ('-r', '--requirement', '-c', '--constraint'):
                    ueberspringen = True
                    continue
                if w.startswith('-') or w.startswith('"$(') or w.startswith('$('):
                    continue
                w = w.strip('"\'')
                if re.match(r'^[A-Za-z]', w):
                    out.append(('Dockerfile', w))
    return out


def _from_catalog():
    sys.path.insert(0, str(ROOT))
    from services.vehicle.catalog import PACKAGES
    return [('catalog', p) for pkgs in PACKAGES.values() for p in pkgs]


def all_specs():
    return (_from_requirements('requirements.txt')
            + _from_requirements('requirements-vehicles.txt')
            + _from_dockerfile() + _from_catalog())


def has_upper_bound(spec: str) -> bool:
    m = _NAME.match(spec)
    rest = m.group(2) if m else ''
    return bool(re.search(r'(<|==|~=)', rest))


def static_problems():
    probleme = []
    for quelle, spec in all_specs():
        if not has_upper_bound(spec):
            probleme.append('%s: %r has no upper bound' % (quelle, spec))
    # The catalog and the image must agree on each package's specifier.
    req = {}
    for _, spec in (_from_requirements('requirements-vehicles.txt')
                    + _from_requirements('requirements.txt')):
        m = _NAME.match(spec)
        req[m.group(1).lower()] = m.group(2).replace(' ', '')
    for _, spec in _from_catalog():
        m = _NAME.match(spec)
        name = m.group(1).lower()
        if name in req and req[name] != m.group(2).replace(' ', ''):
            probleme.append('catalog %r differs from the requirement files (%s)'
                            % (spec, req[name]))
    return probleme


def installed_problems():
    from importlib import metadata
    from packaging.requirements import Requirement
    probleme = []
    gesehen = set()
    for quelle, spec in all_specs():
        if quelle == 'catalog':
            continue            # on-demand names (selenium) are not in the image
        r = Requirement(spec)
        if r.name.lower() in gesehen:
            continue
        gesehen.add(r.name.lower())
        try:
            v = metadata.version(r.name)
        except metadata.PackageNotFoundError:
            probleme.append('%s: %s is not installed' % (quelle, r.name))
            continue
        ok = r.specifier.contains(v, prereleases=True)
        print('%-40s %-14s %-22s %s' % (r.name, v, str(r.specifier) or '-',
                                        'ok' if ok else 'OUTSIDE'))
        if not ok:
            probleme.append('%s: %s %s is outside %s' % (quelle, r.name, v, r.specifier))
    return probleme


def main(argv):
    probleme = static_problems()
    if '--installed' in argv:
        probleme += installed_problems()
    for p in probleme:
        print('ERROR: ' + p)
    if not probleme:
        print('dependency bounds ok')
    return 1 if probleme else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
