# -*- coding: utf-8 -*-
"""Der GPS-Zeitstempel muss UMGERECHNET werden, nicht umetikettiert.

`datetime.replace(tzinfo=None)` streicht nur das Etikett. Aus einer Zeit in UTC
wird dadurch eine „Ortszeit", die exakt um den UTC-Versatz zu frueh liegt — zwei
Stunden im Sommer, eine im Winter. Alles, was spaeter das Alter dieses Fixes
misst, haelt ihn dann fuer genau so viel aelter, als er ist; und wer aelter als
eine halbe Stunde ist, gilt als unbrauchbar. Die Folge stand in echten Daten:
statt des Ortes, an dem das Auto stand, wurde „unknown" auf der
Sentinel-Koordinate abgelegt.

Gemessen auf drei laufenden Installationen (Kia und Hyundai, deren SDK
zeitzonenbehaftete Werte liefert): das Alter solcher Zeilen lag bei 120,0 bis
120,1 Minuten — auf die Zehntelminute, nie mit einer echten Streuung. Das Auto,
dessen Schnittstelle gar keinen Zeitstempel liefert, war nicht betroffen; darum
blieb es so lange unentdeckt.

🔑 Diese Proben setzen die Zeitzone SELBST. Eine Probe, die von der Zeitzone des
Rechners abhaengt, auf dem sie laeuft, prueft die Maschine und nicht den Code.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Zone:
    """Zeitzone fuer die Dauer eines Blocks setzen und sauber zuruecknehmen."""

    def __init__(self, name):
        self.name = name
        self.vorher = os.environ.get('TZ')

    def __enter__(self):
        os.environ['TZ'] = self.name
        time.tzset()
        return self

    def __exit__(self, *_):
        if self.vorher is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.vorher
        time.tzset()


class _Status:
    """Das Stueck VehicleStatus, das die Funktion anfasst."""

    def __init__(self, ts):
        self.location_last_updated_at = ts


def _hole():
    """Die Funktion ZUR LAUFZEIT holen — nicht oben importieren.

    Dieselbe Falle wie in den anderen Probendateien: eine Fixtur anderswo leert
    `sys.modules` und importiert neu; ein oben gebundener Name zeigt danach auf
    die alte Fassung.
    """
    from app import _extract_location_last_updated
    return _extract_location_last_updated


def pruefe(name, ist, soll):
    print(("  OK   " if ist == soll else "  FEHL ") + name + "   ist=%r soll=%r" % (ist, soll))
    assert ist == soll, "%s: ist=%r soll=%r" % (name, ist, soll)


# ── Sommerzeit: der Versatz ist zwei Stunden ──────────────────────────────

def test_01_aware_utc_wird_in_sommerzeit_umgerechnet():
    """Der gemessene Fall: 10:00 UTC ist 12:00 Ortszeit, nicht 10:00."""
    with _Zone('Europe/Berlin'):
        f = _hole()
        aware = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
        pruefe('Sommer: aware UTC -> Ortszeit',
               f(_Status(aware), ''), datetime(2026, 10, 8, 12, 0, 0))


def test_02_aware_utc_wird_in_winterzeit_umgerechnet():
    """Gegenprobe ueber die Zeitumstellung: im Winter ist es EINE Stunde.
    Eine feste Zahl im Code waere hier aufgefallen."""
    with _Zone('Europe/Berlin'):
        f = _hole()
        aware = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        pruefe('Winter: aware UTC -> Ortszeit',
               f(_Status(aware), ''), datetime(2026, 1, 15, 11, 0, 0))


def test_03_ein_naiver_wert_wird_NICHT_verschoben():
    """🔑 Die wichtigste Gegenprobe: was schon richtig war, bleibt unberuehrt.
    `astimezone()` auf einem naiven Wert waere eine Umrechnung zu viel."""
    with _Zone('Europe/Berlin'):
        f = _hole()
        naiv = datetime(2026, 10, 8, 12, 0, 0)
        pruefe('naiv bleibt naiv', f(_Status(naiv), ''), naiv)


def test_04_eine_zeichenkette_mit_Z_wird_umgerechnet():
    with _Zone('Europe/Berlin'):
        f = _hole()
        pruefe('"...Z" -> Ortszeit',
               f(_Status('2026-10-08T10:00:00Z'), ''),
               datetime(2026, 10, 8, 12, 0, 0))


def test_05_eine_zeichenkette_ohne_versatz_bleibt_stehen():
    with _Zone('Europe/Berlin'):
        f = _hole()
        pruefe('ohne Versatz unveraendert',
               f(_Status('2026-10-08T12:00:00'), ''),
               datetime(2026, 10, 8, 12, 0, 0))


def test_06_eine_andere_zeitzone_bekommt_ihren_eigenen_versatz():
    """Der Wert haengt an der Zone des Nutzers, nicht an einer Konstanten."""
    with _Zone('UTC'):
        f = _hole()
        aware = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
        pruefe('UTC-Rechner: kein Versatz',
               f(_Status(aware), ''), datetime(2026, 10, 8, 10, 0, 0))


# ── Und die Folge, um die es wirklich geht ────────────────────────────────

def test_07_ein_frischer_fix_gilt_danach_auch_als_frisch():
    """Der Schaden entstand erst eine Ebene weiter: das Alter eines gerade
    gemeldeten Fixes war 120 Minuten, und ab 30 Minuten gilt er als
    unbrauchbar. Hier gemessen wie dort: Abrufzeit minus Fixzeit."""
    with _Zone('Europe/Berlin'):
        f = _hole()
        abruf = datetime(2026, 10, 8, 12, 0, 0)          # Ortszeit, wie timestamp
        fix = datetime(2026, 10, 8, 9, 58, 0, tzinfo=timezone.utc)  # 11:58 Ortszeit
        alter = (abruf - f(_Status(fix), '')).total_seconds() / 60.0
        pruefe('Alter in Minuten', round(alter, 1), 2.0)
        pruefe('und damit unter der 30-Minuten-Grenze', alter <= 30.0, True)


def test_08_ohne_zeitstempel_bleibt_es_None():
    """Die Schnittstelle, die gar keinen liefert, wird nicht erfunden."""
    with _Zone('Europe/Berlin'):
        f = _hole()
        pruefe('kein Zeitstempel -> None', f(_Status(None), ''), None)
