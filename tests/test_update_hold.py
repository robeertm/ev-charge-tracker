# -*- coding: utf-8 -*-
"""An update hold must survive somebody pressing the button.

A machine part-way through a migration is the case this was written
for: the new release is right for every other install and wrong for
this one, and "wrong" means a user clicks and their system stops
working. Hiding the button is not enough — the gate has to be in the
route and in ``apply_update``, the same way the container gate and the
charging gate already are.

What is pinned here:

  * when a hold is in force, and when it is not,
  * that it fails **closed** — an unreadable hold file still holds,
  * that neither ``force`` nor ``allow_in_container`` lifts it, and
    that nothing is even downloaded,
  * that all three update exclude lists protect the hold file, so a
    release cannot delete a hold placed against it,
  * that the settings page shows the reason instead of a button
    (run in node; skipped where node is not installed).

Run with:
  python3 tests/test_update_hold.py
Exit code is non-zero if any check fails.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import update_hold  # noqa: E402

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


class Umgebung:
    """Hold file in a temp dir, environment restored afterwards."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ev-hold-'))
        self.alt_dir = update_hold._app_dir
        update_hold._app_dir = lambda: self.tmp
        self.alt_env = {k: os.environ.get(k)
                        for k in (update_hold.ENV_REASON, update_hold.ENV_UNTIL)}
        for k in self.alt_env:
            os.environ.pop(k, None)
        return self

    def schreib(self, inhalt):
        pfad = self.tmp / update_hold.HOLD_FILE
        pfad.write_text(inhalt if isinstance(inhalt, str)
                        else json.dumps(inhalt), encoding='utf-8')

    def __exit__(self, *_):
        update_hold._app_dir = self.alt_dir
        for k, v in self.alt_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


print("\n== when is a hold in force ==")
with Umgebung() as u:
    check(update_hold.status() is None, "no file, no variable: updates may proceed")
    check(update_hold.active() is False, "active() agrees")
    check(update_hold.describe() == '', "describe() is empty when nothing holds")

    u.schreib({'reason': 'migration in progress', 'until': 'forever',
               'set_at': '2026-10-03', 'set_by': 'ops'})
    s = update_hold.status()
    check(s is not None, "a hold file holds")
    check(s['reason'] == 'migration in progress', "the reason is passed through verbatim")
    check(s['until'] == 'forever' and s['source'] == 'file', "condition and source reported")
    check(s.get('set_by') == 'ops', "who set it is carried along for the log")
    check('migration in progress' in update_hold.describe(), "describe() names the reason")

print("\n== the condition is re-evaluated, not baked in ==")
# 🔴 The point of recording a CONDITION: a VM's data directory is copied
# into the container that replaces it. A hold that had stored its verdict
# would arrive with that copy and freeze the very thing it waited for.
with Umgebung() as u:
    u.schreib({'reason': 'until this runs in a container', 'until': 'container'})
    alt = update_hold._condition_met
    try:
        update_hold._condition_met = lambda b: False
        check(update_hold.status() is not None, "not a container yet: still held")
        update_hold._condition_met = lambda b: b == 'container'
        check(update_hold.status() is None, "now a container: the hold lifted itself")
    finally:
        update_hold._condition_met = alt

with Umgebung() as u:
    u.schreib({'reason': 'x', 'until': 'wenn-der-mond-blau-ist'})
    s = update_hold.status()
    check(s is not None and s['until'] == 'forever',
          "an unknown condition is never met — it degrades to 'forever'")

print("\n== it fails CLOSED ==")
# The opposite of platform_service.own_tls_card_useful, deliberately:
# there an unanswerable question costs an untidy card, here it costs a
# broken install.
with Umgebung() as u:
    u.schreib('{ das ist kein JSON')
    s = update_hold.status()
    check(s is not None, "a hold file that cannot be parsed still holds")
    check('UPDATE_HOLD.json' in s['reason'],
          "and says where to look instead of inventing a reason")
    u.schreib('["eine Liste"]')
    check(update_hold.status() is not None, "JSON that is not an object still holds")
    u.schreib({})
    check(update_hold.status() is not None, "an empty object still holds")

print("\n== the environment variable, for a compose file ==")
with Umgebung() as u:
    os.environ[update_hold.ENV_REASON] = 'pinned by the operator'
    s = update_hold.status()
    check(s is not None and s['source'] == 'env', "the variable holds too")
    check(s['until'] == 'forever', "without a condition it is 'forever'")
    os.environ[update_hold.ENV_UNTIL] = 'container'
    s = update_hold.status()
    check(s is not None and s['until'] == 'container', "the condition is read too")
    os.environ[update_hold.ENV_REASON] = '   '
    check(update_hold.status() is None, "a blank reason is no hold at all")


print("\n== apply_update refuses, and downloads nothing ==")
import updater  # noqa: E402

with Umgebung() as u:
    u.schreib({'reason': 'held', 'until': 'forever'})
    geladen = []
    alt_dl = updater._download_zip
    try:
        updater._download_zip = lambda *a, **k: geladen.append(a)
        ok = updater.apply_update('https://example.invalid/x.zip', '9.9.9')
        check(ok is False, "apply_update returns False while a hold is in force")
        # 🔑 Both overrides exist for the gates BELOW this one. Neither is
        # an override for a standing decision by whoever runs the machine.
        ok = updater.apply_update('https://example.invalid/x.zip', '9.9.9',
                                  force=True, allow_in_container=True)
        check(ok is False, "force=True and allow_in_container=True do not lift it")
        check(geladen == [], "nothing was downloaded before refusing")
    finally:
        updater._download_zip = alt_dl


print("\n== a release may not delete a hold placed against it ==")
import updater_helper  # noqa: E402
from services import update_service  # noqa: E402

for name, menge in (('updater._EXCLUDE_NAMES', updater._EXCLUDE_NAMES),
                    ('updater_helper.EXCLUDE_NAMES', updater_helper.EXCLUDE_NAMES),
                    ('update_service.EXCLUDE_NAMES', update_service.EXCLUDE_NAMES)):
    check(update_hold.HOLD_FILE in menge, f"{name} protects {update_hold.HOLD_FILE}")


print("\n== the route refuses, it does not merely hide ==")
quelle = (ROOT / 'app.py').read_text(encoding='utf-8')
check("'error': 'update_held'" in quelle,
      "/api/update/install answers update_held")
check("'hold': sperre" in quelle, "/api/update/check reports the hold")
# The hold must be decided BEFORE the charging gate: no amount of
# waiting or forcing changes its answer, so asking about a charging car
# first would only offer the user a way out that does not exist.
i_hold = quelle.find("'error': 'update_held'")
i_laden = quelle.find("'error': 'vehicle_charging'")
check(0 < i_hold < i_laden, "the hold is decided before the charging gate")


print("\n== the settings page shows the reason instead of a button ==")
blatt = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
bloecke = re.findall(r'<script\b[^>]*>(.*?)</script>', blatt, re.S)
ziel = [b for b in bloecke if 'btnCheckUpdate' in b and 'data.hold' in b]
check(len(ziel) == 1, "the update block knows about data.hold")

if not shutil.which('node') or not ziel:
    print("  (node not installed — the browser branch is not exercised here)")
else:
    js = re.sub(r'\{\{.*?\}\}', '"T"', ziel[0], flags=re.S)
    js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
    anker = '    let pendingZipUrl = null;'
    # Reach doCheck out of its OWN closure, so this also proves the
    # escaping helper is in scope there — using the page's other
    # escapeHtml would be a ReferenceError and an empty box.
    js = js.replace(anker, anker + '\n    global.__p = (...a) => doCheck(...a);', 1)
    probe = '''
const _el = {};
const mach = () => ({ innerHTML: '', disabled: false, addEventListener(){} });
global.document = { getElementById: (id) => (_el[id] = _el[id] || mach()),
                    addEventListener(){} };
const kasten = document.getElementById('updateResult');
global.T = new Proxy({}, { get: (_, k) => '<' + String(k) + '>' });
global.window = global;
global.navigator = { clipboard: { writeText: async () => {} } };
global.fetch = async () => ({ ok: true, json: async () => ANTWORT });
global.ANTWORT = null;
''' + js + '''
let schlecht = 0;
const pruef = (b, t) => { console.log((b ? '  ok: ' : '  FAIL: ') + t); if (!b) schlecht++; };
const antw = (u, extra) => Object.assign({ update_available: true, latest: '9.9.9',
    current: '1.0.0', release_url: 'https://example.invalid/r', hold: u }, extra || {});
(async () => {
  global.ANTWORT = antw({ reason: 'Grund <script>x</script> & "y"',
                          until: 'container', source: 'file' });
  await __p();
  let h = kasten.innerHTML;
  pruef(h.includes('<upd_hold_title>'), 'the hold heading is shown');
  pruef(h.includes('<upd_hold_until_container>'), 'the condition is named');
  pruef(h.includes('9.9.9'), 'the new version is still named');
  pruef(h.includes('example.invalid/r'), 'the release notes stay reachable');
  pruef(!h.includes('btnInstallUpdate'), 'NO install button');
  pruef(!h.includes('<script>x'), 'the free-text reason is escaped');
  pruef(h.includes('&lt;script&gt;') && h.includes('&quot;y&quot;'), 'angle brackets and quotes replaced');
  pruef(!h.includes('<app_error>'), 'the branch ran without throwing');
  global.ANTWORT = antw({ reason: 'x', until: 'forever', source: 'file' },
                        { by_image: true, helper_available: true });
  await __p();
  h = kasten.innerHTML;
  pruef(h.includes('<upd_hold_title>'), 'the hold outranks the helper offer');
  pruef(!h.includes('btnInstallUpdate'), 'the helper button is not offered');
  pruef(!h.includes('<upd_hold_until_container>'), 'no container line for forever');
  global.ANTWORT = antw(null);
  await __p();
  pruef(kasten.innerHTML.includes('btnInstallUpdate'), 'without a hold the button is back');
  pruef(!kasten.innerHTML.includes('<upd_hold_title>'), 'and no hold message');
  process.exit(schlecht ? 1 : 0);
})();
'''
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / 'hold.js'
        f.write_text(probe, encoding='utf-8')
        r = subprocess.run(['node', str(f)], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode != 0:
            if r.stderr.strip():
                print(r.stderr.strip()[-600:])
            _failures.append('the settings page branch')

print()
if _failures:
    print(f"🔴 {len(_failures)} check(s) failed")
    sys.exit(1)
print("✅ all checks passed")
