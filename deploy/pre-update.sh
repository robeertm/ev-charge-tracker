#!/bin/sh
# Watchtower pre-update hook — the charging guard, in Watchtower's own
# mechanism.
#
# WHY THIS EXISTS
# ---------------
# Updates arrive on their own: a release moves the `latest` tag (after the
# smoke test) and Watchtower recreates this container. Nobody clicks
# anything, which is the point — but it also means nobody is there to
# notice that the car is charging. An update stops the sync loop, and the
# loop is what sees a charge end; miss that and the charge is filed wrong
# or not at all.
#
# Before this hook existed the container carried
# `com.centurylinklabs.watchtower.enable=false` for exactly that reason.
# The guard is not dropped — it moves here.
#
# 🔴 A NON-ZERO EXIT CANCELS THE UPDATE. Measured, not assumed: a
# throwaway container whose hook exits non-zero kept its container ID
# across a `--run-once` that found a new image, while an identical one
# whose hook exits 0 was recreated. Do not "improve" this script into
# something that always succeeds.
#
# 🔴 THE LOG LINE LIES. Watchtower logs `Pre-update command executed
# success=false` even for an exit code of 0. Only the outcome tells you
# what happened — read the container ID, not that line.
#
# The answer comes from services/charge_gate, the same code the update
# endpoint uses, so there is one definition of "a charge is running". It
# reads the database file read-only and deliberately does NOT build the
# application: create_app() would start the sync loop, the wallbox poll
# and the geocode loop inside this short-lived process.
set -u

APP_DIR="${EV_APP_DIR:-/app}"

cd "$APP_DIR" 2>/dev/null || {
    # Cannot even reach the app: unclear evidence, so the gate stays shut.
    echo "pre-update: $APP_DIR nicht erreichbar — Update wird verschoben"
    exit 1
}

# python3 zuerst: im Abbild gibt es beides, auf anderen Systemen oft nur
# eines von beiden — und ein Hook, der am Interpreternamen scheitert,
# verschiebt jedes Update auf immer.
PY_BIN=""
for kandidat in python3 python; do
    command -v "$kandidat" >/dev/null 2>&1 && { PY_BIN="$kandidat"; break; }
done
[ -n "$PY_BIN" ] || {
    echo "pre-update: kein Python gefunden — Update wird verschoben"
    exit 1
}

PYTHONPATH="$APP_DIR" "$PY_BIN" - <<'PY'
import sys
try:
    from services.charge_gate import charge_state
    zustand = charge_state()
except Exception as e:                      # fail closed, with the reason
    print("pre-update: Ladezustand nicht feststellbar (%s) — verschoben" % e)
    sys.exit(1)
if zustand['charging']:
    print("pre-update: Fahrzeug laedt (%s, Stand %s) — Update verschoben"
          % (zustand['reason'], zustand['row_at']))
    sys.exit(1)
print("pre-update: keine Ladung (%s) — Update darf laufen" % zustand['reason'])
sys.exit(0)
PY
