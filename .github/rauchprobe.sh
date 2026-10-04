#!/usr/bin/env bash
# Faehrt EIN veroeffentlichtes Abbild an und sagt, ob es laeuft.
#
#   ./.github/rauchprobe.sh <abbild> <datenverzeichnis> <name> <port> [erwartete_version]
#
# 🔴 WARUM DAS HIER STEHT (04.10.2026)
#
# Der Bau hat `:latest` gesetzt, sobald ein Commit auf main landete. Geprueft
# wurde vorher nichts am gebauten Abbild. Bei DocuSort ging so eine Fassung
# hinaus, die auf bestehenden Datenbanken nicht startete — verteilt binnen einer
# Stunde, und ohne Eingriff nicht zurueckgekommen.
#
# Drei Fragen, und jede einzelne war dort die, die den Fehler gesehen haette:
#
#   1. antwortet `/api/health` mit 200?   (HTTP 000 war die Antwort)
#   2. steht ein Absturz im Protokoll?    (7 Treffer waren es)
#   3. haengt es in einer Startschleife?  (restarting, restarts=10)
#
# 🔑 Die erste Frage allein genuegt nicht: ein Container in einer
# Neustartschleife antwortet zwischendurch. Darum alle drei.
#
# 🔑 Und weil `/api/health` die Version mitliefert, sagt die Probe auch, WELCHE
# Fassung da wirklich laeuft — nicht welche draufsteht.
set -euo pipefail

ABBILD="${1:?Abbild fehlt}"
DATEN="${2:?Datenverzeichnis fehlt}"
NAME="${3:?Name fehlt}"
PORT="${4:?Port fehlt}"
ERWARTET="${5:-}"

mkdir -p "$DATEN"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "── $NAME: $ABBILD auf $DATEN (Port $PORT) ──"
# 🔴 `--restart unless-stopped` ist kein Beiwerk: ohne Neustartregel bleibt ein
# Absturz ein stilles `exited` und `RestartCount` steht auf 0. Mit ihr sieht man
# die Startschleife, die der Besitzer auch sieht.
#
# 🔑 SECRET_KEY muss gesetzt sein — ohne ihn startet die App absichtlich nicht
# (compose verlangt ihn mit `:?`). Hier ein Wegwerfwert, der nie irgendwo
# ankommt: die Probe wird nach jedem Lauf geloescht.
docker run -d --name "$NAME" --restart unless-stopped \
  -p "127.0.0.1:${PORT}:7654" \
  -v "${DATEN}:/app/data" \
  -e SECRET_KEY="rauchprobe-$(date +%s)-$RANDOM" \
  -e TZ=Europe/Berlin \
  "$ABBILD" >/dev/null

fehler=0
antwort=""
code="000"
# 🔑 120 s, nicht 30: beim ersten Start legt die App ihre Datenbank an, und auf
# arm64 unter QEMU ist das deutlich langsamer als hier.
for _ in $(seq 1 60); do
    code="$(curl -s -o /tmp/rauch-$NAME.json -w '%{http_code}' "http://127.0.0.1:${PORT}/api/health" || echo 000)"
    if [ "$code" = "200" ]; then antwort="$(cat /tmp/rauch-$NAME.json)"; break; fi
    sleep 2
done
if [ "$code" = "200" ]; then
    echo "  OK   /api/health antwortet 200  — $antwort"
else
    echo "  🔴   /api/health antwortet $code"
    fehler=1
fi

LAEUFT=""
if [ -n "$antwort" ]; then
    LAEUFT="$(printf '%s' "$antwort" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ -n "$ERWARTET" ]; then
        if [ "$LAEUFT" = "$ERWARTET" ]; then
            echo "  OK   es laeuft wirklich $ERWARTET"
        else
            echo "  🔴   erwartet war $ERWARTET, es laeuft $LAEUFT"
            fehler=1
        fi
    else
        echo "       es laeuft: ${LAEUFT:-unbekannt}"
    fi
fi

# 🔑 `grep -c` mit `|| true`: ohne Treffer gibt grep 1 zurueck und `set -e`
# wuerde die Probe beenden, bevor sie ihr Urteil sagen kann.
treffer="$(docker logs "$NAME" 2>&1 | grep -c 'Traceback\|OperationalError\|SyntaxError' || true)"
if [ "$treffer" = "0" ]; then
    echo "  OK   kein Absturz im Protokoll"
else
    echo "  🔴   $treffer Absturz-Spuren im Protokoll"
    fehler=1
fi

status="$(docker inspect -f '{{.State.Status}}' "$NAME")"
neustarts="$(docker inspect -f '{{.RestartCount}}' "$NAME")"
if [ "$status" = "running" ] && [ "$neustarts" = "0" ]; then
    echo "  OK   laeuft, keine Neustarts"
else
    echo "  🔴   Status=$status Neustarts=$neustarts"
    fehler=1
fi

if [ "$fehler" != "0" ]; then
    echo "── Protokoll von $NAME ──"
    docker logs "$NAME" 2>&1 | tail -60
fi

# Der Container geht weg, das Datenverzeichnis BLEIBT — der Aufstiegstest
# braucht genau das, was die vorige Fassung hinterlassen hat.
docker rm -f "$NAME" >/dev/null 2>&1 || true
rm -f "/tmp/rauch-$NAME.json"

if [ "$fehler" != "0" ]; then
    echo "🔴 NICHT AUSLIEFERN — $ABBILD"
    exit 1
fi
echo "✅ $ABBILD ist angefahren und laeuft"
