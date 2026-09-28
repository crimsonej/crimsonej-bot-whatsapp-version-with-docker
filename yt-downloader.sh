#!/usr/bin/env bash

# yt-downloader.sh
# Author: CrimsonEj (https://github.com/crimsonej)
# Usage: ./yt-downloader.sh <playlist_url> [video|audio] [folder_name]
# Example: ./yt-downloader.sh "https://music.youtube.com/playlist?list=PLzb_jAVcgU7Ny_9uNxn5NLsTda47l6FZW" video "My Likes"
# Default: video mode, uses playlist title or "Downloads" folder

set -euo pipefail

# ================= CONFIG =================
MAX_RES="1080"                  # Max video height (change to 720 for smaller files)
ARCHIVE_FILE="yt-archive.txt"   # Tracks downloaded IDs (in current dir or per-folder)
SLEEP_INTERVAL=3                # Sleep between downloads to avoid throttling
RETRIES=10                      # Retry count for network issues

# Preferred format: MP4 video + AAC audio, H.264 priority
FORMAT_VIDEO="bestvideo[ext=mp4][height<=?${MAX_RES}][vcodec^=avc1]/bestvideo[height<=?${MAX_RES}]+bestaudio[ext=m4a]/best[ext=mp4]/best"
FORMAT_AUDIO="bestaudio[ext=m4a]/bestaudio/best"

# Default mode: video
MODE="${2:-video}"
CUSTOM_FOLDER="${3:-}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ================= FUNCTIONS =================
check_deps() {
    command -v yt-dlp >/dev/null || { echo -e "${RED}yt-dlp not found. Install it first.${NC}"; exit 1; }
    command -v ffmpeg >/dev/null || { echo -e "${RED}ffmpeg not found (needed for merging).${NC}"; exit 1; }
}

get_playlist_title() {
    yt-dlp --get-filename -o "%(playlist_title)s" --flat-playlist --playlist-end 1 "$1" 2>/dev/null | head -n1 || echo "Downloads"
}

# ================= MAIN =================
check_deps

URL="$1"
if [[ -z "$URL" ]]; then
    echo -e "${RED}Error: Provide a playlist or video URL as first argument.${NC}"
    echo "Usage: $0 <url> [video|audio] [custom_folder]"
    exit 1
fi

# Get playlist title for folder (fallback if single video)
PLAYLIST_TITLE=$(get_playlist_title "$URL")
SAFE_TITLE=$(echo "$PLAYLIST_TITLE" | tr -s '[:space:]' '_' | tr -cd '[:alnum:]_-')

FOLDER="${CUSTOM_FOLDER:-$SAFE_TITLE}"
if [[ -z "$FOLDER" || "$FOLDER" == "NA" ]]; then FOLDER="Downloads"; fi

mkdir -p "$FOLDER"
cd "$FOLDER" || exit 1

echo -e "${GREEN}Downloading to folder: $(pwd)${NC}"
echo -e "Mode: ${YELLOW}${MODE}${NC} | Playlist: ${YELLOW}${PLAYLIST_TITLE}${NC}"

# Common yt-dlp options
COMMON_OPTS=(
    --ignore-errors
    --no-overwrites
    --continue
    --retries "$RETRIES"
    --sleep-interval "$SLEEP_INTERVAL"
    --max-sleep-interval 10
    --download-archive "$ARCHIVE_FILE"
    --extractor-args "youtube:player_client=android"
    --progress
)

if [[ "$MODE" == "audio" ]]; then
    # Audio-only MP3
    yt-dlp "${COMMON_OPTS[@]}" \
        -x --audio-format mp3 --audio-quality 0 \
        -o "%(title)s.%(ext)s" \
        "$URL"
else
    # Video MP4 preferred
    yt-dlp "${COMMON_OPTS[@]}" \
        -f "$FORMAT_VIDEO" \
        --merge-output-format mp4 \
        --remux-video mp4 \
        -S "vcodec:h264,ext:mp4,res,acodec:aac" \
        -o "%(title)s.%(ext)s" \
        "$URL"
fi

echo -e "${GREEN}Done! Files are in: $(pwd)${NC}"
echo "Archive file: $ARCHIVE_FILE (prevents re-downloads next time)"
