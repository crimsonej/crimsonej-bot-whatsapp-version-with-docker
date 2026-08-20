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
ENV DATA_DIR=/data
RUN mkdir -p /data

# Copy bot source onto /data so BASE_DIR resolves to the Persistent Disk.
# We use a tmp staging dir to avoid clobbering existing disk contents.
COPY crimson-bot/ /opt/bot-src/
RUN cp -a /opt/bot-src/. /data/ && rm -rf /opt/bot-src

WORKDIR /data

# Render sets $PORT. We honor it via BOT_PORT, which config.py reads.
ENV BOT_PORT=5000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5000

# Health check uses the Flask /health route already defined in bot.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5000/health || exit 1

# Two-process note:
#   bot.py's __main__ boots several background threads (dispatcher,
#   reporter, scheduler, sweeper, heartbeat) and then runs Flask's
#   dev server. For Render, we run it directly with python so the
#   background services start. gunicorn would also work but is not
#   strictly required for a single-process bot.
# Render/Railway inject $PORT. BOT_PORT (if set) wins, then $PORT, else 5000.
CMD ["sh", "-c", "BOT_PORT=${BOT_PORT:-${PORT:-5000}} python bot.py server 2>&1"]
