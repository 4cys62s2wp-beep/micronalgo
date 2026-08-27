# micronalgo als Dauerbetrieb-Container.
#
# Laeuft auf jedem Docker-Host (Fly.io, Railway, Render, eigener Server) und
# braucht keinen Mac. Der Bot laeuft durchgehend und schlaeft selbst bis zum
# naechsten Entscheidungszeitpunkt -- deshalb ist ein Container die richtige
# Form und ein Cron-Job die schlechtere (siehe .github/workflows/).

FROM python:3.12-slim

# tzdata ist NICHT optional: der gesamte Zeitplan rechnet in
# America/New_York, und ohne Zoneninfo wirft zoneinfo beim ersten Kalender-
# Zugriff. Ein slim-Image hat sie nicht von Haus aus.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[all]"

# Zustand, Logs und Cache liegen auf einem Volume, damit ein Neustart des
# Containers keine offene Position vergisst. Ohne Volume rekonstruiert die
# Reconciliation zwar aus dem Broker, haelt aber im Zweifel an -- richtig, aber
# unnoetig unbequem.
ENV MICRONALGO_STATE_DIR=/data/state \
    MICRONALGO_LOG_DIR=/data/logs \
    MICRONALGO_CACHE_DIR=/data/cache \
    MICRONALGO_REPORTS_DIR=/data/reports \
    MICRONALGO_KILL_SWITCH_FILE=/data/state/KILL \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

# Standard ist der Trockenlauf. Echte (Paper-)Orders erst durch bewusstes
# Ueberschreiben des Commands mit --live.
CMD ["micronalgo", "paper"]
