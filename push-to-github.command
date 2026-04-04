#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
REPO_NAME="robeertm/ev-charge-tracker"

echo ""
echo "⚡ EV Charge Tracker — GitHub Push"
echo "───────────────────────────────────────"

# Commit der Username-Änderungen
git add -A
git commit -m "Fix GitHub username to robeertm" 2>/dev/null || echo "✅ Keine neuen Änderungen"

# GitHub Repo erstellen (falls es noch nicht existiert)
if gh repo view "$REPO_NAME" &>/dev/null 2>&1; then
    echo "✅ Repository $REPO_NAME existiert bereits"
else
    echo "🌐 Erstelle öffentliches Repository..."
    gh repo create "$REPO_NAME" --public --source=. \
        --description "EV Charge Tracker — Local web app for tracking electric vehicle charging costs and CO₂ emissions"
    echo "✅ Repository erstellt"
fi

# Remote setzen
if ! git remote get-url origin &>/dev/null 2>&1; then
    git remote add origin "https://github.com/$REPO_NAME.git"
elif [ "$(git remote get-url origin)" != "https://github.com/$REPO_NAME.git" ]; then
    git remote set-url origin "https://github.com/$REPO_NAME.git"
fi

# Push
echo "🚀 Pushe zu GitHub..."
git push -u origin main
echo ""
echo "✅ Fertig! https://github.com/$REPO_NAME"

# VS Code öffnen
if command -v code &>/dev/null; then
    echo "💻 Öffne VS Code..."
    code .
fi

# Aufräumen: dieses Skript kann danach gelöscht werden
echo ""
echo "ℹ️  Du kannst push-to-github.command jetzt löschen."
read -p "Drücke Enter zum Beenden..." dummy
