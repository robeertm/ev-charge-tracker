# EV Charge Tracker — container image.
#
# The app is a self-hosted Flask application that serves plain HTTP on
# port 7654 by default (its canonical port across start.sh, the systemd
# unit and config.py). TLS is handled by the deployment's reverse proxy,
# so we run it in HTTP mode here. app.py's __main__ calls create_app()
# and app.run(host=APP_HOST, port=APP_PORT) — both overridable via env.

# Python 3.12, NOT 3.11 — deliberately. The optional Kia/Hyundai connector
# (hyundai-kia-connect-api) needs >=4.26.3 for the headless CCI password
# sign-in, and every SDK release from 4.23.1 onward declares
# Requires-Python >=3.12. Raspberry Pi OS bookworm ships Python 3.11, so a
# NATIVE install there can never install the password-login SDK — the app's
# runtime guard (_python_supports_cci in connector_hyundai_kia.py, driven by
# sys.version_info) then routes Kia/Hyundai users to the manual browser token
# flow. Running the app in THIS container instead gives those users a 3.12
# interpreter, so the guard lifts automatically and the direct password
# sign-in becomes installable from the Fahrzeug-API page. Keep this at >=3.12.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=7654

WORKDIR /app

# Install dependencies first for better layer caching. Only the core
# runtime deps are installed; optional vehicle-API connectors stay
# commented out in requirements.txt and are added from the app UI.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY . .

# SQLite DB and caches live under data/ (see config.py). Declare it as a
# volume so charge history survives container replacement.
RUN mkdir -p data
VOLUME ["/app/data"]

EXPOSE 7654

CMD ["python", "app.py"]
