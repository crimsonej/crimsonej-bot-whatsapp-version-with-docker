# Unified Dockerfile for Crimsonej (Python AI Engine + Node.js WhatsApp Bridge)
FROM python:3.11-slim

# Install Node.js 20, FFmpeg, and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        ca-certificates \
        git \
        procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement files first for optimal layer caching
COPY crimson-bot/requirements.txt /app/crimson-bot/requirements.txt
COPY whatsapp-bridge/package.json /app/whatsapp-bridge/package.json
COPY whatsapp-bridge/package-lock.json /app/whatsapp-bridge/package-lock.json

# Install Python dependencies
RUN pip install --no-cache-dir -r /app/crimson-bot/requirements.txt && pip install --no-cache-dir -U yt-dlp

# Install Node.js dependencies
WORKDIR /app/whatsapp-bridge
RUN npm ci --omit=dev --no-audit --no-fund || npm install --omit=dev --no-audit --no-fund

# Copy entire application codebase
WORKDIR /app
COPY . /app/

# Environment defaults
ENV PORT=7860 \
    BOT_PORT=5000 \
    DATA_DIR=/data \
    AUTH_DIR=/data/auth_info_baileys \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production

# Ensure permissions for executable scripts
RUN chmod +x /app/crimsonej /app/orchestrator.py

# Railway injects $PORT for public routing.
# Run orchestrator in foreground mode to keep both Python AI engine & Node.js bridge alive.
CMD ["python3", "orchestrator.py", "start", "--foreground"]
