# Dockerfile for the Crimsonej WhatsApp Bridge (Node.js + Baileys)
# Renders the bridge service. No bridge source code is modified.
#
# Persistent Disk strategy:
#   Render mounts the Persistent Disk at /data.
#   bridge.js auto-detects /data and stores Baileys auth state in
#   /data/auth_info_baileys when present:
#       const AUTH_DIR = process.env.AUTH_DIR
#                  || (fs.existsSync('/data') ? '/data/auth_info_baileys'
#                                              : 'auth_info_baileys');
#   So we just need to ensure /data exists at runtime — no source edit.
#
# Puppeteer note:
#   whatsapp-web.js / venom-bot pull in Puppeteer. We install Chromium
#   deps and point Puppeteer at the bundled Chromium. This image is
#   large (~1 GB) — Render free tier has a 2 GB image cap, so we may
#   hit that. See runbook.

FROM node:20-bookworm-slim

# Chromium runtime deps for Puppeteer + general build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xvfb \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Persistent Disk mount point
ENV DATA_DIR=/data
RUN mkdir -p /data

# Install deps with caching
WORKDIR /opt/build
COPY whatsapp-bridge/package.json     /opt/build/package.json
COPY whatsapp-bridge/package-lock.json /opt/build/package-lock.json
RUN npm ci --omit=dev --no-audit --no-fund \
    || npm install --omit=dev --no-audit --no-fund

# Stage bridge source onto /data alongside the persistent auth dir.
# The repo contains a committed node_modules/ which is stale and broken
# (missing ESM dist files, e.g. lru-cache). Copy source files only, then
# install a fresh node_modules from the build stage.
WORKDIR /opt/bridge-src
COPY whatsapp-bridge/*.js whatsapp-bridge/*.json whatsapp-bridge/*.py whatsapp-bridge/*.md whatsapp-bridge/.env.example /opt/bridge-src/
RUN cp -a /opt/bridge-src/. /data/ && rm -rf /opt/bridge-src \
    && cp -a /opt/build/node_modules /data/node_modules

WORKDIR /data

ENV NODE_ENV=production \
    PORT=7860 \
    AUTH_DIR=/data/auth_info_baileys \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=false

EXPOSE 7860

# Health check on the simple HTTP / route the bridge already serves.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:7860/ || exit 1

CMD ["node", "bridge.js"]
