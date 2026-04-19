#!/bin/bash
# EV Charge Tracker — one-shot installer for Debian/Ubuntu.
#
# Usage (recommended):
#   curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/install.sh | sudo bash
#
# Env overrides:
#   EV_REPO=<git-url>       Override repo URL (default: upstream)
#   EV_BRANCH=<name>        Checkout a specific branch/tag (default: main)
#   EV_APP_DIR=<path>       Install path (default: /srv/ev-data/app)
#   EV_USER=<name>          Service user (default: ev-tracker)
#   EV_WITH_TAILSCALE=1     Install Tailscale automatically (default: interactive prompt)
#   EV_UNATTENDED=1         Skip all interactive prompts, assume defaults
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────
REPO="${EV_REPO:-https://github.com/robeertm/ev-charge-tracker.git}"
BRANCH="${EV_BRANCH:-main}"
APP_DIR="${EV_APP_DIR:-/srv/ev-data/app}"
SERVICE_USER="${EV_USER:-ev-tracker}"
UNATTENDED="${EV_UNATTENDED:-0}"
WITH_TAILSCALE="${EV_WITH_TAILSCALE:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()  { printf "${BLUE}▶${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
die()  { printf "${RED}✗ %s${NC}\n" "$*" >&2; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    die "Muss als root laufen. Versuch: curl -fsSL … | sudo bash"
fi

if [ ! -f /etc/os-release ]; then
    die "/etc/os-release fehlt — unbekannte Distribution."
fi
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
    debian:*|ubuntu:*|*:debian|*:ubuntu)
        ok "Distribution: ${PRETTY_NAME:-$ID}"
        ;;
    *)
        warn "Nicht-Debian-System erkannt (${PRETTY_NAME:-$ID}). Installer ist für Debian/Ubuntu gebaut, Paketinstallation könnte scheitern."
        ;;
esac

# Prompt helper: reads from /dev/tty so curl|bash pipelines work.
ask_yes_no() {
    local prompt="$1" default="${2:-n}" reply
    if [ "$UNATTENDED" = "1" ]; then
        [ "$default" = "y" ] && return 0 || return 1
    fi
    local hint="[y/N]"
    [ "$default" = "y" ] && hint="[Y/n]"
    if ! [ -t 0 ] && [ ! -r /dev/tty ]; then
        # No tty at all (e.g. piped without /dev/tty) — use default silently.
        [ "$default" = "y" ] && return 0 || return 1
    fi
    printf "${BOLD}? %s %s: ${NC}" "$prompt" "$hint" > /dev/tty
    read -r reply < /dev/tty || reply=""
    [ -z "$reply" ] && reply="$default"
    case "$reply" in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}

# ── Package install ───────────────────────────────────────────────
log "Paketliste wird aktualisiert …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

log "Installiere Basis-Pakete …"
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl sqlite3 \
    cryptsetup \
    fail2ban unattended-upgrades \
    ca-certificates > /dev/null
ok "Pakete installiert."

# Debian trixie ships Python 3.13 as /usr/bin/python3. Older releases may
# need a PPA or backport; bail out loudly if <3.11.
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
case "$PY_VER" in
    3.11|3.12|3.13|3.14) ok "Python $PY_VER vorhanden." ;;
    *) die "Python $PY_VER ist zu alt. EV-Tracker braucht ≥ 3.11. Aktualisiere das OS oder installiere python3.13 manuell." ;;
esac

# ── Service user ──────────────────────────────────────────────────
if id "$SERVICE_USER" &>/dev/null; then
    ok "Service-User $SERVICE_USER existiert bereits."
else
    log "Lege Service-User $SERVICE_USER an …"
    useradd -r -m -d "/home/$SERVICE_USER" -s /bin/bash "$SERVICE_USER"
    ok "User $SERVICE_USER erstellt."
fi

# ── Repo klonen / updaten ─────────────────────────────────────────
APP_PARENT=$(dirname "$APP_DIR")
mkdir -p "$APP_PARENT"
chown "$SERVICE_USER":"$SERVICE_USER" "$APP_PARENT" || true

if [ -d "$APP_DIR/.git" ]; then
    log "Repo bereits vorhanden unter $APP_DIR — aktualisiere …"
    sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch --tags --quiet origin
    sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
    sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH" || true
else
    log "Klone Repo ($BRANCH) nach $APP_DIR …"
    sudo -u "$SERVICE_USER" git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
ok "Code synchronisiert."

# ── Python venv + deps ────────────────────────────────────────────
log "Baue Python-venv …"
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
log "Installiere requirements.txt — kann 2–5 min dauern (pandas/matplotlib/numpy)."
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --progress-bar on -r "$APP_DIR/requirements.txt"
ok "Python-Abhängigkeiten installiert."

# ── systemd unit ──────────────────────────────────────────────────
UNIT_PATH=/etc/systemd/system/ev-tracker.service
if [ -f "$APP_DIR/deploy/ev-tracker.service" ]; then
    install -m 644 "$APP_DIR/deploy/ev-tracker.service" "$UNIT_PATH"
else
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=EV Charge Tracker
After=network-online.target
Wants=network-online.target
ConditionPathExists=$APP_DIR/venv/bin/python

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi
ok "systemd-Unit installiert: $UNIT_PATH"

# ── sudoers ───────────────────────────────────────────────────────
SUDOERS_PATH=/etc/sudoers.d/ev-tracker
if [ -f "$APP_DIR/deploy/sudoers.ev-tracker" ]; then
    install -m 440 "$APP_DIR/deploy/sudoers.ev-tracker" "$SUDOERS_PATH"
    # Replace placeholder user if file uses one
    sed -i "s/@@SERVICE_USER@@/$SERVICE_USER/g" "$SUDOERS_PATH"
else
    cat > "$SUDOERS_PATH" <<EOF
# Allow the ev-tracker service user to restart its own service and run
# security-updates/reboot from the web UI.
$SERVICE_USER ALL=(root) NOPASSWD: /bin/systemctl restart ev-tracker.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/unattended-upgrade -v
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/unattended-upgrade --dry-run -v
$SERVICE_USER ALL=(root) NOPASSWD: /sbin/shutdown -r now
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/chpasswd
EOF
    chmod 440 "$SUDOERS_PATH"
fi
# visudo check so a broken rule can't lock the box.
if ! visudo -c -f "$SUDOERS_PATH" > /dev/null; then
    rm -f "$SUDOERS_PATH"
    die "sudoers-Datei hat Syntaxfehler und wurde wieder entfernt."
fi
ok "sudoers-Regel installiert: $SUDOERS_PATH"

# ── optional: LUKS unlock helper ──────────────────────────────────
if [ -f "$APP_DIR/deploy/ev-unlock" ]; then
    install -m 755 "$APP_DIR/deploy/ev-unlock" /usr/local/bin/ev-unlock
    # Append LUKS-specific sudoers rules
    if ! grep -q "ev-unlock" "$SUDOERS_PATH"; then
        {
            echo ""
            echo "# LUKS unlock helper (only relevant if /srv/ev-data is on an encrypted volume)"
            echo "$SERVICE_USER ALL=(root) NOPASSWD: /usr/local/bin/ev-unlock"
            echo "$SERVICE_USER ALL=(root) NOPASSWD: /usr/local/bin/ev-unlock --stdin --no-start"
            echo "$SERVICE_USER ALL=(root) NOPASSWD: /sbin/cryptsetup luksChangeKey *"
        } >> "$SUDOERS_PATH"
        visudo -c -f "$SUDOERS_PATH" > /dev/null || die "sudoers-Datei nach LUKS-Anhang fehlerhaft."
        ok "LUKS-Helper /usr/local/bin/ev-unlock installiert."
    fi
fi

# ── Service starten ───────────────────────────────────────────────
log "Aktiviere und starte ev-tracker.service …"
systemctl daemon-reload
systemctl enable --quiet ev-tracker.service
systemctl restart ev-tracker.service
sleep 2
if systemctl is-active --quiet ev-tracker.service; then
    ok "ev-tracker.service läuft."
else
    warn "Service-Start fehlgeschlagen. Log:"
    journalctl -u ev-tracker.service -n 30 --no-pager || true
    die "Abbruch — Service nicht aktiv."
fi

# ── Optional: Tailscale ───────────────────────────────────────────
install_tailscale() {
    if command -v tailscale &>/dev/null; then
        ok "Tailscale bereits installiert."
    else
        log "Installiere Tailscale …"
        curl -fsSL https://tailscale.com/install.sh | sh > /dev/null
        ok "Tailscale installiert."
    fi
    log "Starte 'tailscale up --ssh' — folge dem Login-URL im Browser."
    tailscale up --ssh || warn "Tailscale-Login nicht abgeschlossen. Später: 'sudo tailscale up --ssh'"
    if tailscale status --json 2>/dev/null | grep -q '"BackendState": "Running"'; then
        log "Aktiviere 'tailscale serve --https=443 → 127.0.0.1:7654' …"
        tailscale serve reset 2>/dev/null || true
        tailscale serve --bg --https=443 http://127.0.0.1:7654 || warn "Tailscale Serve fehlgeschlagen."
        HOST_DNS=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || echo "")
        [ -n "$HOST_DNS" ] && ok "HTTPS erreichbar unter https://$HOST_DNS"
    fi
}

case "$WITH_TAILSCALE" in
    1|yes|true) install_tailscale ;;
    0|no|false) ok "Tailscale übersprungen (EV_WITH_TAILSCALE=$WITH_TAILSCALE)." ;;
    *)
        if ask_yes_no "Tailscale jetzt installieren und HTTPS-Zugang einrichten?" y; then
            install_tailscale
        fi
        ;;
esac

# ── Fertig ────────────────────────────────────────────────────────
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<deine-lan-ip>")
cat <<EOF

${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${GREEN}${BOLD}  EV Charge Tracker läuft!${NC}
${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

Web-UI:       http://${LAN_IP}:7654
Service-User: ${SERVICE_USER}
Install-Dir:  ${APP_DIR}
Daten-DB:     ${APP_DIR}/data/ev_tracker.db

Nächste Schritte im Browser:
  1. Passwort für Web-Login setzen (Einstellungen → Sicherheit)
  2. Fahrzeug anbinden (Einstellungen → Fahrzeug-API)
  3. Optional: HTTPS aktivieren (Einstellungen → HTTPS, oder Tailscale)

Logs anschauen:    journalctl -u ev-tracker.service -f
Service neustart:  sudo systemctl restart ev-tracker.service
Updates:           Im Web-UI unter Einstellungen → Updates

EOF
