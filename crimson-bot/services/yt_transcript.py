"""
services/yt_transcript.py
=========================
YouTube Video Transcript Extractor.
Extracts captions/transcripts from YouTube URLs or IDs without downloading video.
"""

import logging
import re
from typing import Dict, Any, Optional

log = logging.getLogger("crimson")

def extract_youtube_id(url_or_id: str) -> Optional[str]:
    """Extract 11-character YouTube video ID from various URL formats or raw ID."""
    if not url_or_id:
        return None
    url_or_id = url_or_id.strip()
    if len(url_or_id) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", url_or_id):
        return url_or_id
    
    patterns = [
        r"(?:v=|\/vi\/|v\/|vi\/|youtu\.be\/|\/embed\/|\/shorts\/|\/e\/|watch\?.*v=)([^#\&\?]*)"
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            candidate = match.group(1)
            if len(candidate) == 11:
                return candidate
    return None

def get_youtube_transcript(url_or_id: str, max_chars: int = 15000) -> Dict[str, Any]:
    """
    Fetches transcript/captions for a YouTube video.
    
    Returns:
        Dict with keys: ok (bool), video_id (str), transcript (str), duration_mins (float), error (str/None)
    """
    video_id = extract_youtube_id(url_or_id)
    if not video_id:
        return {"ok": False, "video_id": "", "transcript": "", "error": "Invalid YouTube URL or Video ID"}

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        
        # Try fetching transcript (prefer english, fallback to available languages)
        api = YouTubeTranscriptApi()
        try:
            transcript_list = api.fetch(video_id=video_id, languages=['en', 'en-US', 'en-GB'])
        except Exception:
            # Fallback to list available transcripts and pick first available
            list_transcripts = api.list(video_id=video_id)
            first_t = list_transcripts.find_transcript(['en', 'es', 'fr', 'de', 'pt', 'ru', 'hi', 'ja', 'zh'])
            transcript_list = first_t.fetch()

        formatted_lines = []
        total_duration = 0.0
        
        for item in transcript_list:
            if hasattr(item, "text"):
                text = str(getattr(item, "text", "")).strip()
                start = float(getattr(item, "start", 0.0))
                duration = float(getattr(item, "duration", 0.0))
            elif isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                start = float(item.get("start", 0.0))
                duration = float(item.get("duration", 0.0))
            else:
                continue

            total_duration = max(total_duration, start + duration)
            
            # Format timestamp [MM:SS]
            mins = int(start // 60)
            secs = int(start % 60)
            formatted_lines.append(f"[{mins:02d}:{secs:02d}] {text}")

        full_transcript = "\n".join(formatted_lines)[:max_chars]
        duration_mins = round(total_duration / 60.0, 1)

        log.info(f"[YT Transcript] Extracted {len(full_transcript)} chars ({duration_mins} mins) for {video_id}")
        return {
            "ok": True,
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "transcript": full_transcript,
            "duration_mins": duration_mins,
            "error": None
        }
    except Exception as e:
        log.warning(f"[YT Transcript] Could not fetch transcript for {video_id}: {e}")
        return {
            "ok": False,
            "video_id": video_id,
            "transcript": "",
            "error": f"Transcripts unavailable or disabled for video {video_id}"
        }
