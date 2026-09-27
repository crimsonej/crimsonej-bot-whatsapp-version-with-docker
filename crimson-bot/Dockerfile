# Dockerfile for the Crimsonej AI Engine (Python Flask)
FROM python:3.11-slim

# System deps for yt-dlp (ffmpeg for audio extraction), Pillow, pdf2docx, ReportLab fonts, build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        build-essential \
        libffi-dev \
        libssl-dev \
        fonts-liberation \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for layer caching
WORKDIR /opt/build
COPY crimson-bot/requirements.txt /opt/build/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /opt/build/requirements.txt \
    && pip install --no-cache-dir gunicorn

# Where persistent disk is mounted
ENV DATA_DIR=/data \
    MEDIA_ROOT=/media
RUN mkdir -p /data
RUN mkdir -p /media

# Copy bot source onto /data
COPY crimson-bot/ /opt/bot-src/
RUN cp -a /opt/bot-src/. /data/ && rm -rf /opt/bot-src

WORKDIR /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["sh", "-c", "exec gunicorn --config gunicorn.conf.py bot:app 2>&1"]
