# Dockerfile for the Crimsonej AI Engine (Python Flask)
FROM python:3.11-slim

# System deps for yt-dlp, Pillow, pdf2docx, OCR, PDF conversion, LibreOffice, Chromium, ReportLab fonts, build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        atomicparsley \
        aria2 \
        ca-certificates \
        curl \
        wget \
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
        exiftool \
        mediainfo \
        chromium \
        chromium-driver \
        xvfb \
        jq \
        net-tools \
        iputils-ping \
        dnsutils \
        htop \
        tree \
        psmisc \
    && sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml || true \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for layer caching
WORKDIR /opt/build
COPY crimson-bot/requirements.txt /opt/build/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /opt/build/requirements.txt \
    && pip install --no-cache-dir -U yt-dlp gunicorn

# Where persistent disk is mounted
ENV DATA_DIR=/data \
    MEDIA_ROOT=/media \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROME_BIN=/usr/bin/chromium

RUN mkdir -p /data
RUN mkdir -p /media

# Copy bot source onto /data
COPY crimson-bot/ /opt/bot-src/
RUN cp -a /opt/bot-src/. /data/ && rm -rf /opt/bot-src

WORKDIR /data

CMD ["sh", "-c", "exec gunicorn --config gunicorn.conf.py bot:app 2>&1"]
