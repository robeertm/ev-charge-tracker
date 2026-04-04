#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# EV Charge Tracker — Projekt-Setup
# Verschiebt nach ~/Projects, Git init, GitHub push,
# öffnet VS Code.
# ─────────────────────────────────────────────────────────
set -e

SOURCE="$HOME/ev-charge-tracker"
TARGET_DIR="$HOME/Projects"
TARGET="$TARGET_DIR/ev-charge-tracker"
REPO_NAME="robeertm/ev-charge-tracker"

echo ""
echo "⚡ EV Charge Tracker — Projekt-Setup"
echo "───────────────────────────────────────"
echo ""

# ── 1. Projekt nach ~/Projects verschieben ───────────────
if [ -d "$TARGET" ]; then
    echo "⚠️  $TARGET existiert bereits."
    echo "   Überspringe Verschieben."
elif [ -d "$SOURCE" ]; then
    echo "📁 Verschiebe $SOURCE → $TARGET ..."
    mkdir -p "$TARGET_DIR"
    mv "$SOURCE" "$TARGET"
    echo "✅ Projekt verschoben nach ~/Projects/ev-charge-tracker"
else
    echo "ℹ️  Projekt liegt bereits unter $TARGET_DIR oder anderswo."
    echo "   Bitte Pfad prüfen."
fi

cd "$TARGET" 2>/dev/null || cd "$SOURCE" 2>/dev/null || { echo "❌ Projektordner nicht gefunden!"; exit 1; }
echo "📂 Arbeitsverzeichnis: $(pwd)"

# ── 2. Alten .git Ordner aufräumen ───────────────────────
if [ -d ".git" ]; then
    # Prüfe ob schon Commits vorhanden
    if git log --oneline -1 &>/dev/null 2>&1; then
        echo "✅ Git-Repository mit Commits vorhanden"
    else
        echo "🧹 Leeres .git Verzeichnis gefunden, lösche..."
        rm -rf .git
        echo "✅ Altes .git gelöscht"
    fi
fi

# ── 3. Alten venv aufräumen ─────────────────────────────
if [ -L "venv/bin/python3" ] || [ -d "venv" ]; then
    echo "🧹 Lösche alten venv..."
    rm -rf venv
    echo "✅ venv gelöscht (wird beim nächsten Start neu erstellt)"
fi

# ── 4. Git initialisieren ───────────────────────────────
if [ ! -d ".git" ]; then
    echo "📁 Initialisiere Git-Repository..."
    git init -b main
    echo "✅ Git initialisiert"
fi

# ── 5. Erster Commit ────────────────────────────────────
if [ -z "$(git log --oneline 2>/dev/null | head -1)" ]; then
    echo "📝 Erstelle ersten Commit..."
    git add .gitignore README.md app.py config.py import_gsheet.py updater.py \
            requirements.txt start.sh start.bat start.command \
            setup-github.command setup-project.command \
            models/ services/ templates/ static/
    git commit -m "Initial commit: EV Charge Tracker v1.0

Flask web app for tracking Kia Niro EV charging data.
Features: Dashboard, mobile input, ENTSO-E CO2 integration,
Google Sheet import, auto-updater via GitHub releases."
    echo "✅ Erster Commit erstellt"
else
    echo "✅ Commits bereits vorhanden"
fi

# ── 6. GitHub CLI prüfen ────────────────────────────────
if ! command -v gh &>/dev/null; then
    echo ""
    echo "❌ GitHub CLI (gh) ist nicht installiert."
    echo "   brew install gh && gh auth login"
    echo ""
    read -p "Drücke Enter zum Beenden..." dummy
    exit 1
fi

if ! gh auth status &>/dev/null 2>&1; then
    echo ""
    echo "❌ GitHub CLI nicht eingeloggt."
    echo "   gh auth login"
    echo ""
    read -p "Drücke Enter zum Beenden..." dummy
    exit 1
fi

echo "✅ GitHub CLI bereit"

# ── 7. GitHub Repo erstellen ─────────────────────────────
if gh repo view "$REPO_NAME" &>/dev/null 2>&1; then
    echo "✅ Repository $REPO_NAME existiert bereits"
    if ! git remote get-url origin &>/dev/null 2>&1; then
        git remote add origin "https://github.com/$REPO_NAME.git"
    fi
else
    echo "🌐 Erstelle öffentliches Repository: $REPO_NAME"
    gh repo create "$REPO_NAME" --public --source=. \
        --description "EV Charge Tracker — Local web app for tracking electric vehicle charging costs and CO₂ emissions"
    echo "✅ Repository erstellt"
fi

# ── 8. Push ──────────────────────────────────────────────
echo "🚀 Pushe zu GitHub..."
git push -u origin main
echo "✅ Code auf GitHub: https://github.com/$REPO_NAME"

# ── 9. VS Code öffnen ───────────────────────────────────
if command -v code &>/dev/null; then
    echo "💻 Öffne VS Code..."
    code .
    echo "✅ VS Code geöffnet"
else
    echo "ℹ️  VS Code CLI 'code' nicht gefunden."
    echo "   Öffne VS Code manuell und wähle: File → Open Folder → $(pwd)"
fi

echo ""
echo "───────────────────────────────────────"
echo "✅ Alles fertig!"
echo ""
echo "📂 Projekt:  $(pwd)"
echo "🌐 GitHub:   https://github.com/$REPO_NAME"
echo "🚀 Starten:  ./start.command"
echo "───────────────────────────────────────"
echo ""
read -p "Drücke Enter zum Beenden..." dummy
