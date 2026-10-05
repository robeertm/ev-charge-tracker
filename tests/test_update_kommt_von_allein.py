# -*- coding: utf-8 -*-
"""Updates arrive by themselves — without dropping the charging guard.

The arrangement: a release publishes the versioned image, the promote
workflow smoke-tests it and only then moves `latest`, and a watcher
container recreates the app. Nobody clicks anything, and the interface
stops announcing versions nobody has to act on.

🔴 The one thing that must survive all of that is the charging guard. The
container carried `watchtower.enable=false` precisely because an update
stops the sync loop, and the loop is what notices a charge ending. The
guard therefore moves into Watchtower's `pre-update` hook — measured to
cancel an update on a non-zero exit — and the answer comes from
``services.charge_gate``, the same code the update endpoint uses, so
there is one definition and not two that can drift.

Two traps this pins down:

* **A charge end writes no row.** ``is_charging`` is not a tracked field,
  so the newest row keeps saying "charging" after the cable comes out.
  Next to a button with a force option that is an annoyance; in front of
  an automatic updater it is a silent permanent block. Hence the age
  bound — three hours, chosen because across 294 charging rows of a real
  install the longest gap to the next row was 1.95 h.
* **"How loud are we" is not "may this happen".** The update mode decides
  what is *said*. It must never touch the gate.

All figures invented. Run with:
  python3 -m pytest tests/test_update_kommt_von_allein.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DATEN = tempfile.mkdtemp(prefix='evupd-')
os.environ.setdefault('EV_DATA_DIR', _DATEN)

from services import charge_gate as G                                  # noqa: E402
from services import update_mode as M                                  # noqa: E402

SPRACHEN = ('de', 'en', 'es', 'fr', 'it', 'nl')


# ── die Ladeprüfung, ohne Anwendung ──────────────────────────────────

def _db_mit(zeilen):
    """A database file carrying exactly these (is_charging, timestamp)."""
    pfad = os.path.join(tempfile.mkdtemp(prefix='evdb-'), 'ev_tracker.db')
    con = sqlite3.connect(pfad)
    con.execute('CREATE TABLE vehicle_syncs (id INTEGER PRIMARY KEY,'
                ' is_charging INTEGER, timestamp TEXT)')
    for i, (laedt, stempel) in enumerate(zeilen, 1):
        con.execute('INSERT INTO vehicle_syncs VALUES (?,?,?)',
                    (i, 1 if laedt else 0, stempel.strftime('%Y-%m-%d %H:%M:%S.%f')))
    con.commit(); con.close()
    return pfad


def _mit_db(monkeypatch, pfad):
    """Pin the hook's path: database file, no application context.

    🔴 Both are needed. ``charge_state`` picks its way in by asking
    ``has_app_context()``, and another test file in the same session can
    leave a context pushed (several here push one and never pop it). With
    a context lying around, the ORM path runs and the file below is never
    read — the probe would then measure the wrong half and pass or fail
    depending on which files ran before it. Measured, not feared: these
    five checks failed in exactly that order before this line existed.
    """
    monkeypatch.setattr('flask.has_app_context', lambda: False)
    monkeypatch.setattr(G, '_db_path', lambda: pfad)


def test_eine_frische_installation_wird_nicht_blockiert(monkeypatch):
    """No database at all is "no car yet" — not unclear evidence."""
    _mit_db(monkeypatch, '/gibt/es/nicht/ev_tracker.db')
    z = G.charge_state()
    assert z['charging'] is False and z['reason'] == 'no data'


def test_eine_leere_datenbank_blockiert_auch_nicht(monkeypatch):
    _mit_db(monkeypatch, _db_mit([]))
    assert G.charge_in_progress() is False


def test_waehrend_einer_ladung_wird_verschoben(monkeypatch):
    jetzt = datetime.now()
    _mit_db(monkeypatch, _db_mit([(False, jetzt - timedelta(hours=2)),
                                  (True, jetzt - timedelta(minutes=4))]))
    z = G.charge_state()
    assert z['charging'] is True and z['reason'] == 'charging'


def test_ohne_ladung_darf_das_update_laufen(monkeypatch):
    jetzt = datetime.now()
    _mit_db(monkeypatch, _db_mit([(True, jetzt - timedelta(hours=5)),
                                  (False, jetzt - timedelta(minutes=3))]))
    assert G.charge_in_progress() is False


def test_eine_alte_laedt_zeile_blockiert_nicht_fuer_immer(monkeypatch):
    """The trap: a charge END writes no row, so this row would otherwise
    hold the gate shut until something unrelated changes."""
    jetzt = datetime.now()
    alt = jetzt - timedelta(hours=G.CHARGE_ROW_MAX_AGE_H + 0.5)
    _mit_db(monkeypatch, _db_mit([(True, alt)]))
    z = G.charge_state()
    assert z['charging'] is False
    assert 'stale' in z['reason']


def test_knapp_innerhalb_der_grenze_gilt_die_ladung_noch(monkeypatch):
    jetzt = datetime.now()
    frisch = jetzt - timedelta(hours=G.CHARGE_ROW_MAX_AGE_H - 0.5)
    _mit_db(monkeypatch, _db_mit([(True, frisch)]))
    assert G.charge_in_progress() is True


def test_die_grenze_ist_grosszuegiger_als_die_gemessene_luecke():
    """1.95 h was the longest observed gap after a charging row."""
    assert G.CHARGE_ROW_MAX_AGE_H >= 2.5


def test_unlesbare_belege_halten_das_tor_zu(monkeypatch):
    kaputt = os.path.join(tempfile.mkdtemp(prefix='evbad-'), 'ev_tracker.db')
    with open(kaputt, 'wb') as fh:
        fh.write(b'das ist keine datenbank')
    _mit_db(monkeypatch, kaputt)
    z = G.charge_state()
    assert z['charging'] is True
    assert z['reason'].startswith('unreadable')


def test_ein_unlesbarer_zeitstempel_haelt_das_tor_auch_zu(monkeypatch):
    pfad = os.path.join(tempfile.mkdtemp(prefix='evts-'), 'ev_tracker.db')
    con = sqlite3.connect(pfad)
    con.execute('CREATE TABLE vehicle_syncs (id INTEGER PRIMARY KEY,'
                ' is_charging INTEGER, timestamp TEXT)')
    con.execute("INSERT INTO vehicle_syncs VALUES (1, 1, 'irgendwann')")
    con.commit(); con.close()
    _mit_db(monkeypatch, pfad)
    assert G.charge_in_progress() is True


def test_mit_anwendungskontext_wird_ueber_das_modell_gefragt():
    """The endpoint's half of the same function — asked through the ORM,
    so it sees the session it is already in."""
    import tempfile as _tf
    from flask import Flask
    from models.database import db, Vehicle, VehicleSync
    os.environ.setdefault('EV_DATA_DIR', _tf.mkdtemp(prefix='evorm-'))
    a = Flask(__name__)
    a.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    a.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(a)
    ctx = a.app_context(); ctx.push()
    try:
        db.create_all()
        db.session.add(Vehicle(id=1, name='Pruefwagen'))
        db.session.add(VehicleSync(vehicle_id=1, is_charging=True,
                                   timestamp=datetime.now() - timedelta(minutes=2)))
        db.session.commit()
        assert G.charge_in_progress() is True
    finally:
        ctx.pop()


# ── der Hook, den Watchtower aufruft ─────────────────────────────────

def _hook(pfad_db):
    """Run deploy/pre-update.sh against a prepared database."""
    umgebung = dict(os.environ)
    umgebung['EV_APP_DIR'] = ROOT
    umgebung['EV_DATA_DIR'] = os.path.dirname(pfad_db)
    return subprocess.run(['sh', os.path.join(ROOT, 'deploy', 'pre-update.sh')],
                          capture_output=True, text=True, env=umgebung, timeout=60)


def test_der_hook_sagt_nein_waehrend_einer_ladung():
    jetzt = datetime.now()
    p = _db_mit([(True, jetzt - timedelta(minutes=2))])
    e = _hook(p)
    assert e.returncode != 0, e.stdout + e.stderr
    assert 'laedt' in e.stdout.lower() or 'verschoben' in e.stdout.lower()


def test_der_hook_sagt_ja_ohne_ladung():
    jetzt = datetime.now()
    p = _db_mit([(False, jetzt - timedelta(minutes=2))])
    e = _hook(p)
    assert e.returncode == 0, e.stdout + e.stderr


def test_der_hook_sagt_nein_wenn_er_nichts_feststellen_kann():
    e = subprocess.run(['sh', os.path.join(ROOT, 'deploy', 'pre-update.sh')],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, EV_APP_DIR='/gibt/es/nicht'))
    assert e.returncode != 0


def test_der_hook_baut_die_anwendung_nicht_auf():
    """create_app() would start the sync loop, the wallbox poll and the
    geocode loop inside this short-lived process — measured, not feared."""
    # 🔴 Ohne die Kommentare pruefen: die Begruendung NENNT create_app,
    # und eine Textsuche ueber die ganze Datei faellt darauf herein (genau
    # dieser Fehler ist mir heute schon einmal unterlaufen).
    zeilen = open(os.path.join(ROOT, 'deploy', 'pre-update.sh'),
                  encoding='utf-8').read().splitlines()
    code = '\n'.join(z for z in zeilen if not z.lstrip().startswith('#'))
    assert 'create_app' not in code


# ── Betriebsart: nur die Lautstärke, nie die Erlaubnis ───────────────

def test_die_vorgabe_ist_manuell(monkeypatch):
    monkeypatch.delenv(M.ENV_MODE, raising=False)
    assert M.mode() == M.MANUAL and M.is_automatic() is False


def test_die_umgebung_schaltet_auf_automatisch(monkeypatch):
    monkeypatch.setenv(M.ENV_MODE, 'auto')
    assert M.is_automatic() is True


def test_ein_tippfehler_schaltet_nichts_ab(monkeypatch):
    monkeypatch.setenv(M.ENV_MODE, 'atuo')
    assert M.mode() == M.MANUAL


def test_die_betriebsart_ruehrt_die_ladepruefung_nicht_an(monkeypatch):
    jetzt = datetime.now()
    _mit_db(monkeypatch, _db_mit([(True, jetzt - timedelta(minutes=1))]))
    for wert in ('auto', 'manual'):
        monkeypatch.setenv(M.ENV_MODE, wert)
        assert G.charge_in_progress() is True, wert


def test_die_meldung_verstummt_nur_in_der_automatik():
    """The endpoint must not notify where nothing is to be done."""
    quelle = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert 'if new_version and not automatisch:' in quelle


# ── alle sechs Sprachen ──────────────────────────────────────────────

def _sprache(l):
    with open(os.path.join(ROOT, 'translations', '%s.json' % l),
              encoding='utf-8') as fh:
        return json.load(fh)


def test_der_neue_hinweis_steht_in_allen_sechs_sprachen():
    for l in SPRACHEN:
        d = _sprache(l)
        assert 'upd.automatic_hint' in d, l
        assert d['upd.automatic_hint'].strip(), l


def test_die_sechs_sprachen_tragen_dieselben_schluessel():
    """Without this a new key reaches one file and the other five render
    raw German to their owners."""
    basis = set(_sprache('de'))
    for l in SPRACHEN[1:]:
        k = set(_sprache(l))
        assert not (basis - k), '%s fehlen: %s' % (l, sorted(basis - k)[:5])
        assert not (k - basis), '%s zusaetzlich: %s' % (l, sorted(k - basis)[:5])
