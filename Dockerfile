# Unified Dockerfile for Crimsonej (Python AI Engine + Node.js WhatsApp Bridge)
FROM python:3.12-slim

# Install Node.js 20, FFmpeg, fonts, OCR, PDF tools, LibreOffice, ImageMagick, WebP, Chromium, media tools, and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        ffmpeg \
        atomicparsley \
        aria2 \
        ca-certificates \
        git \
        procps \
        build-essential \
        libffi-dev \
        libssl-dev \
        fonts-liberation \
        fontconfig \
        fonts-dejavu-core \
        fonts-freefont-ttf \
        fonts-noto-color-emoji \
        fonts-roboto \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-spa \
        tesseract-ocr-deu \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
        tesseract-ocr-ara \
        tesseract-ocr-rus \
        tesseract-ocr-por \
        tesseract-ocr-ita \
        tesseract-ocr-hin \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-java-common \
        pandoc \
        graphviz \
        sqlite3 \
        zip \
        unzip \
        tar \
        gzip \
        bzip2 \
        xz-utils \
        imagemagick \
        graphicsmagick \
        webp \
        libwebp-dev \
        ghostscript \
        sox \
        libsox-fmt-all \
        flac \
        lame \
        vorbis-tools \
        opus-tools \
        libimage-exiftool-perl \
        mediainfo \
        chromium \
        chromium-driver \
        xvfb \
        jq \
        net-tools \
        iputils-ping \
        bind9-dnsutils \
        nmap \
        whois \
        redis-server \
        htop \
        tree \
        psmisc \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && (sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml || true) \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/amass.zip https://github.com/owasp-amass/amass/releases/download/v4.1.0/amass_Linux_amd64.zip \
    && echo "a5cbbccf1b4a7493f36eb7b51beb19c77a8ac044a4edfb2a5f13d6a00601eb29  /tmp/amass.zip" | sha256sum -c - \
    && unzip -p /tmp/amass.zip amass_Linux_amd64/amass > /usr/local/bin/amass \
    && chmod 0755 /usr/local/bin/amass \
    && rm /tmp/amass.zip

WORKDIR /app

# Copy requirement files first for optimal layer caching
COPY crimson-bot/requirements.txt /app/crimson-bot/requirements.txt
COPY whatsapp-bridge/package.json /app/whatsapp-bridge/package.json
COPY whatsapp-bridge/package-lock.json /app/whatsapp-bridge/package-lock.json

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/crimson-bot/requirements.txt && \
    pip install --no-cache-dir -U yt-dlp

# Install Node.js dependencies
WORKDIR /app/whatsapp-bridge
RUN npm ci --omit=dev --no-audit --no-fund || npm install --omit=dev --no-audit --no-fund

# Pinned client-side React/Vite toolchain for temporary web-app previews.
WORKDIR /app/preview-runtime
COPY preview-runtime/package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund

# Copy entire application codebase
WORKDIR /app
COPY . /app/

# Environment defaults
ENV PORT=7860 \
    BOT_PORT=5000 \
    DATA_DIR=/data \
    AUTH_DIR=/data/auth_info_baileys \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROME_BIN=/usr/bin/chromium \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

# Ensure permissions for executable scripts
RUN chmod +x /app/crimsonej /app/orchestrator.py

# Railway injects $PORT for public routing.
# Run orchestrator in foreground mode to keep both Python AI engine & Node.js bridge alive.
CMD ["python3", "orchestrator.py", "start", "--foreground"]
