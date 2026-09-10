#!/usr/bin/env bash
# EV Charge Tracker — install (or update) the container on any Linux box.
#
#   curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/docker-install.sh | bash
#
# Run it again later and it updates instead of breaking: the compose file is
# refreshed, the image pulled, the container recreated — your .env and your
# data volume are never touched.
#
# This is the Docker path. deploy/install.sh is the other one: a native
# systemd install, which is what you want if you need the LUKS-encrypted
# data directory.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main"
DIR="${EV_DIR:-$HOME/ev-charge-tracker-docker}"
PORT="${EV_PORT:-7654}"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 1. Is Docker there, and may we use it? ────────────────────────────────
say "Checking Docker"
command -v docker >/dev/null 2>&1 || die \
"Docker is not installed. The one-liner from https://get.docker.com installs it:
    curl -fsSL https://get.docker.com | sh
Then run this script again."

# Being in the docker group is the usual reason 'docker ps' fails for a
# non-root user. Say so plainly instead of failing later with a socket error.
DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
    warn "using sudo for docker"
  else
    die "Cannot talk to the Docker daemon. Either add yourself to the docker group:
    sudo usermod -aG docker \$USER   # then log out and back in
or run this script with sudo."
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || die \
"The Docker Compose plugin is missing. On Debian/Ubuntu:
    sudo apt-get install -y docker-compose-plugin"
ok "$($DOCKER --version)"

# ── 2. Fetch the compose file and the updater helper ──────────────────────
say "Installing into $DIR"
mkdir -p "$DIR/deploy"
cd "$DIR"

hole() {   # hole <remote path> <local path>
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$REPO_RAW/$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$2" "$REPO_RAW/$1"
  else
    die "Neither curl nor wget is available."
  fi
}

hole docker-compose.yml docker-compose.yml
ok "docker-compose.yml"

# The sibling container that performs updates when the app asks for one.
# Fetched every run, including on an existing install: that is how a
# machine set up before this existed gains the button.
hole deploy/updater.sh deploy/updater.sh
chmod +x deploy/updater.sh
ok "deploy/updater.sh (one-click updates)"

# ── 3. Configuration — created once, never overwritten ────────────────────
# Re-running the installer must not roll a new SECRET_KEY: that would log
# everyone out, and it would throw away an ENTSO-E token entered by hand.
if [ -f .env ]; then
  ok ".env kept (already configured)"
else
  if command -v openssl >/dev/null 2>&1; then
    KEY="$(openssl rand -hex 32)"
  else
    KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  [ -n "$KEY" ] || die "Could not generate a secret key."
  umask 077
  cat > .env <<EOF
# Written by docker-install.sh. Keep this file private.
SECRET_KEY=$KEY
ENTSOE_API_KEY=
EV_PORT=$PORT
TZ=$(cat /etc/timezone 2>/dev/null || echo Europe/Berlin)
EOF
  ok ".env created with a fresh secret key (mode 600)"
fi

# ── 4. Pull and start ─────────────────────────────────────────────────────
say "Pulling the image (no build — this is a download)"
$DOCKER compose pull
say "Starting"
$DOCKER compose up -d

# ── 5. Wait until it actually answers ─────────────────────────────────────
# "Container started" is not the same as "the app is up". Ask the app.
say "Waiting for the app"
URL="http://localhost:${PORT}"
for _ in $(seq 1 60); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
    ok "$(curl -fsS "$URL/api/health")"
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '\n\033[1;32mReady.\033[0m Open %s' "$URL"
    [ -n "${IP:-}" ] && printf '  (or http://%s:%s from another machine)' "$IP" "$PORT"
    printf '\n\nThe setup wizard runs on first visit.\n'
    printf 'Update later:  cd %s && %s compose pull && %s compose up -d\n' "$DIR" "$DOCKER" "$DOCKER"
    printf 'Logs:          cd %s && %s compose logs -f\n\n' "$DIR" "$DOCKER"
    exit 0
  fi
  sleep 2
done

warn "The app did not answer within two minutes."
warn "Its own log usually says why:"
printf '    cd %s && %s compose logs --tail 50\n\n' "$DIR" "$DOCKER"
exit 1
