# Dockerfile for the Crimsonej WhatsApp Bridge (Node.js + Baileys)
FROM node:20-bookworm-slim

# Chromium runtime deps for Puppeteer + media & build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        fonts-noto-color-emoji \
        ffmpeg \
        webp \
        libwebp-dev \
        imagemagick \
        procps \
        git \
        wget \
        curl \
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
WORKDIR /opt/bridge-src
COPY whatsapp-bridge/*.js whatsapp-bridge/*.json whatsapp-bridge/*.py whatsapp-bridge/*.md whatsapp-bridge/.env.example /opt/bridge-src/
RUN cp -a /opt/bridge-src/. /data/ && rm -rf /opt/bridge-src \
    && cp -a /opt/build/node_modules /data/node_modules

WORKDIR /data

ENV NODE_ENV=production \
    PORT=7860 \
    AUTH_DIR=/data/auth_info_baileys \
    MEDIA_ROOT=/media \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=false

EXPOSE 7860

CMD ["node", "bridge.js"]
