#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# EV Charge Tracker — GitHub Setup (einmalig ausführen)
# Initialisiert Git, erstellt den ersten Commit und
# pusht zu GitHub.
# ─────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REPO_NAME="robeertm/ev-charge-tracker"

echo ""
echo "⚡ EV Charge Tracker — GitHub Setup"
echo "───────────────────────────────────────"
echo ""

# ── Prüfe ob gh CLI installiert ist ──────────────────────
if ! command -v gh &>/dev/null; then
    echo "❌ GitHub CLI (gh) ist nicht installiert."
    echo ""
    echo "   Installieren mit Homebrew:"
    echo "   brew install gh"
    echo ""
    echo "   Dann einloggen:"
    echo "   gh auth login"
    echo ""
    read -p "Drücke Enter zum Beenden..." dummy
    exit 1
fi

# ── Prüfe ob gh eingeloggt ist ───────────────────────────
if ! gh auth status &>/dev/null 2>&1; then
    echo "❌ GitHub CLI ist nicht eingeloggt."
    echo ""
    echo "   Bitte zuerst einloggen:"
    echo "   gh auth login"
    echo ""
    read -p "Drücke Enter zum Beenden..." dummy
    exit 1
fi

echo "✅ GitHub CLI ist installiert und eingeloggt"

# ── Git initialisieren (falls nötig) ────────────────────
if [ ! -d ".git" ]; then
    echo "📁 Initialisiere Git-Repository..."
    git init -b main
else
    echo "✅ Git-Repository existiert bereits"
fi

# ── .gitignore prüfen ───────────────────────────────────
if [ ! -f ".gitignore" ]; then
    echo "⚠️  Keine .gitignore gefunden — bitte prüfen!"
    read -p "Drücke Enter zum Fortfahren..." dummy
fi

# ── Alten venv ausschließen ──────────────────────────────
if [ -d "venv" ]; then
    echo "⚠️  venv/ Ordner gefunden — wird nicht committet (steht in .gitignore)"
fi

# ── Erster Commit ────────────────────────────────────────
if [ -z "$(git log --oneline 2>/dev/null | head -1)" ]; then
    echo "📝 Erstelle ersten Commit..."
    git add .gitignore README.md app.py config.py import_gsheet.py updater.py \
            requirements.txt start.sh start.bat start.command setup-github.command \
            models/ services/ templates/ static/
    git commit -m "Initial commit: EV Charge Tracker v1.0

Flask web app for tracking Kia Niro EV charging data.
Features: Dashboard, mobile input, ENTSO-E CO2 integration,
Google Sheet import, auto-updater."
    echo "✅ Erster Commit erstellt"
else
    echo "✅ Commits existieren bereits"
fi

# ── GitHub Repo erstellen ────────────────────────────────
echo ""
echo "🌐 Erstelle GitHub Repository: $REPO_NAME"
echo ""
read -p "   Soll das Repo public sein? (j/n, Standard: j): " VISIBILITY
VISIBILITY="${VISIBILITY:-j}"

if [[ "$VISIBILITY" == "j" || "$VISIBILITY" == "J" || "$VISIBILITY" == "y" ]]; then
    VIS_FLAG="--public"
    echo "   → Public Repository"
else
    VIS_FLAG="--private"
    echo "   → Private Repository"
fi

# Prüfe ob Repo bereits existiert
if gh repo view "$REPO_NAME" &>/dev/null 2>&1; then
    echo "✅ Repository $REPO_NAME existiert bereits auf GitHub"
    # Remote setzen falls nötig
    if ! git remote get-url origin &>/dev/null 2>&1; then
        git remote add origin "https://github.com/$REPO_NAME.git"
    fi
else
    echo "   Erstelle Repository auf GitHub..."
    gh repo create "$REPO_NAME" $VIS_FLAG --source=. --description "EV Charge Tracker — Local web app for tracking electric vehicle charging costs and CO₂ emissions"
    echo "✅ Repository erstellt: https://github.com/$REPO_NAME"
fi

# ── Push ─────────────────────────────────────────────────
echo "🚀 Pushe zu GitHub..."
git push -u origin main
echo "✅ Code erfolgreich zu GitHub gepusht!"

echo ""
echo "───────────────────────────────────────"
echo "✅ Fertig! Dein Repo: https://github.com/$REPO_NAME"
echo ""
echo "Nächste Schritte:"
echo "  1. Google Sheet als CSV exportieren"
echo "  2. python import_gsheet.py <datei>.csv"
echo "  3. ENTSO-E Token in Einstellungen eintragen"
echo "  4. App starten mit: ./start.command"
echo "───────────────────────────────────────"
echo ""
read -p "Drücke Enter zum Beenden..." dummy
