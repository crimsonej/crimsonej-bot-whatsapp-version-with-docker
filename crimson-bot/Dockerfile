# Dockerfile for the Crimsonej AI Engine (Python Flask)
# Renders the bot service. No bot source code is modified.
#
# Persistent Disk strategy:
#   Render mounts the Persistent Disk at /data BEFORE this image runs.
#   We WORKDIR into /data so BASE_DIR = /data inside the container.
#   core/config.py anchors all state files (vectors.json, sessions.json,
#   cache.json, vaults/, docs/, user_profiles.json, etc.) to BASE_DIR,
#   so they automatically land on the Persistent Disk without any
#   source-code change.

FROM python:3.11-slim

# System deps for yt-dlp (ffmpeg for audio extraction), Pillow, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for layer caching
WORKDIR /opt/build
COPY crimson-bot/requirements.txt /opt/build/requirements.txt
RUN pip install --no-cache-dir -r /opt/build/requirements.txt \
    && pip install --no-cache-dir gunicorn

# Where Render mounts the Persistent Disk. State lives here.
ENV DATA_DIR=/data \
    MEDIA_ROOT=/media
RUN mkdir -p /data
RUN mkdir -p /media

# Copy bot source onto /data so BASE_DIR resolves to the Persistent Disk.
# We use a tmp staging dir to avoid clobbering existing disk contents.
COPY crimson-bot/ /opt/bot-src/
RUN cp -a /opt/bot-src/. /data/ && rm -rf /opt/bot-src

WORKDIR /data

# Railway injects $PORT (e.g. 8080) and routes traffic to it.
# BOT_PORT (if explicitly set in dashboard) wins, then $PORT, else 5000.
# NOTE: no ENV BOT_PORT here — it would shadow the $PORT fallback.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NOTE: no EXPOSE here. Railway routes to the injected $PORT; a static
# EXPOSE (e.g. 5000) makes Railway proxy to that port while the app
# listens on $PORT, breaking health checks.

# Two-process note:
#   bot.py's __main__ boots several background threads (dispatcher,
#   reporter, scheduler, sweeper, heartbeat) and then runs Flask's
#   dev server. For Render, we run it directly with python so the
#   background services start. gunicorn would also work but is not
#   strictly required for a single-process bot.
# Railway/Render inject $PORT and route traffic to it — it MUST win.
# Local dev: PORT unset, BOT_PORT or 5000 used as fallback.
CMD ["sh", "-c", "exec gunicorn --config gunicorn.conf.py bot:app 2>&1"]
