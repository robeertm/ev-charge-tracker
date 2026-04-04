#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# EV Charge Tracker — Start Script (Linux / macOS)
# Creates a virtual environment, installs dependencies,
# and launches the web application.
# ─────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/venv"
PYTHON=""
APP_PORT="${APP_PORT:-7654}"

echo ""
echo "⚡ EV Charge Tracker"
echo "───────────────────────────────────────"

# ── Find Python 3 ──────────────────────────────────────
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" = "3" ] && [ "$minor" -ge 8 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.8+ nicht gefunden!"
    echo "   Bitte installiere Python: https://www.python.org/downloads/"
    echo ""
    read -p "Drücke Enter zum Beenden..."
    exit 1
fi

echo "🐍 Python: $($PYTHON --version)"

# ── Create/activate venv ───────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Erstelle virtuelle Umgebung..."
    "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null || "$PYTHON" -m venv --without-pip "$VENV_DIR"
    # Ensure pip is available
    if [ ! -f "$VENV_DIR/bin/pip" ] && [ ! -f "$VENV_DIR/bin/pip3" ]; then
        echo "   ↳ Installiere pip..."
        curl -sS https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python" 2>/dev/null \
            || wget -qO- https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python" 2>/dev/null
    fi
    echo "   ✓ venv erstellt in $VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"
echo "   ✓ venv aktiviert"

# ── Install/update dependencies ────────────────────────
if [ ! -f "$VENV_DIR/.deps_installed" ] || [ "requirements.txt" -nt "$VENV_DIR/.deps_installed" ]; then
    echo "📥 Installiere Abhängigkeiten..."
    pip install --upgrade pip -q 2>/dev/null
    pip install -r requirements.txt -q
    touch "$VENV_DIR/.deps_installed"
    echo "   ✓ Alle Pakete installiert"
else
    echo "   ✓ Abhängigkeiten aktuell"
fi

# ── Initialize database if needed ──────────────────────
if [ ! -f "data/ev_tracker.db" ]; then
    echo "🗄️  Initialisiere Datenbank..."
    python -c "from app import create_app; app = create_app(); app.app_context().push()"
    echo "   ✓ Datenbank erstellt"
fi

# ── Get local IP for smartphone access ─────────────────
LOCAL_IP=""
if command -v hostname &>/dev/null; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
fi
if [ -z "$LOCAL_IP" ] && command -v ifconfig &>/dev/null; then
    LOCAL_IP=$(ifconfig | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1)
fi

echo ""
echo "🚀 Starte EV Charge Tracker..."
echo "───────────────────────────────────────"
echo "🌐 Browser:    http://localhost:$APP_PORT"
if [ -n "$LOCAL_IP" ]; then
    echo "📱 Smartphone: http://$LOCAL_IP:$APP_PORT"
fi
echo "───────────────────────────────────────"
echo "   Drücke Ctrl+C zum Beenden"
echo ""

# ── Open browser (background, don't fail) ──────────────
(sleep 1.5 && {
    if command -v xdg-open &>/dev/null; then
        xdg-open "http://localhost:$APP_PORT" 2>/dev/null
    elif command -v open &>/dev/null; then
        open "http://localhost:$APP_PORT" 2>/dev/null
    fi
}) &

# ── Launch Flask ───────────────────────────────────────
python app.py
