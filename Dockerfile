# EV Charge Tracker — container image.
#
# The app is a self-hosted Flask application that serves plain HTTP on
# port 7654 by default (its canonical port across start.sh, the systemd
# unit and config.py). TLS is handled by the deployment's reverse proxy,
# so we run it in HTTP mode here. app.py's __main__ calls create_app()
# and app.run(host=APP_HOST, port=APP_PORT) — both overridable via env.

FROM python:3.11-slim

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
