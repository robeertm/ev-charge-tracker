# -*- coding: utf-8 -*-
"""Every page must stay readable in dark mode, too.

How dark mode works here: ``base.html`` loads Bootstrap 5.3 and switches the
theme by setting ``data-bs-theme="light|dark"`` on ``<html>`` (saved in
``localStorage['ev_theme']``, toggled by ``#btnTheme``). Bootstrap's
theme-aware classes (``text-body``, ``bg-body-tertiary``, ``alert-light``,
``btn-outline-secondary`` …) follow the attribute by themselves. Where the
app keeps a fixed-colour class it adds a ``[data-bs-theme="dark"] …``
override to the one central file ``static/css/custom.css``.

A handful of Bootstrap classes do NOT follow the theme — they are the same
colour in both modes — and break on a dark page:

  * ``btn-outline-dark``  near-black border and text on a near-black page:
                          the button is invisible (the OBD page's
                          "Ins Batteriezertifikat übernehmen" link was).
  * ``btn-dark``, ``border-dark``, ``link-dark``  the same problem.
  * ``table-light``       a glaring light-grey stripe inside a dark table.
  * ``bg-light``, ``bg-white``  a fixed light box; the text inside follows
                          the theme and turns light-on-light.
  * ``text-dark``, ``text-black``  fixed dark text; on a dark background it
                          disappears.

The rule this test enforces:

  1. ``btn-outline-dark``, ``btn-dark``, ``border-dark``, ``link-dark`` and
     ``table-light`` may only be used if ``custom.css`` carries a
     ``[data-bs-theme="dark"] .<class>`` override for exactly that class.
  2. ``bg-light`` / ``bg-white`` / ``text-dark`` / ``text-black`` are fine
     only as a self-contained pair whose colours do not depend on the theme:
     fixed dark text on a fixed light background in the SAME class string
     (``badge bg-warning text-dark``, ``badge bg-light text-dark``). A lone
     ``bg-light`` or a lone ``text-dark`` is the bug.
  3. ``bg-white`` on a ``card-header`` is fine — ``custom.css`` maps
     ``.card-header.bg-white`` to the card background in dark mode.
  4. No inline ``style="background: #fff / white / #f8f9fa …"``: use
     ``bg-body-tertiary`` (identical in light mode, dark in dark mode).

Use instead: ``bg-body-tertiary`` / ``bg-body-secondary`` for light boxes,
``text-body`` / ``text-body-emphasis`` for dark text, or add a dark override
to ``custom.css`` (rule 1).

"Same class string" means the text between the nearest quote characters
around the class, so Jinja (``{{ 'bg-warning text-dark' if … }}``) and JS
(``'<span class="badge bg-warning text-dark">'``) are judged correctly.
Jinja and HTML comments are ignored. Static on purpose: no Flask app needed.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / 'static' / 'css' / 'custom.css'

# Rule 1: classes that need a dark-mode override in custom.css.
NEEDS_OVERRIDE = ('btn-outline-dark', 'btn-dark', 'border-dark', 'link-dark', 'table-light')
# Rule 2: fixed light backgrounds and fixed dark text — only valid as a pair.
LIGHT_BG = ('bg-light', 'bg-white')
DARK_TEXT = ('text-dark', 'text-black')
# Backgrounds that stay light in BOTH themes, so fixed dark text on them is fine.
# (*-subtle variants are theme-aware and turn dark — deliberately not listed.)
FIXED_LIGHT_BG = ('bg-warning', 'bg-info', 'bg-light', 'bg-white',
                  'btn-warning', 'btn-info', 'btn-light')
# Rule 4. Near-white = white, #fff, or six hex digits whose every channel is
# >= f8 (#f8f9fa, #fafafa, #ffffff) — not merely "starts with f" (#fd7e14).
INLINE_LIGHT_BG = re.compile(
    r"""style\s*=\s*(["'])[^"']*?background(?:-color)?\s*:\s*"""
    r"""(?:\#fff\b|\#(?:f[89a-f]){3}\b|white\b)""",
    re.IGNORECASE,
)

QUOTES = '"\'`'


def _sources():
    files = sorted((ROOT / 'templates').rglob('*.html'))
    files += sorted((ROOT / 'static' / 'js').rglob('*.js'))
    return [f for f in files if '.min.' not in f.name]


def _strip_comments(text):
    """Blank out Jinja/HTML comments but keep line numbers."""
    blank = lambda m: re.sub(r'[^\n]', ' ', m.group(0))
    text = re.sub(r'\{#.*?#\}', blank, text, flags=re.S)
    return re.sub(r'<!--.*?-->', blank, text, flags=re.S)


def _class_re(cls):
    return re.compile(r'(?<![\w-])' + re.escape(cls) + r'(?![\w-])')


def _has(segment, classes):
    return any(_class_re(c).search(segment) for c in classes)


def _enclosing(text, start, end):
    """The text between the nearest quote characters around [start, end)."""
    lo = max(text.rfind(q, 0, start) for q in QUOTES) + 1
    his = [i for i in (text.find(q, end) for q in QUOTES) if i != -1]
    hi = min(his) if his else len(text)
    return text[lo:hi]


def _dark_rules():
    """{selector: declarations} for every ``[data-bs-theme="dark"]`` rule."""
    css = re.sub(r'/\*.*?\*/', '', CSS.read_text(encoding='utf-8'), flags=re.S)
    rules = {}
    for sels, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
        for sel in sels.split(','):
            sel = ' '.join(sel.split())
            if sel.startswith('[data-bs-theme="dark"]'):
                rules[sel] = rules.get(sel, '') + body
    return rules


def _overridden(cls):
    return f'[data-bs-theme="dark"] .{cls}' in _dark_rules()


def find_problems(text, overridden=_overridden):
    """List of (line, class, reason) for one source text."""
    text = _strip_comments(text)
    out = []

    def line(pos):
        return text.count('\n', 0, pos) + 1

    for cls in NEEDS_OVERRIDE:
        for m in _class_re(cls).finditer(text):
            if not overridden(cls):
                out.append((line(m.start()), cls,
                            f'no [data-bs-theme="dark"] .{cls} override in custom.css'))

    for cls in LIGHT_BG:
        for m in _class_re(cls).finditer(text):
            seg = _enclosing(text, m.start(), m.end())
            if cls == 'bg-white' and _has(seg, ('card-header',)):
                continue
            if not _has(seg, DARK_TEXT):
                out.append((line(m.start()), cls,
                            'fixed light background without fixed dark text — '
                            'use bg-body-tertiary'))

    for cls in DARK_TEXT:
        for m in _class_re(cls).finditer(text):
            seg = _enclosing(text, m.start(), m.end())
            if not _has(seg, FIXED_LIGHT_BG):
                out.append((line(m.start()), cls,
                            'fixed dark text without a fixed light background — '
                            'use text-body / text-body-emphasis'))

    for m in INLINE_LIGHT_BG.finditer(text):
        out.append((line(m.start()), 'style=background',
                    'inline light background — use bg-body-tertiary'))
    return out


# --------------------------------------------------------------------------
# The real files
# --------------------------------------------------------------------------

def test_theme_switch_is_the_data_bs_theme_attribute():
    """The CSS overrides key on [data-bs-theme="dark"] — keep it that way."""
    base = (ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')
    assert 'data-bs-theme' in base
    assert "setAttribute('data-bs-theme'" in base
    assert 'css/custom.css' in base


@pytest.mark.parametrize('path', _sources(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_class_that_vanishes_in_dark_mode(path):
    problems = find_problems(path.read_text(encoding='utf-8'))
    assert not problems, '\n'.join(
        f'{path.relative_to(ROOT)}:{ln}: {cls}: {why}' for ln, cls, why in problems)


def test_outline_dark_override_turns_the_button_light():
    body = _dark_rules().get('[data-bs-theme="dark"] .btn-outline-dark', '')
    for var in ('--bs-btn-color', '--bs-btn-border-color',
                '--bs-btn-hover-bg', '--bs-btn-hover-color'):
        assert var in body, f'.btn-outline-dark dark override lacks {var}'


def test_table_light_override_follows_the_theme():
    body = _dark_rules().get('[data-bs-theme="dark"] .table-light', '')
    for var in ('--bs-table-bg', '--bs-table-color'):
        assert var in body, f'.table-light dark override lacks {var}'


def test_card_header_bg_white_override_exists():
    body = _dark_rules().get('[data-bs-theme="dark"] .card-header.bg-white', '')
    assert 'background-color' in body


# --------------------------------------------------------------------------
# The checker itself — so a refactor of the test cannot quietly pass all
# --------------------------------------------------------------------------

@pytest.mark.parametrize('snippet', [
    '<span class="badge bg-warning text-dark">x</span>',
    '<span class="badge bg-light text-dark ms-1">x</span>',
    "{{ 'bg-warning text-dark' if a else 'bg-secondary' }}",
    '<span class="badge {% if s == \'overdue\' %}bg-danger{% else %}bg-warning text-dark{% endif %}">',
    "html = '<span class=\"badge bg-warning text-dark me-1\">' + n;",
    "const cls = { yellow: 'bg-warning text-dark' }[d];",
    '<div class="card-header bg-white fw-bold">',
    '<div class="form-control bg-body-tertiary">',
    '{# old: btn-outline-dark bg-light text-dark #}',
    '<!-- was style="background:#f8f9fa" -->',
    '<a class="btn btn-outline-darkish">',
    '<div style="background:#fd7e14;color:white;border:2px solid white">',
])
def test_checker_allows(snippet):
    assert find_problems(snippet, overridden=lambda c: False) == []


@pytest.mark.parametrize('snippet,cls', [
    ('<a class="btn btn-sm btn-outline-dark">', 'btn-outline-dark'),
    ('<thead class="table-light">', 'table-light'),
    ('<div class="form-control bg-light">', 'bg-light'),
    ('<span class="badge bg-light text-muted border">', 'bg-light'),
    ('<div class="p-2 bg-white">', 'bg-white'),
    ('<div class="fw-bold text-dark">', 'text-dark'),
    ('<span class="badge bg-warning-subtle text-dark">', 'text-dark'),
    ('<pre style="margin:0; background: #f8f9fa;">', 'style=background'),
    ('<input readonly style="background:#fff">', 'style=background'),
    ('<div style="padding:1rem; background-color: white">', 'style=background'),
])
def test_checker_flags(snippet, cls):
    found = [c for _, c, _ in find_problems(snippet, overridden=lambda c: False)]
    assert cls in found


def test_checker_accepts_override():
    snippet = '<a class="btn btn-outline-dark">'
    assert find_problems(snippet, overridden=lambda c: c == 'btn-outline-dark') == []
