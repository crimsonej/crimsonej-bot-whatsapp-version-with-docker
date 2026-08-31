"""
services/media.py
=================
Media downloading, format conversion, and public host uploader.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import requests

from core.config import log

LAST_DL_ERROR = "No downloads attempted yet"
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")

def update_ytdlp_async() -> None:
    """Run yt-dlp update in a background thread on startup to ensure YouTube format compatibility."""
    def _update():
        try:
            log.info("[Media] Checking yt-dlp updates...")
            res = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], capture_output=True, text=True, timeout=90)
            if res.returncode == 0:
                log.info("[Media] yt-dlp update check completed successfully.")
            else:
                log.debug("[Media] yt-dlp update check result: %s", res.stderr[:150])
        except Exception as e:
            log.debug("[Media] yt-dlp update check exception: %s", e)

    t = threading.Thread(target=_update, name="YtDlpUpdater", daemon=True)
    t.start()

INVIDIOUS_INSTANCES = [
    "https://yewtu.eu",
    "https://yewtu.cafe",
    "https://yewtu.snopyta.org",
    "https://yewtu.workers.dev",
    "https://yewtu.kavin.rocks",
]

PIPED_INSTANCES = [
    "https://piped.video",
    "https://piped.kavin.rocks",
    "https://piped.snopyta.org",
]

MAX_RES = 1080
# Prefer pre-merged MP4 with H.264 video + AAC audio (WhatsApp-compatible).
# Only fall back to video-only if no combined stream is available.
FORMAT_VIDEO = (
    f"best[ext=mp4][height<=?{MAX_RES}][vcodec^=avc1][acodec^=mp4a]/"
    f"best[ext=mp4][height<=?{MAX_RES}]/"
    f"bestvideo[ext=mp4][height<=?{MAX_RES}][vcodec^=avc1]+bestaudio[ext=m4a]/"
    f"bestvideo[height<=?{MAX_RES}]+bestaudio/"
    f"best"
)
FORMAT_AUDIO = "bestaudio[ext=m4a]/bestaudio/best"

def format_duration(seconds) -> str:
    if not seconds or not isinstance(seconds, (int, float)):
        return "?:??"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


_VERSION_KEYWORDS = (
    "show me", "show me the", "list", "options", "which", "which versions",
    "what versions", "available", "what's available", "whats available",
    "choose", "pick", "let me pick", "let me choose",
    "see the", "see options", "see versions",
    "not sure", "i'm not sure", "im not sure", "i dont know",
)

# Words that strongly hint at the song's owner / context. When the query
# includes one of these we trust the bot to auto-download.
_ARTIST_HINTS = (
    "by ", " ft.", " feat.", "featuring ", " x ", " & ",
    " vevo", " - ", " – ",
    "official", "official video", "official audio", "official music video",
    "topic", "lyrics", " lyric video",
    # Common artist-name suffixes that show up in queries
    "drake", "rihanna", "adele", "taylor", "beyonce", "beyoncé",
    "eminem", "kanye", "weeknd", "the weeknd", "billie", "olivia",
    "bad bunny", "arctic monkeys", "tame impala", "aurora", "clairo",
    "maroon 5", "maroon5", "ice spice", "cardi b", "doja cat",
)


def _is_ambiguous_query(query: str) -> bool:
    """True when the query is too generic to safely auto-pick a single result.

    A query is considered ambiguous when:
    - it has no artist hint (no 'by', 'ft.', no known artist name, etc.)
    - it's short (≤4 meaningful words) — i.e. the user didn't add context
    - it doesn't look like a URL fragment
    """
    if not query:
        return True
    q = query.lower().strip()
    # Strip the trailing "video"/"audio"/"song"/"track" tokens that the LLM
    # sometimes appends — they don't add disambiguation.
    q = re.sub(r"\b(video|audio|song|track|official|full|song video|music video)\b", "", q).strip()
    if not q:
        return True
    if any(h in q for h in _ARTIST_HINTS):
        return False
    # Count meaningful words (≥3 chars each)
    words = [w for w in re.split(r"[\s,]+", q) if len(w) >= 3]
    return len(words) <= 4


def user_wants_versions(query: str) -> bool:
    """True when the user either asked for options first OR the query is
    too ambiguous for the bot to confidently pick a single result."""
    if not query:
        return False
    q = query.lower()
    if any(kw in q for kw in _VERSION_KEYWORDS):
        return True
    return _is_ambiguous_query(query)


def format_version_list(results: list[dict], media_type: str) -> str:
    """Render a numbered menu the user can reply to with a single digit.
    Written in Crimsonej's voice — casual, brief, slangy."""
    if not results:
        return "couldn't find anything for that one 😭 try a different name?"
    lines = [f"{i+1}. {r['title']} ({format_duration(r.get('duration'))}) — {r.get('channel', 'Unknown')}"
             for i, r in enumerate(results)]
    label = "vid" if media_type == "video" else "audio"
    opener = random.choice([
        "yo found a few of these floating around, which one you want 👇",
        "aight there's a couple of versions, lemme know which one 👇",
        "i see a few options for this one, pick your fighter 👇",
        "found a couple of these, which one hits right 👇",
    ])
    closer = random.choice([
        f"reply with the number (1-{len(results)}) and i'll send it. 0 to bail.",
        f"send the number (1-{len(results)}) and it's yours. 0 to skip.",
        f"pick a number 1-{len(results)} and i'll grab it. 0 to dip.",
    ])
    return f"{opener}\n" + "\n".join(lines) + f"\n\n{closer}"


def format_download_confirmation(filename: str, media_type: str) -> str:
    """Quick in-character line confirming what was sent."""
    clean = filename.rsplit(".", 1)[0] if "." in filename else filename
    opener = random.choice([
        "say less, on the way 🫡",
        "bet, sending it now �",
        "aight, here you go 🎯",
        "gotchu, sending it 📦",
        "locked in, enjoy ✨",
    ])
    if media_type == "audio":
        tail = random.choice([
            f" — *{clean}*",
            f" — {clean}",
            "",
            f" 🎧 *{clean}*",
        ])
    else:
        tail = random.choice([
            f" — *{clean}*",
            f" — {clean}",
            "",
            f" 🎬 *{clean}*",
        ])
    return opener + tail

def convert_video_for_whatsapp(input_path: str) -> str | None:
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_whatsapp.mp4"
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', r'scale=min(720\,iw):-2',
        '-c:v', 'libx264', '-profile:v', 'main', '-level', '3.1',
        '-preset', 'fast', '-crf', '26',
        '-c:a', 'aac', '-b:a', '128k',
        '-fs', '100M',
        '-movflags', '+faststart',
        '-threads', '0', '-y', output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        return None
    except Exception as e:
        log.error("[Video Convert] ffmpeg error: %s", e)
        return None

def convert_audio_for_whatsapp(input_path: str) -> str | None:
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_whatsapp.ogg"
    cmd = [
        'ffmpeg', '-i', input_path, '-vn',
        '-c:a', 'libopus', '-b:a', '128k', '-y', output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return output_path
    except Exception as e:
        log.error("[Audio Convert] ffmpeg error: %s", e)
        return None


def _file_has_audio(path: str) -> bool:
    """Return True if the file contains an audio stream (uses ffprobe).
    If ffprobe is not available, fall back to simple extension/size heuristic.
    """
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return '"index"' in res.stdout
    except Exception:
        pass

    # Fallback: check extension and file size
    if os.path.getsize(path) < 5000:
        return False
    lower = path.lower()
    if lower.endswith(('.mp3', '.m4a', '.aac', '.ogg', '.opus')):
        return True
    # MP4/WEBM may or may not have audio; assume likely if >1MB
    return os.path.getsize(path) > (1 * 1024 * 1024)


def _probe_audio_codec(path: str) -> str | None:
    """Return the audio codec name (e.g. 'aac', 'mp4a', 'opus') of `path`,
    or None if there is no audio stream or ffprobe fails."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_name",
            "-of", "default=nw=1:nk=1",
            path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip() or None
    except Exception:
        pass
    return None

def upload_file_public(file_path: str) -> str | None:
    if not os.path.exists(file_path):
        return None
    filename = os.path.basename(file_path)
    headers = {"User-Agent": "crimsonej-uploader/1.0"}
    try:
        with open(file_path, "rb") as fh:
            r = requests.post("https://0x0.st", files={"file": (filename, fh)}, headers=headers, timeout=30)
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception as e:
        log.debug("[Upload] 0x0.st failed: %s", e)
    try:
        with open(file_path, "rb") as fh:
            r = requests.put(f"https://transfer.sh/{filename}", data=fh, headers=headers, timeout=60)
        if r.status_code in (200, 201) and r.text.strip():
            return r.text.strip()
    except Exception as e:
        log.debug("[Upload] transfer.sh failed: %s", e)
    return None

def download_youtube(url: str, media_type: str = "audio", retries: int = 2) -> tuple[str, str] | None:
    download_youtube_sync(url, media_type, retries=retries)


# ── Self-awareness wrapper ────────────────────────────────────────────────────
def download_youtube_task(*, url: str, media_type: str, owner_jid: str = "",
                          owner_user_id: str = "", task_id: str | None = None,
                          progress=None) -> dict:
    """Action fn for the dispatcher.

    The `progress` argument is a `services.progress.ProgressSession` injected
    by the dispatcher. It is forwarded to `download_youtube_sync` so the
    per-percent updates stream into the WhatsApp progress message.
    """
    from core.eventlog import event_log

    if progress is not None:
        try:
            progress.update(8, "starting")
        except Exception:
            pass

    res = download_youtube_sync(url, media_type, progress=progress)
    if not res:
        raise RuntimeError(LAST_DL_ERROR or "download failed")
    path, filename = res

    is_public_url = path.startswith("https://") or path.startswith("http://")
    file_path = path if not is_public_url else None

    event_log.append("task", "download_ready",
                     summary=f"download {media_type} ready: {filename}",
                     user_id=owner_user_id or None,
                     jid=owner_jid or None,
                     payload={"task_id": task_id, "filename": filename,
                              "url": path, "media_type": media_type})

    return {"path": path, "filename": filename, "media_type": media_type,
            "is_public": is_public_url}


def download_youtube_sync(url: str, media_type: str = "audio", retries: int = 2,
                           *, progress=None) -> tuple[str, str] | None:
    global LAST_DL_ERROR
    LAST_DL_ERROR = "Download in progress..."

    temp_id = f"{int(time.time()*1000)}_{random.randint(1000, 9999)}"
    temp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
    prefix = f"song_{temp_id}---"
    out_template = os.path.join(temp_dir, f"{prefix}%(title)s.%(ext)s")

    common_opts = [
        "yt-dlp", "--force-ipv4", "--no-playlist", "--ignore-errors",
        "--no-overwrites", "--continue", "--retries", "10",
        "--socket-timeout", "15", "--concurrent-fragments", "4",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--restrict-filenames",
        # Stable line-by-line progress (we stream stdout to drive a progress message)
        "--newline", "--no-color",
        # Use the Android client to negotiate combined H.264/AAC streams — this
        # is the same flag the reference shell downloader uses and is what gets
        # us a video with audio instead of a video-only stream.
        "--extractor-args", "youtube:player_client=android",
    ]

    yt_cookies = os.getenv("YT_COOKIES", "").strip()
    cookie_file = None
    if yt_cookies and os.path.isfile(yt_cookies):
        cookie_file = yt_cookies
        common_opts += ["--cookies", cookie_file]

    if media_type == "audio":
        cmd = common_opts + ["-x", "--audio-format", "mp3", "--audio-quality", "0", "-f", FORMAT_AUDIO, "--output", out_template, url]
    else:
        cmd = common_opts + [
            "-f", FORMAT_VIDEO,
            "-S", "vcodec:h264,ext:mp4,res,acodec:aac",
            "--merge-output-format", "mp4",
            "--remux-video", "mp4",
            "--output", out_template,
            url,
        ]

    for attempt in range(retries):
        try:
            log.info("[Media] yt-dlp cmd: %s", " ".join(cmd))
            # Use Popen so we can stream progress lines into the WhatsApp progress
            # message. Without this the user just sees a static "🎬 downloading..."
            # for the entire yt-dlp run.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            last_pct_fired = 0
            stderr_tail: list[str] = []
            rc: int | None = None
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    stderr_tail.append(line)
                    if len(stderr_tail) > 30:
                        stderr_tail.pop(0)
                    # [download]  12.3% of  3.45MiB at 1.23MiB/s ETA 00:02
                    m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)\s*%", line)
                    if m:
                        pct = int(float(m.group(1)))
                        if pct >= last_pct_fired + 5 and progress is not None:
                            try:
                                progress.update(pct, "downloading")
                            except Exception:
                                pass
                            last_pct_fired = pct
                        continue
                    if "ExtractAudio" in line and progress is not None:
                        try:
                            progress.update(max(last_pct_fired, 60), "converting")
                        except Exception:
                            pass
                        continue
                    if ("Merger" in line or "Merging formats" in line) and progress is not None:
                        try:
                            progress.update(max(last_pct_fired, 80), "merging")
                        except Exception:
                            pass
                        continue
                rc = proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                LAST_DL_ERROR = "yt-dlp timed out after 600s"
                log.warning("[Media] yt-dlp timeout")
                continue
            except Exception as e:
                if proc.poll() is None:
                    proc.kill()
                raise

            if rc != 0:
                tail = "\n".join(stderr_tail)[-300:]
                LAST_DL_ERROR = f"yt-dlp error ({rc}): {tail}"
                log.warning("[Media] yt-dlp failed rc=%s: %s", rc, tail)
                continue

            # collect candidate files produced by yt-dlp
            candidates = [f for f in os.listdir(temp_dir) if f.startswith(prefix)]
            candidates = [os.path.join(temp_dir, f) for f in candidates if os.path.getsize(os.path.join(temp_dir, f)) > 5000]
            log.info("[Media] yt-dlp rc=0, candidates=%s", [(os.path.basename(c), os.path.getsize(c)) for c in candidates])
            if not candidates:
                continue

            chosen = None
            # Prefer files that actually contain audio when requesting video
            if media_type == "video":
                for p in candidates:
                    try:
                        if _file_has_audio(p):
                            chosen = p
                            break
                    except Exception:
                        continue

                # If none of the candidates contain audio but we have separate audio+video files, try to merge
                if not chosen:
                    video_p = None
                    audio_p = None
                    for p in candidates:
                        low = p.lower()
                        if low.endswith(('.m4a', '.mp3', '.aac', '.opus', '.ogg')):
                            audio_p = p
                        elif low.endswith(('.mp4', '.mkv', '.webm', '.mov')):
                            video_p = p if not video_p or os.path.getsize(p) > os.path.getsize(video_p) else video_p

                    if video_p and audio_p:
                        merged = os.path.join(temp_dir, f"{prefix}merged_{int(time.time())}.mp4")
                        try:
                            cmd_merge = [
                                'ffmpeg', '-i', video_p, '-i', audio_p,
                                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-y', merged
                            ]
                            mres = subprocess.run(cmd_merge, capture_output=True, text=True, timeout=300)
                            if mres.returncode == 0 and os.path.exists(merged) and os.path.getsize(merged) > 5000:
                                chosen = merged
                                log.info("[Media] merged video+audio into %s", merged)
                                try:
                                    os.remove(video_p)
                                except Exception:
                                    pass
                                try:
                                    os.remove(audio_p)
                                except Exception:
                                    pass
                        except Exception:
                            chosen = None

                # If still nothing with audio, refuse to ship a silent file.
                if not chosen:
                    LAST_DL_ERROR = "yt-dlp produced no candidate with an audio track"
                    log.warning("[Media] no candidate has audio; refusing silent video. candidates=%s",
                                [os.path.basename(c) for c in candidates])
                    for c in candidates:
                        try: os.remove(c)
                        except Exception: pass
                    continue

            else:  # audio
                # pick a candidate that likely is audio
                for p in candidates:
                    if p.lower().endswith(('.m4a', '.mp3', '.aac', '.opus', '.ogg')):
                        chosen = p
                        break
                if not chosen:
                    chosen = max(candidates, key=lambda x: os.path.getsize(x))

            p = chosen

            if media_type == "video":
                # AAC guarantee pass for WhatsApp compatibility:
                # 1. Ensure we have audio.
                # 2. Ensure audio codec is AAC (mp4a).
                # 3. Ensure container is MP4.
                has_audio = _file_has_audio(p)
                if not has_audio:
                    log.warning("[Media] candidate %s has no audio track — rejecting", p)
                    LAST_DL_ERROR = "video stream has no audio track"
                    try: os.remove(p)
                    except Exception: pass
                    continue

                audio_codec = _probe_audio_codec(p) or ""
                log.info("[Media] chosen=%s size=%s audio_codec=%s", os.path.basename(p), os.path.getsize(p), audio_codec or "<none>")

                try:
                    needs_transcode = False
                    needs_remux = False
                    if audio_codec.lower() not in ("aac", "mp4a"):
                        needs_remux = True
                    if not p.lower().endswith('.mp4'):
                        needs_transcode = True

                    if needs_transcode:
                        c = convert_video_for_whatsapp(p)
                        if c:
                            if c != p:
                                try: os.remove(p)
                                except Exception: pass
                            p = c
                    elif needs_remux:
                        base, _ = os.path.splitext(p)
                        remuxed = f"{base}_aac.mp4"
                        cmd_remux = [
                            'ffmpeg', '-i', p,
                            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                            '-movflags', '+faststart', '-y', remuxed,
                        ]
                        rres = subprocess.run(cmd_remux, capture_output=True, text=True, timeout=600)
                        if rres.returncode == 0 and os.path.exists(remuxed) and os.path.getsize(remuxed) > 5000:
                            log.info("[Media] remuxed to AAC: %s -> %s", os.path.basename(p), os.path.basename(remuxed))
                            try: os.remove(p)
                            except Exception: pass
                            p = remuxed
                        else:
                            log.warning("[Media] AAC remux failed; keeping original. rc=%s err=%s",
                                        rres.returncode, rres.stderr[:200])
                except Exception as e:
                    log.warning("[Media] post-processing failed: %s", e)
            elif media_type == "audio":
                c = convert_audio_for_whatsapp(p)
                if c:
                    if c != p:
                        try: os.remove(p)
                        except Exception: pass
                    p = c

            filename = os.path.basename(p).replace(prefix, "").replace("_", " ")
            if progress is not None:
                try:
                    progress.update(95, "preparing media")
                except Exception:
                    pass
            # Return local file path for direct native WhatsApp media delivery.
            # Only fall back to public upload if local file size exceeds 100MB.
            if os.path.getsize(p) > 100 * 1024 * 1024:
                upload_url = upload_file_public(p)
                if upload_url:
                    try: os.remove(p)
                    except Exception: pass
                    return upload_url, filename

            return p, filename
        except Exception as e:
            LAST_DL_ERROR = f"Exception: {e}"
            log.warning("[Media] download_youtube exception: %s", e)

    return None

# Tokens that signal the user wants the original / uncut / explicit version.
_ORIGINAL_TOKENS = (
    "explicit", "dirty", "uncut", "original", "uncensored",
    "extended", "full version", "album version",
)
# Tokens that signal a derivative the bot should usually avoid unless asked.
_DERIVATIVE_TOKENS = (
    "clean", "censored", "radio edit", "radio version", "edit",
    "sped up", "slowed", "nightcore", "8d", "reverb",
    "lyric", "lyrics", "1 hour", "loop",
    "cover", "remix", "karaoke", "instrumental", "tribute",
    "reaction", "react", "review",
)


def _score_search_hit(hit: dict, media_type: str, query: str = "") -> int:
    """Heuristic score for a YouTube search candidate. Higher = better match."""
    score = 0
    title = (hit.get("title") or "").lower()
    channel = (hit.get("channel") or "").lower()
    views = hit.get("views") or 0
    duration = hit.get("duration") or 0
    ql = (query or "").lower()

    # Query-token overlap — the strongest signal. A title that shares the
    # user's exact words beats one that merely looks popular.
    if ql:
        _stop = {"the", "a", "an", "of", "to", "in", "for", "and", "with", "official", "video", "lyrics", "youtube", "music", "audio"}
        q_tokens = {t for t in re.findall(r"[a-z0-9]+", ql) if t not in _stop}
        t_tokens = {t for t in re.findall(r"[a-z0-9]+", title) if t not in _stop}
        overlap = len(q_tokens & t_tokens)
        score += overlap * 2
        extra = len(t_tokens - q_tokens)
        score -= extra * 3
        if ql.strip() in title or title in ql.strip():
            score += 10

    # Popularity
    if views > 1_000_000:
        score += 3
    elif views > 100_000:
        score += 1

    # "Topic" channels are YouTube's auto-generated official-audio channels —
    # best source for clean audio.
    if "topic" in channel:
        score += 5

    # "Official" / VEVO labels strongly suggest the canonical upload.
    if "official" in title:
        score += 4
    if "vevo" in channel:
        score += 4

    # Duration sanity: 30s..10min is reasonable for a music track.
    if 30 <= duration <= 600:
        score += 2
    else:
        score -= 3
    if duration <= 0:
        score -= 1

    # Penalize "lyrics" for audio downloads (usually lower quality / wrong track).
    if media_type == "audio" and "lyrics" in title:
        score -= 2

    # Reward explicit/original phrasing unless the user asked for a clean/edited cut.
    user_wants_clean = any(t in ql for t in ("clean", "radio", "censored"))
    if not user_wants_clean:
        for tok in _ORIGINAL_TOKENS:
            if tok in title:
                score += 2
                break

    # Penalize derivative edits unless the user asked for them.
    user_wants_derivative = any(t in ql for t in _DERIVATIVE_TOKENS)
    if not user_wants_derivative:
        for tok in _DERIVATIVE_TOKENS:
            if tok in title:
                score -= 3
                break

    return score


def search_youtube(query: str, limit: int = 5, media_type: str = "video") -> list[dict]:
    """Search YouTube and return up to `limit` hits ranked by relevance for `media_type`.

    Always returns at least one hit (the raw top yt-dlp result) so callers
    that only read results[0] still get something.
    """
    return _ranked_search(query, limit=limit, media_type=media_type)


def search_youtube_with_versions(query: str, media_type: str = "video", limit: int = 5) -> list[dict]:
    """Public alias used by the tool path when the user wants to see options first.
    Identical to search_youtube; kept as a named entry point so callers can be
    explicit about intent (don't auto-download, present list to user).
    """
    return _ranked_search(query, limit=limit, media_type=media_type)


def _ranked_search(query: str, limit: int = 5, media_type: str = "video") -> list[dict]:
    fetch = max(limit * 2, 10)
    cmd = [
        "yt-dlp", f"ytsearch{fetch}:{query}",
        "--flat-playlist", "--dump-json", "--skip-download",
        "--socket-timeout", "10",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        results = []
        for line in res.stdout.splitlines():
            try:
                d = json.loads(line)
                results.append({
                    "title": d.get("title", "Unknown"),
                    "url": d.get("webpage_url") or d.get("url", ""),
                    "duration": d.get("duration", 0) or 0,
                    "channel": d.get("channel") or d.get("uploader") or "Unknown",
                    "views": d.get("view_count") or 0,
                })
            except Exception:
                continue
        if not results:
            return []

        scored = [(h, _score_search_hit(h, media_type, query)) for h in results]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        ranked = [h for h, _ in scored]

        top = scored[0]
        log.info("[Media] search '%s' (media_type=%s) returned %d hits, picked '%s' (score=%s, views=%s, dur=%ss)",
                 query, media_type, len(scored), top[0].get("title"), top[1],
                 top[0].get("views"), top[0].get("duration"))
        for h, s in scored[:limit]:
            log.info("[Media]   candidate score=%s title='%s' channel='%s' views=%s dur=%ss",
                     s, h.get("title"), h.get("channel"), h.get("views"), h.get("duration"))

        return ranked[:limit]
    except Exception as e:
        log.warning("[Media] search_youtube failed: %s", e)
        return []
