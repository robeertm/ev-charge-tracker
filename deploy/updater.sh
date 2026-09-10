#!/bin/sh
# EV Charge Tracker — container self-update helper.
#
# WHY THIS EXISTS
# ---------------
# A container cannot replace its own image from the inside: the process
# doing the replacing would be killed by the replacement. So the app
# cannot update itself, and telling the owner to "run docker compose
# pull" assumes they are sitting at a shell on the host. Someone driving
# through Australia with the server in Spain is not.
#
# So this runs as a small sibling container next to the app, and the app
# asks it for an update by dropping a file into a shared volume.
#
# WHY A FILE AND NOT AN API
# -------------------------
# The app never gets Docker access. It writes a marker; this loop reads
# it. That is the same drop-box shape the release broker uses, and it
# means a compromised web app cannot ask Docker for anything at all.
#
# 🔴 THE FILE IS A TRIGGER, NOT AN INSTRUCTION. Its contents are never
# read into a command. Everything this script may do is written here,
# fixed: pull THIS project's images and recreate THIS service. Even a
# fully compromised app can therefore only ever cause "update to the
# published image" — never an arbitrary container, image or command.
#
# 🔴 --no-deps IS NOT OPTIONAL. Without it compose also recreates the
# services the app depends on, which in some setups includes the thing
# this script is talking through — it would cut its own line mid-update.
set -eu

INBOX="${EV_UPDATER_INBOX:-/inbox}"
PROJECT="${EV_UPDATER_PROJECT:-/project}"
SERVICE="${EV_UPDATER_SERVICE:-ev-charge-tracker}"
INTERVAL="${EV_UPDATER_INTERVAL:-5}"

# 🔴 THE PROJECT NAME MUST BE PINNED.
#
# Compose derives it from the working directory, and ours is the mount
# point "/project" — not the host directory the stack was created from.
# Left to guess, compose believes it is looking at a DIFFERENT project,
# creates a second set of networks and volumes and then collides on the
# fixed container name:
#
#   Error response from daemon: Conflict. The container name
#   "/ev-charge-tracker" is already in use ...
#
# Measured, not imagined: the first live run of this script failed
# exactly there, with the app left running on the old version.
#
# So we ask Docker who we are. Our own container carries the compose
# labels, including the host path the stack really lives at — which is
# also what relative paths in the compose file have to resolve against,
# because the daemon binds host paths, not ours.
ermittle_projekt() {
    selbst="$(hostname)"
    PROJEKT="$(docker inspect --format \
        '{{index .Config.Labels "com.docker.compose.project"}}' \
        "$selbst" 2>/dev/null || true)"
    HOSTDIR="$(docker inspect --format \
        '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
        "$selbst" 2>/dev/null || true)"
}

compose_args() {
    # Three flags, and each one is here because leaving it out broke a
    # real run:
    #
    #   -f                  the file WE can read (our mount), not the
    #                       host path, which does not exist in here.
    #   --project-name      or compose invents one from "/project", finds
    #                       no existing stack and collides on the
    #                       container name.
    #   --project-directory the HOST path, because the daemon resolves
    #                       relative bind mounts against it.
    #
    # 🔴 And --env-file, because --project-directory also moves where
    # compose looks for ".env" — to a host path we cannot read. The very
    # next run then died on "required variable SECRET_KEY is missing".
    set -- -f "$PROJECT/docker-compose.yml"
    [ -n "${PROJEKT:-}" ] && set -- "$@" --project-name "$PROJEKT"
    [ -n "${HOSTDIR:-}" ] && set -- "$@" --project-directory "$HOSTDIR"
    [ -f "$PROJECT/.env" ] && set -- "$@" --env-file "$PROJECT/.env"
    printf '%s\n' "$@"
}

REQUEST="$INBOX/update.request"
STATUS="$INBOX/update.status"
LOG="$INBOX/update.log"

schreibe_status() {
    # state, plus an optional detail line. Read by the app for the UI.
    printf '{"state":"%s","detail":"%s","at":"%s"}\n' \
        "$1" "$(printf '%s' "${2:-}" | tr -d '"\\')" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS.tmp"
    mv "$STATUS.tmp" "$STATUS"
}

mkdir -p "$INBOX"
ermittle_projekt
if [ -z "${PROJEKT:-}" ]; then
    # Started outside compose, or without the socket. Say so instead of
    # letting the first update fail with a name conflict.
    schreibe_status unconfigured "compose labels not readable"
    echo "[updater] no compose labels — cannot pin the project name" >> "$LOG"
else
    schreibe_status idle "waiting"
fi
echo "[updater] watching $REQUEST (service=$SERVICE, project=$PROJEKT, dir=$HOSTDIR)" >> "$LOG"

while true; do
    if [ -f "$REQUEST" ]; then
        # Take the request out of the way FIRST. If anything below dies,
        # we must not spin on the same request forever.
        rm -f "$REQUEST"
        schreibe_status pulling "docker compose pull"
        echo "[updater] $(date -u +%FT%TZ) pull" >> "$LOG"
        # shellcheck disable=SC2046  # word splitting is the point here
        if ! docker compose $(compose_args) pull "$SERVICE" >> "$LOG" 2>&1; then
            schreibe_status failed "pull failed — see update.log"
            echo "[updater] pull FAILED" >> "$LOG"
            sleep "$INTERVAL"
            continue
        fi
        schreibe_status restarting "docker compose up -d --no-deps"
        echo "[updater] $(date -u +%FT%TZ) recreate" >> "$LOG"
        # From here the app container is replaced and stops answering.
        # Its next start reports the new version; that is the signal the
        # page polls for. Writing "done" is best effort — the app may
        # already be gone by the time we get here, which is fine.
        # shellcheck disable=SC2046
        if docker compose $(compose_args) up -d --no-deps "$SERVICE" >> "$LOG" 2>&1; then
            schreibe_status done "recreated"
            echo "[updater] done" >> "$LOG"
        else
            schreibe_status failed "recreate failed — see update.log"
            echo "[updater] recreate FAILED" >> "$LOG"
        fi
    fi
    sleep "$INTERVAL"
done
