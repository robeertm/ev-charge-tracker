# -*- coding: utf-8 -*-
"""The charging guard checks a fresh "charging" flag against its raw values.

What happened: a vendor library release reported "charging" on every
cached status that carried a remaining-charge time. Cable out, 0 kW, the
car being driven — and because SoC and odometer kept moving, new rows kept
arriving, every one of them fresh. The age bound never fired and the
Watchtower hook refused every update for a week.

The rule these tests pin down (see ``services.charge_gate``):

* a real charge — power above zero, or SoC rising while parked — keeps the
  gate shut, whatever else the row says;
* three of four pieces of evidence against (0 kW, no cable, driven while
  flagged, SoC fell while flagged) overrule the flag at once;
* after 24 h without a sign of a real charge, two suffice if one of them
  is physical (driven, or no cable);
* mixed values and missing values keep the gate shut.

The rows below follow the shape of the affected install's history (two
syncs per poll, the forced one saying "not charging"), all figures
invented. Run with:
  python3 -m pytest tests/test_ladewaechter_plausibel.py
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
os.environ.setdefault('EV_DATA_DIR', tempfile.mkdtemp(prefix='evlw-'))

from services import charge_gate as G                                  # noqa: E402

JETZT = datetime(2026, 11, 12, 11, 50)

# Raw payload of the false flag: the library says charging, the car says
# connector not fastened and charge state 0.
ROH_FALSCH = json.dumps({
    'ev_battery_is_charging': True, 'ev_battery_is_plugged_in': None,
    'ev_charging_power': 0, 'ev_connector_fastening_state': 0,
    'data': {'Green': {'ChargingInformation': {
        'Charging': {'State': 0, 'RemainTime': 100},
        'ConnectorFastening': {'State': 0}}}},
})
# Raw payload of a real AC charge: plugged, charging state 1.
ROH_ECHT = json.dumps({
    'ev_battery_is_charging': True, 'ev_battery_is_plugged_in': True,
    'ev_connector_fastening_state': 1,
    'data': {'Green': {'ChargingInformation': {
        'Charging': {'State': 1}, 'ConnectorFastening': {'State': 1}}}},
})


def _z(minuten_vorher, soc, odo, laedt, kw, roh=None, auto=1):
    """One sync row, ``minuten_vorher`` before JETZT."""
    return (auto, JETZT - timedelta(minutes=minuten_vorher), soc, odo,
            laedt, kw, roh)


def _db(zeilen):
    pfad = os.path.join(tempfile.mkdtemp(prefix='evlwdb-'), 'ev_tracker.db')
    con = sqlite3.connect(pfad)
    con.execute('CREATE TABLE vehicle_syncs (id INTEGER PRIMARY KEY,'
                ' vehicle_id INTEGER, timestamp TEXT, soc_percent INTEGER,'
                ' odometer_km INTEGER, is_charging INTEGER,'
                ' charge_power_kw REAL, raw_json TEXT)')
    for auto, t, soc, odo, laedt, kw, roh in zeilen:
        con.execute('INSERT INTO vehicle_syncs (vehicle_id, timestamp,'
                    ' soc_percent, odometer_km, is_charging, charge_power_kw,'
                    ' raw_json) VALUES (?,?,?,?,?,?,?)',
                    (auto, t.strftime('%Y-%m-%d %H:%M:%S.%f'), soc, odo,
                     1 if laedt else 0, kw, roh))
    con.commit(); con.close()
    return pfad


def _mit(monkeypatch, pfad):
    monkeypatch.setattr('flask.has_app_context', lambda: False)
    monkeypatch.setattr(G, '_db_path', lambda: pfad)


# ── die drei Fälle aus dem Auftrag ───────────────────────────────────

def _echte_ladung():
    """Wallbox charge in progress: 11 kW, SoC climbing, odometer standing."""
    return [
        _z(300, 32, 51000, False, 0.0),
        _z(200, 40, 51005, True, 10.6, ROH_ECHT),
        _z(150, 50, 51005, True, 10.6, ROH_ECHT),
        _z(100, 60, 51005, True, 10.7, ROH_ECHT),
        _z(50, 70, 51005, True, 10.6, ROH_ECHT),
        _z(5, 80, 51005, True, 10.7, ROH_ECHT),
    ]


def test_eine_echte_ladung_blockiert_weiter(monkeypatch):
    _mit(monkeypatch, _db(_echte_ladung()))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is True
    assert 'power' in z['reason'] or 'soc_rose' in z['reason']


def _fehlflag():
    """The week-long false flag: the charge ended at 100 % the day before,
    since then every cached sync says "charging" while the car is driven,
    0 kW, connector not fastened; the forced sync after each one says no."""
    return [
        _z(1400, 97, 51005, True, 10.7, ROH_ECHT),       # real charge, day before
        _z(1390, 100, 51005, False, 0.0),                # charge complete
        _z(1290, 98, 51010, True, 0.0, ROH_FALSCH),      # library: "charging"
        _z(1289, 98, 51010, False, 0.0),                 # forced sync: no
        _z(1110, 98, 51010, True, 0.0, ROH_FALSCH),
        _z(1100, 97, 51015, True, 0.0, ROH_FALSCH),
        _z(350, 97, 51015, True, 0.0, ROH_FALSCH),
        _z(290, 95, 51020, True, 0.0, ROH_FALSCH),
        _z(289, 95, 51020, False, 0.0),
        _z(200, 89, 51049, True, 0.0, ROH_FALSCH),
        _z(110, 84, 51080, True, 0.0, ROH_FALSCH),
        _z(80, 83, 51085, True, 0.0, ROH_FALSCH),
        _z(47, 83, 51086, True, 0.0, ROH_FALSCH),
        _z(46, 82, 51086, False, 0.0),
        _z(36, 82, 51086, True, 0.0, ROH_FALSCH),
        _z(26, 82, 51086, True, 0.0, ROH_FALSCH),
        _z(16, 81, 51096, True, 0.0, ROH_FALSCH),
        _z(6, 81, 51096, True, 0.0, ROH_FALSCH),
    ]


def test_das_fehlflag_laesst_das_update_durch(monkeypatch):
    _mit(monkeypatch, _db(_fehlflag()))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is False, z
    assert z['reason'].startswith('implausible charging flag')
    for beleg in ('zero_power', 'no_cable', 'drove', 'soc_fell'):
        assert beleg in z['reason']


def test_gemischte_werte_blockieren_soc_steigt(monkeypatch):
    """0 kW and connector not fastened — but SoC climbs while parked.
    One sign of a real charge outweighs any amount of evidence against."""
    zeilen = [
        _z(120, 60, 51000, True, 0.0, ROH_FALSCH),
        _z(60, 61, 51000, True, 0.0, ROH_FALSCH),
        _z(30, 60, 51010, True, 0.0, ROH_FALSCH),
        _z(5, 59, 51020, True, 0.0, ROH_FALSCH),
    ]
    _mit(monkeypatch, _db(zeilen))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is True
    assert 'soc_rose' in z['reason']


def test_gemischte_werte_blockieren_leistung_gemeldet(monkeypatch):
    """Driving rows and no cable, but the newest row reports 7 kW."""
    zeilen = [
        _z(60, 70, 51000, True, 0.0, ROH_FALSCH),
        _z(30, 69, 51010, True, 0.0, ROH_FALSCH),
        _z(5, 69, 51010, True, 7.2, ROH_FALSCH),
    ]
    _mit(monkeypatch, _db(zeilen))
    assert G.charge_in_progress(now=JETZT) is True


def test_warten_auf_den_ladetimer_blockiert(monkeypatch):
    """Plugged in, waiting for a timer: flag on, 0 kW, nothing moves.
    Only one piece of evidence (zero_power) — the gate stays shut."""
    zeilen = [
        _z(100, 32, 51000, True, 0.0, ROH_ECHT),
        _z(40, 32, 51000, True, 0.0, ROH_ECHT),
        _z(10, 32, 51000, True, 0.0, ROH_ECHT),
    ]
    _mit(monkeypatch, _db(zeilen))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is True
    assert 'zero_power' in z['reason']


def test_fehlende_werte_sind_kein_beleg(monkeypatch):
    """A connector that reports no power and no payload cannot overrule a
    flag even while the odometer rises — that is one piece of evidence."""
    zeilen = [
        _z(60, 70, 51000, True, None),
        _z(30, 70, 51010, True, None),
        _z(5, 70, 51020, True, None),
    ]
    _mit(monkeypatch, _db(zeilen))
    assert G.charge_in_progress(now=JETZT) is True


# ── die 24-Stunden-Stufe ─────────────────────────────────────────────

def _zwei_belege(stunden):
    """Flag on, 0 kW, connector not fastened, car parked (SoC and odometer
    standing) — two pieces of evidence, one physical. Rows come every
    40 min (a 12 V or range wobble writes one)."""
    zeilen = []
    m = int(stunden * 60)
    while m >= 5:
        zeilen.append(_z(m, 80, 51000, True, 0.0, ROH_FALSCH))
        m -= 40
    zeilen.append(_z(5, 80, 51000, True, 0.0, ROH_FALSCH))
    return zeilen


def test_zwei_belege_reichen_vor_24_stunden_nicht(monkeypatch):
    _mit(monkeypatch, _db(_zwei_belege(10)))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is True
    assert 'no_cable' in z['reason'] and 'zero_power' in z['reason']


def test_nach_24_stunden_reichen_zwei_eindeutige_belege(monkeypatch):
    _mit(monkeypatch, _db(_zwei_belege(G.LONG_CLAIM_H + 1)))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is False, z
    assert z['reason'].startswith('implausible')


def test_nach_24_stunden_reicht_kein_einzelner_beleg(monkeypatch):
    """24 h of a flag with only 0 kW (plugged in, timer far away) — still
    no physical evidence, still deferred."""
    zeilen = []
    m = int((G.LONG_CLAIM_H + 2) * 60)
    while m >= 5:
        zeilen.append(_z(m, 50, 51000, True, 0.0, ROH_ECHT))
        m -= 40
    _mit(monkeypatch, _db(zeilen))
    assert G.charge_in_progress(now=JETZT) is True


# ── mehrere Autos, Protokoll, Hook ───────────────────────────────────

def test_ein_ladendes_zweites_auto_blockiert(monkeypatch):
    """A newer idle row of car 2 must not hide car 1's running charge."""
    zeilen = [(1, t, s, o, l, k, r) for (_, t, s, o, l, k, r) in _echte_ladung()]
    zeilen.append(_z(2, 55, 22000, False, 0.0, auto=2))
    _mit(monkeypatch, _db(zeilen))
    assert G.charge_in_progress(now=JETZT) is True


def test_das_fehlflag_eines_autos_blockiert_das_andere_nicht(monkeypatch):
    zeilen = [(1, t, s, o, l, k, r) for (_, t, s, o, l, k, r) in _fehlflag()]
    zeilen.append(_z(3, 55, 22000, False, 0.0, auto=2))
    _mit(monkeypatch, _db(zeilen))
    z = G.charge_state(now=JETZT)
    assert z['charging'] is False
    assert z['reason'].startswith('implausible')


def test_die_begruendung_landet_im_protokoll(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    _mit(monkeypatch, _db(_fehlflag()))
    z = G.charge_state(now=JETZT)
    G.record(z, 'pre-update')
    text = (tmp_path / G.LOG_NAME).read_text(encoding='utf-8')
    assert 'ALLOWED' in text and 'implausible' in text and 'drove' in text
    G.record({'charging': False, 'row_at': None, 'reason': 'idle'}, 'x')
    assert text == (tmp_path / G.LOG_NAME).read_text(encoding='utf-8')


def test_das_protokoll_wirft_nie(monkeypatch):
    import config
    monkeypatch.setattr(config, 'DATA_DIR', '/gibt/es/nicht/und/nie')
    G.record({'charging': True, 'row_at': None, 'reason': 'charging'}, 'x')


def test_der_hook_laesst_das_fehlflag_durch_und_schreibt_den_grund():
    """End to end through deploy/pre-update.sh, timestamps relative to now."""
    verschiebung = datetime.now() - JETZT
    zeilen = [(a, t + verschiebung, s, o, l, k, r)
              for (a, t, s, o, l, k, r) in _fehlflag()]
    pfad = _db(zeilen)
    umgebung = dict(os.environ)
    umgebung['EV_APP_DIR'] = ROOT
    umgebung['EV_DATA_DIR'] = os.path.dirname(pfad)
    e = subprocess.run(['sh', os.path.join(ROOT, 'deploy', 'pre-update.sh')],
                       capture_output=True, text=True, env=umgebung, timeout=60)
    assert e.returncode == 0, e.stdout + e.stderr
    assert 'implausible' in e.stdout
    log = os.path.join(os.path.dirname(pfad), G.LOG_NAME)
    assert 'implausible' in open(log, encoding='utf-8').read()


def test_der_hook_blockiert_die_echte_ladung():
    verschiebung = datetime.now() - JETZT
    zeilen = [(a, t + verschiebung, s, o, l, k, r)
              for (a, t, s, o, l, k, r) in _echte_ladung()]
    pfad = _db(zeilen)
    umgebung = dict(os.environ)
    umgebung['EV_APP_DIR'] = ROOT
    umgebung['EV_DATA_DIR'] = os.path.dirname(pfad)
    e = subprocess.run(['sh', os.path.join(ROOT, 'deploy', 'pre-update.sh')],
                       capture_output=True, text=True, env=umgebung, timeout=60)
    assert e.returncode != 0, e.stdout + e.stderr
