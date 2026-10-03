"""The settings sidebar and the settings cards must agree.

The sidebar is built from one list; the cards are written out further
down. For a long time those were two places saying the same thing, and
they drifted: ``ssl`` sat unconditionally in the list while its card was
behind an ``{% if %}``, so on every install that hid the card the sidebar
kept a link to a section that was not on the page. The reverse happened
too — the ``syncaudit`` card existed with no way to reach it from the
sidebar.

This pins the agreement:

  * every entry in the sidebar list has a card with that id,
  * every card on the page has an entry in the sidebar list,
  * every id that is conditional (listed in ``zeigen``) has its card
    guarded by that same variable — not by a second condition that can
    drift away from it.

It reads the template as text on purpose. Rendering it would need the
whole application context, and the bug this guards against is a
disagreement between two places in the source, which is exactly what a
reader of the source can check.

Run with:
  python3 tests/test_settings_sections.py
Exit code is non-zero if any check fails.
"""
import os
import re
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VORLAGE = os.path.join(ROOT, 'templates', 'settings.html')

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


with open(VORLAGE, encoding='utf-8') as f:
    html = f.read()

# ── the sidebar list ─────────────────────────────────────────────────
m = re.search(r"\{%\s*set settings_sections\s*=\s*\[(.*?)\]\s*%\}", html, re.S)
if not m:
    print("  FAIL: settings_sections list not found at all")
    sys.exit(1)
liste = re.findall(r"\(\s*'([a-z_]+)'", m.group(1))
print(f"\n== sidebar list: {len(liste)} entries ==")
check(len(liste) == len(set(liste)), "no id appears twice in the list")

# ── the conditional ids ──────────────────────────────────────────────
mz = re.search(r"\{%\s*set zeigen\s*=\s*\{(.*?)\}\s*%\}", html, re.S)
check(mz is not None, "the `zeigen` map exists")
bedingt = re.findall(r"'([a-z_]+)'\s*:", mz.group(1)) if mz else []
print(f"== conditional ids: {', '.join(bedingt) or 'none'} ==")

# ── the cards actually written out ───────────────────────────────────
karten = re.findall(r'id="sec-([a-z_]+)"', html)
print(f"== cards in the page: {len(karten)} ==")
check(len(karten) == len(set(karten)), "no card id appears twice")

# ── they must agree, in both directions ──────────────────────────────
ohne_karte = [s for s in liste if s not in karten]
ohne_eintrag = [k for k in karten if k not in liste]
check(not ohne_karte,
      f"every sidebar entry has a card (orphans: {ohne_karte or 'none'})")
check(not ohne_eintrag,
      f"every card has a sidebar entry (unreachable: {ohne_eintrag or 'none'})")

# ── a conditional id must be guarded by `zeigen`, not by something else
for sid in bedingt:
    stelle = html.find(f'id="sec-{sid}"')
    if stelle < 0:
        _failures.append(f"{sid} is in `zeigen` but has no card")
        print(f"  FAIL: {sid} is in `zeigen` but has no card")
        continue
    davor = html[max(0, stelle - 400):stelle]
    check(f"zeigen['{sid}']" in davor,
          f"the {sid} card is guarded by zeigen['{sid}']")

# ── the sidebar loop must filter on the same map ─────────────────────
check("zeigen.get(sid, True)" in html,
      "the sidebar loop filters on the same `zeigen` map")

# ── nobody may reintroduce a second, independent gate ────────────────
check("hide_ssl_card" not in html,
      "the old per-viewer `hide_ssl_card` gate is gone from the template")

# ── every template must COMPILE, not merely parse ────────────────────
# 🔴 This is here because a `| bool` filter — which neither Jinja nor
# Flask has — once reached three live installs. It is not a parse error:
# `Environment.parse()` accepts an unknown filter without a murmur and
# only `compile()` raises, so reading the template as text (which is what
# the checks above do) and parsing it both said "fine" while every
# request for the page answered 500.
#
# A bare environment is enough: measured, none of these templates needs a
# filter that Flask adds on top of Jinja. If one ever does, this check
# will say so and the filter belongs in the list here, deliberately.
print("\n== every template compiles ==")
try:
    import jinja2
except ImportError:
    print("  (jinja2 not installed — skipped)")
else:
    umgebung = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(ROOT, 'templates')),
        autoescape=True)
    vorlagen = sorted(Path(os.path.join(ROOT, 'templates')).rglob('*.html'))
    check(len(vorlagen) > 0, "there are templates to check")
    kaputt = []
    for datei in vorlagen:
        try:
            umgebung.compile(datei.read_text(encoding='utf-8'),
                             filename=str(datei))
        except Exception as e:
            kaputt.append(f"{datei.name}: {type(e).__name__}: {e}")
    check(not kaputt,
          f"all {len(vorlagen)} templates compile"
          + ("" if not kaputt else f" — broken: {kaputt[:3]}"))

print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
