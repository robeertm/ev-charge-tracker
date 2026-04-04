#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# EV Charge Tracker — macOS Launcher (.command)
# Double-click this file in Finder to start the app.
# ─────────────────────────────────────────────────────────

# cd to the directory where this script lives
cd "$(dirname "$0")"

# Run the main start script
exec bash ./start.sh
