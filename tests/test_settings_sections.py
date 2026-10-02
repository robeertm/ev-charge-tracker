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

print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
