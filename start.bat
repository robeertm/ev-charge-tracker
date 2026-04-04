@echo off
chcp 65001 >nul 2>&1
title EV Charge Tracker
:: ─────────────────────────────────────────────────────────
:: EV Charge Tracker — Start Script (Windows)
:: Creates a virtual environment, installs dependencies,
:: and launches the web application.
:: ─────────────────────────────────────────────────────────

cd /d "%~dp0"

set VENV_DIR=venv
set APP_PORT=7654

echo.
echo   EV Charge Tracker
echo   -------------------------------------------

:: ── Find Python ──────────────────────────────────────
set PYTHON=
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
    set PYTHON=python
    goto :found_python
)
where python3 >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "tokens=2 delims= " %%v in ('python3 --version 2^>^&1') do set PYVER=%%v
    set PYTHON=python3
    goto :found_python
)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "tokens=2 delims= " %%v in ('py -3 --version 2^>^&1') do set PYVER=%%v
    set PYTHON=py -3
    goto :found_python
)

echo   [ERROR] Python 3.8+ nicht gefunden!
echo   Bitte installiere Python: https://www.python.org/downloads/
echo   Wichtig: Bei der Installation "Add Python to PATH" aktivieren!
echo.
pause
exit /b 1

:found_python
echo   Python: %PYVER%

:: ── Create venv ──────────────────────────────────────
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo   Erstelle virtuelle Umgebung...
    %PYTHON% -m venv %VENV_DIR%
    if %ERRORLEVEL% neq 0 (
        echo   [ERROR] venv konnte nicht erstellt werden.
        pause
        exit /b 1
    )
    echo   [OK] venv erstellt
)

:: ── Activate venv ────────────────────────────────────
call %VENV_DIR%\Scripts\activate.bat
echo   [OK] venv aktiviert

:: ── Install dependencies ─────────────────────────────
if not exist "%VENV_DIR%\.deps_installed" (
    echo   Installiere Abhaengigkeiten...
    pip install --upgrade pip -q 2>nul
    pip install -r requirements.txt -q
    if %ERRORLEVEL% neq 0 (
        echo   [ERROR] Pakete konnten nicht installiert werden.
        pause
        exit /b 1
    )
    echo. > "%VENV_DIR%\.deps_installed"
    echo   [OK] Alle Pakete installiert
) else (
    echo   [OK] Abhaengigkeiten aktuell
)

:: ── Initialize database ──────────────────────────────
if not exist "data\ev_tracker.db" (
    echo   Initialisiere Datenbank...
    python -c "from app import create_app; app = create_app(); app.app_context().push()"
    echo   [OK] Datenbank erstellt
)

:: ── Get local IP ─────────────────────────────────────
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set LOCAL_IP=%%a
    goto :got_ip
)
:got_ip
set LOCAL_IP=%LOCAL_IP: =%

echo.
echo   Starte EV Charge Tracker...
echo   -------------------------------------------
echo   Browser:    http://localhost:%APP_PORT%
if defined LOCAL_IP (
    echo   Smartphone: http://%LOCAL_IP%:%APP_PORT%
)
echo   -------------------------------------------
echo   Druecke Ctrl+C zum Beenden
echo.

:: ── Open browser ─────────────────────────────────────
start "" "http://localhost:%APP_PORT%"

:: ── Launch Flask ─────────────────────────────────────
python app.py

pause
