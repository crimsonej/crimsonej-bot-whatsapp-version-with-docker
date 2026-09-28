# Unified Dockerfile for Crimsonej (Python AI Engine + Node.js WhatsApp Bridge)
FROM python:3.12-slim

# Install base system dependencies (lightweight)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        ffmpeg \
        atomicparsley \
        aria2 \
        ca-certificates \
        git \
        procps \
        libffi-dev \
        libssl-dev \
        fontconfig \
        poppler-utils \
        sqlite3 \
        zip \
        unzip \
        tar \
        gzip \
        bzip2 \
        xz-utils \
        jq \
        net-tools \
        iputils-ping \
        bind9-dnsutils \
        htop \
        tree \
        psmisc \
    && rm -rf /var/lib/apt/lists/*

# Install fonts (needed for PDF/OCR rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-freefont-ttf \
        fonts-noto-color-emoji \
        fonts-roboto \
    && rm -rf /var/lib/apt/lists/*

# Install OCR (Tesseract) - core + essential languages only
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    && rm -rf /var/lib/apt/lists/*

# Install document processing (LibreOffice headless - heavy, split separately)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-java-common \
        pandoc \
        graphviz \
    && rm -rf /var/lib/apt/lists/*

# Install image/media processing
RUN apt-get update && apt-get install -y --no-install-recommends \
        imagemagick \
        graphicsmagick \
        webp \
        libwebp-dev \
        ghostscript \
        libimage-exiftool-perl \
        mediainfo \
    && rm -rf /var/lib/apt/lists/*

# Install audio processing (optional - can remove if not needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        sox \
        libsox-fmt-all \
        flac \
        lame \
        vorbis-tools \
        opus-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Chromium for Puppeteer (heavy - separate layer)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick PDF policy
RUN (sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml 2>/dev/null || \
    sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml 2>/dev/null || true)

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
